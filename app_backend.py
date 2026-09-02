# -*- coding: utf-8 -*-
"""
日志自动下载工具 - 后端 API (支持远程截取)
运行: python app_backend.py
"""

from flask import Flask, render_template, request, jsonify, send_file, Response
from flask_cors import CORS
import paramiko
import time
import os
import threading
import tarfile
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import duckdb
import pandas as pd
import sys
import gzip
import shutil
import zipfile
import bz2

# 设置控制台编码
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

app = Flask(__name__)
CORS(app)

# ========== SSH 连接管理器 ==========
class SSHManager:
    def __init__(self):
        self.connections = {}
        self.lock = threading.Lock()
    
    def get_connection(self, host, username, password, port=22):
        key = f"{host}:{username}"
        with self.lock:
            if key in self.connections:
                transport = self.connections[key].get_transport()
                if transport and transport.is_active():
                    return self.connections[key]
                else:
                    del self.connections[key]
            
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(host, port=port, username=username, password=password, timeout=15)
            ssh.get_transport().set_keepalive(30)
            self.connections[key] = ssh
            return ssh
    
    def close_all(self):
        for ssh in self.connections.values():
            ssh.close()
        self.connections.clear()

ssh_manager = SSHManager()

# ========== 配置 ==========
DB_PATH = r"D:\Onstar\OpenShift\mydb.duckdb"  # 替换为实际路径
CONFIG_FILE = "web_config_v2.json"

# ========== 根路由 ==========
@app.route('/')
def index():
    return render_template('index.html')

# ========== DuckDB 连接 ==========
def get_db_connection():
    return duckdb.connect(DB_PATH)

def get_servers_from_db(filters=None):
    conn = get_db_connection()
    
    query = """
    SELECT 
        Project, 
        Env, 
        Application, 
        Component, 
        IP, 
        POD, 
        "Instance Name", 
        "App OS Username",
        "DNS Name New",
        "Weblogic Password",
        "HTTP Port"
    FROM BuildSheet 
    WHERE IP IS NOT NULL AND IP != ''
    """
    conditions = []
    params = []
    
    if filters:
        if filters.get('project'):
            conditions.append("Project = ?")
            params.append(filters['project'])
        if filters.get('env'):
            conditions.append("Env = ?")
            params.append(filters['env'])
        if filters.get('application'):
            conditions.append("Application = ?")
            params.append(filters['application'])
        if filters.get('component'):
            conditions.append("Component LIKE ?")
            params.append(f"%{filters['component']}%")
        if filters.get('pod'):
            conditions.append("POD LIKE ?")
            params.append(f"%{filters['pod']}%")
        if filters.get('instanceType'):
            conditions.append("InstanceType = ?")
            params.append(filters['instanceType'])
    
    if conditions:
        query += " AND " + " AND ".join(conditions)
    
    query += " ORDER BY Project, Env, Application, Component"
    
    # 添加分页支持
    if filters and filters.get('limit'):
        query += f" LIMIT {int(filters['limit'])}"
    if filters and filters.get('offset'):
        query += f" OFFSET {int(filters['offset'])}"
    print(f"[DEBUG] SQL: {query}")
    print(f"[DEBUG] Params: {params}")
    
    try:
        df = conn.execute(query, params).fetchdf()
    except Exception as e:
        conn.close()
        print(f"[ERROR] Database query failed: {e}")
        raise
    conn.close()
    
    df = df.where(pd.notnull(df), None)
    return df.to_dict('records')

# ========== 辅助函数 ==========
def sanitize_kw(s: str) -> str:
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in s)

def build_kw_suffix(keywords):
    if not keywords:
        return "ALL"
    kw = "_".join(sanitize_kw(k) for k in keywords)
    return kw[:80]

def create_ssh_channel_via_bastion(bastion_host, bastion_user, bastion_pwd,
                                   target_host, timeout=20, bastion_keepalive=30, log_callback=None):
    jump = paramiko.SSHClient()
    jump.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    jump.connect(bastion_host, username=bastion_user, password=bastion_pwd,
                 timeout=timeout, banner_timeout=timeout, auth_timeout=timeout)
    transport = jump.get_transport()
    if bastion_keepalive:
        try:
            transport.set_keepalive(int(bastion_keepalive))
        except Exception:
            pass
    chan = transport.open_channel("direct-tcpip", (target_host, 22), ('', 0))
    return jump, chan

# ========== 远程截取打包命令生成 ==========
def build_remote_truncate_pack_command(directories, days, keywords, kw_mode, truncate_config):
    """
    生成远程命令：
    1. 查找符合条件的文件列表
    2. 对每个文件按配置进行截取，生成临时文件（原文件名.truncated）
    3. 将所有截取后的临时文件打包为 tar.gz
    支持普通文本文件和 gzip 压缩文件（自动 zcat 解压后截取）
    """
    kws = [k.replace("'", "'\"'\"'") for k in keywords if k.strip() != ""]
    targets = " ".join(directories)

    # ---------- 1. 查找文件 ----------
    if not kws:
        find_cmd = f"""
files=$(find {targets} -type f -mtime -{days} 2>/dev/null)
"""
    else:
        if kw_mode.upper() == "OR":
            regex = "|".join([k for k in kws])
            find_cmd = f"""
files=$(find {targets} -type f -mtime -{days} -print0 2>/dev/null | xargs -0 zgrep -Il -E '{regex}' 2>/dev/null)
"""
        else:
            find_cmd = f"""
tmpf=$(mktemp /tmp/_logfiles.XXXXXX)
find {targets} -type f -mtime -{days} -print0 2>/dev/null | xargs -0 -r zgrep -Il '{kws[0]}' 2>/dev/null | tr '\\n' '\\0' > "$tmpf"
"""
            for k in kws[1:]:
                find_cmd += f"""
if [ -s "$tmpf" ]; then
    mv "$tmpf" "$tmpf".bak || true
    cat "$tmpf".bak | tr '\\0' '\\n' | xargs -r -I{{}} zgrep -Il '{k}' "{{}}" 2>/dev/null | tr '\\0' '\\n' > "$tmpf"
fi
"""
            find_cmd += """
files=$(cat "$tmpf" | tr '\\0' '\\n')
rm -f "$tmpf"* 2>/dev/null || true
"""

    # 如果没有截取配置，直接打包原文件
    if not truncate_config:
        pack_cmd = """
if [ -z "$files" ]; then
    echo "NO_FILES"
else
    listf=$(mktemp /tmp/_filelist.XXXXXX)
    printf "%s\\n" $files > "$listf"
    tar -czf {remote_tar} -T "$listf" 2>/dev/null && echo "TAR_CREATED" || echo "TAR_FAILED"
    rm -f "$listf" 2>/dev/null || true
fi
"""
        return find_cmd + pack_cmd

    # ---------- 2. 有截取配置：对每个文件进行截取 ----------
    truncate_cmd = """
if [ -z "$files" ]; then
    echo "NO_FILES"
    exit 0
fi
temp_tar_list=$(mktemp /tmp/_trunc_list.XXXXXX)
for f in $files; do
    if [ ! -f "$f" ]; then continue; fi
    outfile="${f}.truncated"
    # 判断是否为 gzip 压缩文件
    if file "$f" | grep -q "gzip compressed"; then
        DECOMP="zcat"
    else
        DECOMP="cat"
    fi
"""

    if truncate_config['mode'] == 'line':
        start = truncate_config['start_line']
        end = truncate_config['end_line']
        truncate_cmd += f"""
    $DECOMP "$f" | sed -n '{start},{end}p' > "$outfile" 2>/dev/null
"""
    # elif truncate_config['mode'] == 'string':
        # start_str = truncate_config['start_str'].replace("'", "'\\''")
        # # 使用 grep -F 进行固定字符串匹配，并输出匹配行到 outfile
        # truncate_cmd += f"""
        # $DECOMP "$f" | grep -F '{start_str}' > "$outfile" 2>/dev/null
    # """
    elif truncate_config['mode'] == 'string':
    # 支持多个开始字符串（逗号分隔），OR 关系
        start_strs = [s.strip() for s in truncate_config['start_str'].split(',') if s.strip()]
        
        if not start_strs:
            # 如果没有有效的字符串，跳过该文件
            truncate_cmd += "    outfile=\"$f\"\n"
        elif len(start_strs) == 1:
            # 单个字符串：直接 grep
            start_str = start_strs[0].replace("'", "'\\''")
            truncate_cmd += f"""
        $DECOMP "$f" | grep -F '{start_str}' > "$outfile" 2>/dev/null
    """
        else:
            # 多个字符串：使用 grep -F 的 -e 参数（OR 关系）
            grep_patterns = []
            for s in start_strs:
                escaped = s.replace("'", "'\\''")
                grep_patterns.append(f"-e '{escaped}'")
            grep_options = ' '.join(grep_patterns)
            truncate_cmd += f"""
        $DECOMP "$f" | grep -F {grep_options} > "$outfile" 2>/dev/null
    """
    else:
        # 不支持的模式，回退到原文件
        truncate_cmd += "    outfile=\"$f\"\n"

    truncate_cmd += """
    if [ -s "$outfile" ]; then
        echo "$outfile" >> "$temp_tar_list"
    else
        rm -f "$outfile" 2>/dev/null
    fi
done
if [ ! -s "$temp_tar_list" ]; then
    echo "NO_FILES"
    rm -f "$temp_tar_list"
    exit 0
fi
tar -czf {remote_tar} -T "$temp_tar_list" 2>/dev/null && echo "TAR_CREATED" || echo "TAR_FAILED"
rm -f "$temp_tar_list"
# 清理临时截取文件
for f in $files; do rm -f "${f}.truncated" 2>/dev/null; done
"""
    return find_cmd + truncate_cmd

# ========== 执行单台服务器的任务 ==========
def perform_server_task(server_info, custom_username, custom_password, directories, days, 
                        keywords, kw_mode, save_dir, retries, keep_remote_tar, 
                        bastion_cfg, log_callback, timeout=20, truncate_config=None):
    
    host = server_info.get('IP')
    username = custom_username if custom_username else server_info.get('App OS Username', 'root')
    password = custom_password if custom_password else server_info.get('Weblogic Password', '')
    
    project = server_info.get('Project', '')
    env = server_info.get('Env', '')
    app = server_info.get('Application', '')
    component = server_info.get('Component', '')
    instance = server_info.get('Instance Name', '')
    
    kw_suffix = build_kw_suffix(keywords)
    result = {
        "host": host,
        "project": project,
        "env": env,
        "app": app,
        "component": component,
        "instance": instance,
        "success": False,
        "message": "",
        "tar_local": None,
        "tar_size": 0
    }
    
    def log(msg):
        if log_callback:
            log_callback(host, msg)
    
    try:
        last_exc = None
        for attempt in range(max(1, retries)):
            try:
                if bastion_cfg and bastion_cfg.get("host"):
                    jump_host = bastion_cfg.get("host")
                    jump_user = bastion_cfg.get("user")
                    jump_pwd = bastion_cfg.get("password")
                    jump_client, channel = create_ssh_channel_via_bastion(
                        jump_host, jump_user, jump_pwd, host, timeout=timeout, 
                        bastion_keepalive=bastion_cfg.get("keepalive", 30), log_callback=log
                    )
                    ssh = paramiko.SSHClient()
                    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    log(f"通过跳板连接 {host}（尝试 {attempt+1}/{retries}）")
                    ssh.connect(hostname=host, username=username, password=password, sock=channel,
                                timeout=timeout, banner_timeout=timeout, auth_timeout=timeout)
                    using_jump = True
                else:
                    ssh = ssh_manager.get_connection(host, username, password)
                    log(f"直连 {host}（尝试 {attempt+1}/{retries}）")
                    using_jump = False
                break
            except Exception as e:
                last_exc = e
                log(f"连接失败: {e}")
                time.sleep(1 + attempt*2)
        else:
            result["message"] = f"连接失败: {last_exc}"
            return result

        ts = int(time.time())
        remote_tar = f"/tmp/logs_{host}_{ts}.tar.gz"
        remote_cmd = build_remote_truncate_pack_command(directories, int(days), keywords, kw_mode, truncate_config)
        remote_cmd = remote_cmd.replace("{remote_tar}", remote_tar)
        log("查找并打包日志（远程截取模式）...")
        # stdin, stdout, stderr = ssh.exec_command(remote_cmd, timeout=600)
        # out = stdout.read().decode()
        # err = stderr.read().decode()
        stdin, stdout, stderr = ssh.exec_command(remote_cmd, timeout=600)
        out = stdout.read().decode()
        err = stderr.read().decode()
        log(f"命令输出: {out[:200]}")   # 打印前200字符
        if err:
            log(f"命令错误: {err[:200]}")
            
        if "NO_FILES" in out:
            result["message"] = "未找到匹配日志"
            ssh.close()
            if using_jump:
                jump_client.close()
            return result
        if "TAR_FAILED" in out:
            result["message"] = f"打包失败: {err}"
            ssh.close()
            if using_jump:
                jump_client.close()
            return result
        if "TAR_CREATED" not in out:
            result["message"] = f"响应异常"
            ssh.close()
            if using_jump:
                jump_client.close()
            return result

        safe_project = project.replace('/', '_') if project else "unknown"
        safe_app = app.replace('/', '_') if app else "unknown"
        local_tar = os.path.join(save_dir, f"logs_{safe_project}_{env}_{safe_app}_{host}_{instance}_{ts}_RID_{kw_suffix}.tar.gz")
        
        try:
            sftp = ssh.open_sftp()
            log(f"下载截取后的日志包...")
            sftp.get(remote_tar, local_tar)
            sftp.close()
            result["tar_local"] = local_tar
            result["tar_size"] = os.path.getsize(local_tar)
            log(f"下载完成: {os.path.basename(local_tar)}")
        except Exception as e:
            result["message"] = f"下载失败: {e}"
            ssh.close()
            if using_jump:
                jump_client.close()
            return result

        # 可选：本地递归解压（如果截取后的包内可能还有嵌套压缩包）
        try:
            extract_dir = local_tar.replace('.tar.gz', '')
            os.makedirs(extract_dir, exist_ok=True)
            with tarfile.open(local_tar, "r:gz") as tar:
                tar.extractall(path=extract_dir)
            log(f"本地解压完成: {extract_dir}")
        except Exception as e:
            log(f"本地解压失败: {e}")

        if not keep_remote_tar:
            try:
                ssh.exec_command(f"rm -f {remote_tar} 2>/dev/null")
            except Exception:
                pass

        ssh.close()
        if using_jump:
            jump_client.close()

        result["success"] = True
        result["message"] = "成功"
        return result

    except Exception as e:
        result["message"] = f"异常: {e}"
        return result

def execute_remote_command(server_info, custom_username, custom_password, command, 
                           bastion_cfg, log_callback=None, timeout=60):
    """执行任意远程命令"""
    host = server_info.get('IP')
    username = custom_username if custom_username else server_info.get('App OS Username', 'root')
    password = custom_password if custom_password else server_info.get('Weblogic Password', '')
    
    project = server_info.get('Project', '')
    env = server_info.get('Env', '')
    app = server_info.get('Application', '')
    
    result = {
        "host": host,
        "project": project,
        "env": env,
        "app": app,
        "component": server_info.get('Component', ''),
        "success": False,
        "stdout": "",
        "stderr": ""
    }
    
    def log(msg):
        if log_callback:
            log_callback(host, msg)
    
    try:
        last_exc = None
        for attempt in range(3):
            try:
                if bastion_cfg and bastion_cfg.get("host"):
                    jump_host = bastion_cfg.get("host")
                    jump_user = bastion_cfg.get("user")
                    jump_pwd = bastion_cfg.get("password")
                    jump_client, channel = create_ssh_channel_via_bastion(
                        jump_host, jump_user, jump_pwd, host, timeout=20,
                        bastion_keepalive=bastion_cfg.get("keepalive", 30), log_callback=log
                    )
                    ssh = paramiko.SSHClient()
                    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    log(f"通过跳板连接 {host}")
                    ssh.connect(hostname=host, username=username, password=password, sock=channel,
                                timeout=20, banner_timeout=20, auth_timeout=20)
                    using_jump = True
                else:
                    ssh = ssh_manager.get_connection(host, username, password)
                    log(f"直连 {host}")
                    using_jump = False
                break
            except Exception as e:
                last_exc = e
                log(f"连接失败 (尝试 {attempt+1}/3): {e}")
                time.sleep(2)
        else:
            result["stderr"] = f"连接失败: {last_exc}"
            return result
        
        log(f"执行命令: {command[:100]}...")
        stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
        out = stdout.read().decode('utf-8', errors='ignore')
        err = stderr.read().decode('utf-8', errors='ignore')
        
        result["stdout"] = out.strip()
        result["stderr"] = err.strip()
        result["success"] = True
        
        ssh.close()
        if using_jump:
            jump_client.close()
    except Exception as e:
        result["stderr"] = str(e)
    
    return result

# ========== 全局状态 ==========
log_queue = []
task_status = {"running": False, "progress": 0, "total": 0, "results": []}

@app.route('/vcomm-instance')
def vcomm_instance():
    return render_template('vcomm_instance.html')
    
@app.route('/api/vcomm/query', methods=['POST'])
def query_vcomm():
    data = request.json
    keyword = data.get('keyword', '').strip()
    conn = get_db_connection()
    if keyword:
        query = """
        SELECT IP, "Project/Environment/Application", Component, Status, "Output", "Error"
        FROM VCOMM
        WHERE "Output" LIKE ?
        ORDER BY IP, Component
        """
        params = [f"%{keyword}%"]
    else:
        query = """
        SELECT IP, "Project/Environment/Application", Component, Status, "Output", "Error"
        FROM VCOMM
        ORDER BY IP, Component
        LIMIT 500
        """
        params = []
    try:
        df = conn.execute(query, params).fetchdf()
        conn.close()
        df = df.where(pd.notnull(df), None)
        results = df.to_dict('records')
        return jsonify({'success': True, 'data': results, 'count': len(results), 'keyword': keyword})
    except Exception as e:
        conn.close()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/vcomm/filter_options', methods=['GET'])
def vcomm_filter_options():
    conn = get_db_connection()
    try:
        components = conn.execute("SELECT DISTINCT Component FROM VCOMM WHERE Component IS NOT NULL AND Component != '' ORDER BY Component").fetchall()
        statuses = conn.execute("SELECT DISTINCT Status FROM VCOMM WHERE Status IS NOT NULL AND Status != '' ORDER BY Status").fetchall()
        conn.close()
        return jsonify({'success': True, 'components': [c[0] for c in components], 'statuses': [s[0] for s in statuses]})
    except Exception as e:
        conn.close()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/servers')
def servers_api():
    filters = {
        'project': request.args.get('project', ''),
        'env': request.args.get('env', ''),
        'application': request.args.get('application', ''),
        'component': request.args.get('component', ''),
        'pod': request.args.get('pod', ''),
        'instanceType': request.args.get('instanceType', ''),
        # 添加分页参数，默认限制500条防止一次性加载过多
        'limit': request.args.get('limit', 200),
        'offset': request.args.get('offset', 0)
    }
    try:
        servers = get_servers_from_db(filters)
        return jsonify({'servers': servers})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/filter_options')
def filter_options():
    conn = get_db_connection()
    projects = conn.execute("SELECT DISTINCT Project FROM BuildSheet WHERE Project IS NOT NULL AND Project != '' ORDER BY Project").fetchall()
    envs = conn.execute("SELECT DISTINCT Env FROM BuildSheet WHERE Env IS NOT NULL AND Env != '' ORDER BY Env").fetchall()
    applications = conn.execute("SELECT DISTINCT Application FROM BuildSheet WHERE Application IS NOT NULL AND Application != '' ORDER BY Application").fetchall()
    instanceTypes = conn.execute("SELECT DISTINCT InstanceType FROM BuildSheet WHERE InstanceType IS NOT NULL AND InstanceType != '' ORDER BY InstanceType").fetchall()
    conn.close()
    return jsonify({
        'projects': [p[0] for p in projects],
        'envs': [e[0] for e in envs],
        'applications': [a[0] for a in applications],
        'instanceTypes': [i[0] for i in instanceTypes]
    })

@app.route('/api/config', methods=['GET', 'POST'])
def config_api():
    if request.method == 'POST':
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(request.json, f, ensure_ascii=False, indent=2)
        return jsonify({'status': 'ok'})
    else:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return jsonify(json.load(f))
        return jsonify({})

@app.route('/api/logstream')
def log_stream():
    def generate():
        last_id = 0
        while True:
            if last_id < len(log_queue):
                for entry in log_queue[last_id:]:
                    yield f"data: {json.dumps(entry)}\n\n"
                last_id = len(log_queue)
            time.sleep(0.5)
    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/start', methods=['POST'])
def start_task():
    global task_status, log_queue
    if task_status["running"]:
        return jsonify({'status': 'already_running'})
    
    data = request.json
    servers = data.get('servers', [])
    custom_username = data.get('custom_username', '')
    custom_password = data.get('custom_password', '')
    directories = [d.strip() for d in data.get('directories', '').split(',') if d.strip()]
    days = int(data.get('days', 3))
    keywords = [k.strip() for k in data.get('keywords', '').split(',') if k.strip()]
    kw_mode = data.get('kw_mode', 'OR')
    threads = int(data.get('threads', 5))
    save_dir = data.get('save_dir', './downloaded_logs')
    retries = int(data.get('retries', 3))
    keep_remote = data.get('keep_remote', False)
    truncate_config = data.get('truncate_config', None)
    
    bastion_cfg = None
    if data.get('bastion_host'):
        bastion_cfg = {
            "host": data.get('bastion_host'),
            "user": data.get('bastion_user', ''),
            "password": data.get('bastion_pwd', ''),
            "keepalive": 30
        }
    
    os.makedirs(save_dir, exist_ok=True)
    
    task_status = {"running": True, "progress": 0, "total": len(servers), "results": []}
    log_queue = []
    
    def log_callback(host, msg):
        log_queue.append({"host": host, "message": msg, "time": datetime.now().isoformat()})
    
    def run():
        global task_status
        results = []
        completed = 0
        
        with ThreadPoolExecutor(max_workers=threads) as exe:
            futures = {}
            for server in servers:
                fut = exe.submit(perform_server_task, server, custom_username, custom_password,
                                 directories, days, keywords, kw_mode, save_dir, retries, 
                                 keep_remote, bastion_cfg, log_callback,
                                 truncate_config=truncate_config)
                futures[fut] = server.get('IP', 'unknown')
            for fut in as_completed(futures):
                r = fut.result()
                results.append(r)
                completed += 1
                task_status["progress"] = completed
                task_status["results"] = results
        task_status["running"] = False
    
    threading.Thread(target=run, daemon=True).start()
    return jsonify({'status': 'started'})

@app.route('/api/status')
def status():
    return jsonify(task_status)

@app.route('/download/<path:filepath>')
def download_file(filepath):
    return send_file(filepath, as_attachment=True)

@app.route('/api/preview_local', methods=['POST'])
def preview_local():
    data = request.json
    filepath = data.get('filepath')
    offset = int(data.get('offset', 0))
    limit = int(data.get('limit', 1000))
    keyword = data.get('keyword', '')
    
    if not filepath:
        return jsonify({'success': False, 'error': '文件路径为空'})
    if not os.path.isabs(filepath):
        filepath = os.path.abspath(filepath)
    if not os.path.exists(filepath):
        return jsonify({'success': False, 'error': f'文件不存在: {filepath}'})
    
    file_size = os.path.getsize(filepath)
    if file_size > 100 * 1024 * 1024:
        return jsonify({'success': False, 'error': f'文件过大 ({file_size // 1024 // 1024}MB)，请下载后查看'})
    
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            total_lines = 0
            for _ in f:
                total_lines += 1
        lines = []
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            for i, line in enumerate(f):
                if i >= offset and i < offset + limit:
                    lines.append(line)
                elif i >= offset + limit:
                    break
        content = ''.join(lines)
        if keyword:
            filtered_lines = []
            for line in lines:
                if keyword.lower() in line.lower():
                    filtered_lines.append(line)
            content = ''.join(filtered_lines)
        filename = os.path.basename(filepath)
        return jsonify({
            'success': True,
            'filename': filename,
            'filepath': filepath,
            'content': content,
            'total_lines': total_lines,
            'offset': offset,
            'limit': limit,
            'has_more': offset + limit < total_lines,
            'keyword': keyword
        })
    except UnicodeDecodeError:
        return jsonify({'success': False, 'error': '文件编码不是UTF-8，可能是二进制文件'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/list_extracted_files', methods=['POST'])
def list_extracted_files():
    data = request.json
    tar_path = data.get('tar_path')
    if not tar_path or not tar_path.endswith('.tar.gz'):
        return jsonify({'success': False, 'error': f'无效的文件路径: {tar_path}'})
    extract_dir = tar_path.replace('.tar.gz', '')
    if not os.path.isabs(extract_dir):
        extract_dir = os.path.abspath(extract_dir)
    if not os.path.exists(extract_dir):
        return jsonify({'success': False, 'error': f'解压目录不存在: {extract_dir}'})
    log_files = []
    for root, dirs, files in os.walk(extract_dir):
        for f in files:
            if f.endswith('.tar.gz'):
                continue
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, extract_dir)
            is_text = False
            text_extensions = ('.log', '.txt', '.out', '.err', '.log4j', '.log.', '.trace', '.debug')
            if any(f.endswith(ext) for ext in text_extensions) or '.log4j' in f:
                is_text = True
            elif os.path.getsize(full_path) < 10 * 1024 * 1024:
                try:
                    with open(full_path, 'rb') as test_f:
                        sample = test_f.read(100)
                        if b'\x00' not in sample:
                            is_text = True
                except:
                    pass
            if is_text:
                log_files.append({'name': rel_path, 'path': full_path, 'size': os.path.getsize(full_path)})
    log_files.sort(key=lambda x: x['name'])
    return jsonify({'success': True, 'files': log_files, 'extract_dir': extract_dir})

@app.route('/api/check_weblogic', methods=['POST'])
def check_weblogic():
    data = request.json
    servers = data.get('servers', [])
    custom_username = data.get('custom_username', '')
    custom_password = data.get('custom_password', '')
    bastion_cfg = None
    if data.get('bastion_host'):
        bastion_cfg = {
            "host": data.get('bastion_host'),
            "user": data.get('bastion_user', ''),
            "password": data.get('bastion_pwd', ''),
            "keepalive": 30
        }
    command = '''bash -c '
for name in /onstarlog/*/; do
    name=$(basename "$name")
    pid=$(ps -auxww | grep "weblogic.Name=$name" | grep -v grep | awk "{print \$2}")
    if [ -z "$pid" ]; then
        echo "$name|DOWN"
    else
        echo "$name|$pid"
    fi
done
'
'''
    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = []
        for server in servers:
            future = executor.submit(execute_remote_command, server, custom_username, 
                                     custom_password, command, bastion_cfg)
            futures.append(future)
        for future in as_completed(futures):
            results.append(future.result())
    return jsonify({"results": results})

@app.route('/api/check_pidpy', methods=['POST'])
def check_pidpy():
    data = request.json
    servers = data.get('servers', [])
    custom_username = data.get('custom_username', '')
    custom_password = data.get('custom_password', '')
    bastion_cfg = None
    if data.get('bastion_host'):
        bastion_cfg = {
            "host": data.get('bastion_host'),
            "user": data.get('bastion_user', ''),
            "password": data.get('bastion_pwd', ''),
            "keepalive": 30
        }
    command = "bash -lc '/usr/bin/python /onstardata/onstar/vcCOMM/commserver/bin/PID.py'"
    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = []
        for server in servers:
            future = executor.submit(execute_remote_command, server, custom_username,
                                     custom_password, command, bastion_cfg)
            futures.append(future)
        for future in as_completed(futures):
            results.append(future.result())
    return jsonify({"results": results})

@app.route('/api/check_vcomm_version', methods=['POST'])
def check_vcomm_version():
    data = request.json
    servers = data.get('servers', [])
    custom_username = data.get('custom_username', '')
    custom_password = data.get('custom_password', '')
    bastion_cfg = None
    if data.get('bastion_host'):
        bastion_cfg = {
            "host": data.get('bastion_host'),
            "user": data.get('bastion_user', ''),
            "password": data.get('bastion_pwd', ''),
            "keepalive": 30
        }
    # 远程命令（多行，直接在远程 shell 执行）
    command = r"""
cd /onstardata/onstar/vcCOMM/commserver/bin && ls -lrt $(ls -l | awk '/^l/ && $9 !~ /\.sh$/ {printf "../lib/COMMSERVERJAR_%s ../lib/COMMONJAR_%s ../lib/VCSINTERFACE_%s ", $9, $9, $9}') 2>/dev/null | awk '{sub(/\.\.\/lib\//, "", $9); printf "%-18s %-35s -> %s\n", $6" "$7" "$8, $9, $11}'"""
    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = []
        for server in servers:
            future = executor.submit(execute_remote_command, server, custom_username,
                                     custom_password, command, bastion_cfg)
            futures.append(future)
        for future in as_completed(futures):
            results.append(future.result())
    return jsonify({"results": results})
    
if __name__ == '__main__':
    import atexit
    atexit.register(lambda: ssh_manager.close_all())
    
    try:
        conn = get_db_connection()
        count = conn.execute("SELECT COUNT(*) FROM BuildSheet").fetchone()[0]
        conn.close()
        print(f"[OK] Database connected, {count} records found")
    except Exception as e:
        print(f"[WARN] Database connection failed: {e}")
    
    print("=" * 50)
    print("Log Download Tool - Backend API Started (with remote truncate)")
    print("Access: http://localhost:5009")
    print("=" * 50)
    app.run(host='0.0.0.0', port=5009, debug=False, threaded=True)
