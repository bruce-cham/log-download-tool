# Linux 日志自动下载工具

一个基于 Flask + DuckDB 的 Web 工具，用于批量从远程 Linux 服务器下载/截取日志文件。支持跳板机、多线程并发、关键字过滤、远程截取（行号/字符串匹配）以及在线预览日志内容。

---

## 📋 目录

- [特性](#特性)
- [技术栈](#技术栈)
- [安装与配置](#安装与配置)
- [使用指南](#使用指南)
- [API 接口说明](#api-接口说明)
- [常见问题](#常见问题)
- [目录结构](#目录结构)
- [贡献与反馈](#贡献与反馈)
- [许可证](#许可证)

---

## ✨ 特性

- 🔍 **灵活筛选**：通过项目、环境、应用、组件、POD 等条件过滤服务器列表。
- 🚀 **并发下载**：支持自定义线程数，快速批量获取日志。
- 🔐 **多认证方式**：支持直连或通过跳板机 SSH 连接，可自定义用户名/密码（优先使用），亦支持从 DuckDB 数据库读取默认凭证。
- ✂️ **远程截取**：无需下载完整日志，可在服务端按**行号范围**或**开始字符串**（支持多个，OR 关系）截取，大幅减少传输量。
- 📦 **自动打包**：截取后的文件自动打包为 `tar.gz`，并支持本地解压预览。
- 👁️ **在线预览**：在浏览器中直接查看已下载/解压的日志文件，支持关键字搜索和高亮。
- 📊 **VCOMM 实例查询**：附带独立页面，可查询 VCOMM 表数据，支持 Output 字段模糊搜索。
- 💾 **配置持久化**：前端配置可保存/加载，方便复用。

---

## 🛠 技术栈

- **后端**：Flask, Flask-CORS, Paramiko, DuckDB, Pandas
- **前端**：原生 HTML + CSS + JavaScript (无额外框架)
- **数据库**：DuckDB (预置 `BuildSheet` 和 `VCOMM` 表)

---

## 🚀 安装与配置

### 1. 环境要求

- Python 3.8 或更高版本
- 目标服务器支持 SSH（22 端口）
- （可选）跳板机支持 `direct-tcpip` 通道转发

### 2. 克隆项目到本地

```bash
git clone https://github.com/yourusername/log-download-tool.git
cd log-download-tool
```

确保项目根目录下包含以下文件结构（如没有请手动创建）：

```
.
├── app_backend.py              # 后端主程序
├── templates/
│   ├── index.html              # 主页面
│   └── vcomm_instance.html     # VCOMM 查询页面
├── static/                     # 存放 favicon.png 等静态资源（可空）
├── requirements.txt            # 核心 Python 依赖
├── piplist.txt                 # （仅供参考）当前环境全量包列表
└── downloaded_logs/            # （自动创建）日志下载保存目录
```

### 3. 安装 Python 依赖

> **重要说明**：  
> 项目根目录下的 `piplist.txt` 是您当前 Python 环境的**全量包导出清单**（包含 torch、scrapy、streamlit 等众多包），但本工具**实际运行仅依赖少数几个核心库**，请使用精简的 `requirements.txt` 进行安装，避免引入无关包。

#### 推荐使用虚拟环境（避免污染全局环境）：

**Windows:**
```bash
python -m venv venv
venv\Scripts\activate
```

**Linux/macOS:**
```bash
python3 -m venv venv
source venv/bin/activate
```

#### 安装核心依赖：

```bash
pip install -r requirements.txt
```

`requirements.txt` 内容如下（与 `piplist.txt` 中实际版本对齐）：

```
flask==3.0.0
flask-cors==4.0.0
paramiko==2.8.1
duckdb==1.5.2
pandas==2.2.2
```

> **网络慢怎么办？** 可使用国内镜像源加速安装：
> ```bash
> pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
> ```

### 4. 准备 DuckDB 数据库文件

本工具需要从 DuckDB 中读取服务器清单（`BuildSheet` 表）和 VCOMM 数据（`VCOMM` 表）。

#### 步骤 4.1：确认数据库文件

确保您已有 DuckDB 数据库文件（例如 `mydb.duckdb`）。如果没有，可先创建一个空库，再导入数据。

#### 步骤 4.2：修改数据库路径配置

打开 `app_backend.py`，找到约第 40 行的配置项：

```python
DB_PATH = r"D:\Onstar\OpenShift\mydb.duckdb"  # 替换为您的实际绝对路径
```

> **建议**：使用绝对路径，避免因启动目录不同导致找不到文件。

#### 步骤 4.3：创建数据库表结构

表字段名必须严格匹配，代码中通过 `SELECT` 指定了具体列。

##### **表名：`BuildSheet`**（服务器清单）

至少包含以下列（名称需完全一致）：
- `Project`
- `Env`
- `Application`
- `Component`
- `IP`
- `POD`
- `Instance Name`
- `App OS Username`
- `Weblogic Password`
- `HTTP Port`

**示例建表语句（DuckDB 语法）：**
```sql
CREATE TABLE BuildSheet (
    Project VARCHAR,
    Env VARCHAR,
    Application VARCHAR,
    Component VARCHAR,
    IP VARCHAR,
    POD VARCHAR,
    "Instance Name" VARCHAR,
    "App OS Username" VARCHAR,
    "Weblogic Password" VARCHAR,
    "HTTP Port" VARCHAR
);
```

##### **表名：`VCOMM`**（VCOMM 实例查询）

至少包含以下列：
- `IP`
- `Project/Environment/Application`
- `Component`
- `Status`
- `Output`
- `Error`

**示例建表语句：**
```sql
CREATE TABLE VCOMM (
    IP VARCHAR,
    "Project/Environment/Application" VARCHAR,
    Component VARCHAR,
    Status VARCHAR,
    "Output" VARCHAR,
    "Error" VARCHAR
);
```

#### 步骤 4.4：导入数据

可使用 DuckDB 的 `COPY` 命令或 `INSERT` 语句导入数据，确保表中已有记录。

**示例导入 CSV：**
```sql
COPY BuildSheet FROM 'servers.csv' (HEADER, DELIMITER ',');
COPY VCOMM FROM 'vcomm_data.csv' (HEADER, DELIMITER ',');
```

### 5. 检查静态资源目录（可选）

- 若您有网站图标，请将 `favicon.png` 和 `apple-touch-icon.png` 放入 `static/` 文件夹。
- 若没有，不影响功能，浏览器会显示默认图标。

### 6. 启动服务

在项目根目录下执行：

```bash
python app_backend.py
```

启动成功后，控制台会输出：

```
[OK] Database connected, xxx records found
==================================================
Log Download Tool - Backend API Started (with remote truncate)
Access: http://localhost:5009
==================================================
```

### 7. 访问与使用

打开浏览器，访问 `http://localhost:5009` 即可进入主界面。

- **首次使用**：建议先点击"📂 加载配置"加载默认设置，或手动填写筛选条件。
- **下载日志**：勾选服务器 → 配置查询参数 → 点击"🚀 开始下载日志"。
- **查看 VCOMM**：点击页面顶部导航栏的"📊 查看 VCOMM 实例"。

---

## 📖 使用指南

### 主界面（日志下载）

#### 1. 筛选服务器

- 通过"项目/环境/应用/组件/POD/实例类型"下拉框或输入框过滤。
- 点击"清除筛选"恢复全部列表。
- 勾选需要操作的服务器（支持"全选/取消全选"）。

#### 2. 配置查询参数

| 参数 | 说明 | 示例 |
|------|------|------|
| **日志目录** | 多个目录用英文逗号分隔 | `/onstarlog,/var/log` |
| **最近 N 天** | 只查找 N 天内修改过的文件 | `7` |
| **关键字** | 多个关键字用逗号分隔 | `ERROR,Exception` |
| **匹配模式** | `OR`（任一匹配）或 `AND`（全部匹配） | `OR` |
| **截取方式** | `不截取` / `按行号范围` / `按开始字符串` | `按行号范围` |
| **起始行/结束行** | 截取指定行号范围 | `100` - `500` |
| **开始字符串** | 多个字符串逗号分隔，OR 关系 | `ERROR,FATAL` |

> **截取方式说明**：
> - **不截取**：下载完整日志文件（可能很大，传输较慢）
> - **按行号范围**：只保留指定起始行至结束行的内容
> - **按开始字符串**：只保留包含指定字符串的行（支持多个字符串，逗号分隔，OR 关系）

#### 3. 登录凭证（可选）

- 若留空，则使用数据库中 `BuildSheet` 表里的 `App OS Username` 和 `Weblogic Password`。
- 若填写，则**优先使用**您填写的用户名和密码连接所有目标服务器。

#### 4. 跳板机配置（可选）

- 若目标服务器无法直连，填写跳板机 IP、用户名、密码。
- 工具会自动建立 SSH 隧道，通过跳板机连接目标服务器。

#### 5. 开始任务

- 点击"🚀 开始下载日志"，右侧"执行日志"区域会实时显示每台服务器的连接和打包状态。
- 进度条显示整体完成百分比。
- 任务完成后，"执行结果"表格中会列出每台服务器的状态、下载链接和"预览"按钮。

#### 6. 下载与预览

- 点击"📦 下载"可保存 `tar.gz` 压缩包到本地。
- 点击"👁️ 预览"可在弹窗中查看解压后的日志文件，支持搜索关键字和高亮显示。

### VCOMM 实例查询页面

1. 点击顶部导航"📊 查看 VCOMM 实例"进入。
2. 在"Output 模糊搜索"框中输入关键字（如 `ERROR`），点击"🔍 查询"。
3. 可进一步通过"组件筛选"和"状态筛选"缩小范围。
4. 结果表格展示 IP、项目/环境/应用、组件、状态、Output 和 Error 字段。

---

## 🔌 API 接口说明

供二次开发参考：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/servers` | GET | 获取服务器列表，支持 `project/env/application/component/pod/instanceType/limit/offset` 过滤参数 |
| `/api/filter_options` | GET | 获取所有可用的项目、环境、应用、实例类型列表，用于填充下拉框 |
| `/api/start` | POST | 启动下载任务。Body 需包含 `servers` 列表及各配置项 |
| `/api/status` | GET | 获取当前任务进度（`running/progress/total/results`） |
| `/api/logstream` | GET | SSE 长连接，实时推送执行日志 |
| `/api/config` | GET/POST | 获取或保存前端配置（存储于 `web_config_v2.json`） |
| `/api/preview_local` | POST | 预览本地解压后的文件内容，支持分页和关键字搜索 |
| `/api/list_extracted_files` | POST | 获取指定 `tar.gz` 解压后的文件列表 |
| `/api/check_weblogic` | POST | 批量检查 WebLogic 进程状态 |
| `/api/check_pidpy` | POST | 批量执行远程 PID.py 脚本 |
| `/api/check_vcomm_version` | POST | 批量查询 VCOMM 版本信息（jar 包软链接） |
| `/api/vcomm/query` | POST | 查询 VCOMM 表数据（Output 字段模糊匹配） |
| `/api/vcomm/filter_options` | GET | 获取 VCOMM 表的组件和状态筛选选项 |

> 详细请求/响应 JSON 格式请参考 `app_backend.py` 中各路由的代码实现。

---

## ❓ 常见问题

### Q1：启动时提示 `Database connection failed`

**解决方法**：
- 检查 `DB_PATH` 路径是否正确
- 确认 DuckDB 文件是否存在且未被其他进程占用
- 检查文件读写权限

### Q2：连接服务器失败 / 认证被拒

**解决方法**：
- 优先检查填写的用户名/密码是否正确
- 若使用数据库默认凭证，确认 `BuildSheet` 表中对应记录的 `App OS Username` 和 `Weblogic Password` 字段非空且准确
- 检查目标服务器 SSH 服务是否正常运行（`systemctl status sshd`）
- 确认网络连通性（`ping <目标IP>`）

### Q3：跳板机无法连接

**解决方法**：
- 确认跳板机地址、用户名、密码正确
- 检查跳板机是否允许转发（`AllowTcpForwarding yes` 在 `/etc/ssh/sshd_config` 中）
- 确认跳板机 SSH 服务正常运行

### Q4：预览文件时中文乱码

**解决方法**：
- 后端使用 `utf-8` 和 `errors='ignore'` 尝试解码
- 若文件编码非 UTF-8，部分字符可能显示为乱码，属正常现象
- 可尝试下载原始文件后用专业编辑器（如 Notepad++）查看

### Q5：`piplist.txt` 有什么用？

**回答**：它是您当前环境的完整包列表快照，方便后续环境复原时参考。本工具**不需要**也不建议安装其中的全部包，仅依赖 `requirements.txt` 中的核心库即可。

### Q6：下载速度慢怎么办？

**建议**：
- 减少线程数（在界面中调整"线程数"参数）
- 使用"截取"功能减少传输数据量
- 检查网络带宽和延迟
- 考虑将工具部署在与目标服务器同一内网

### Q7：日志文件找不到

**可能原因**：
- 日志目录路径不正确
- 文件修改时间超出"最近 N 天"范围
- 文件权限不足
- 关键字过滤条件过严

---

## 📁 目录结构

```
.
├── app_backend.py              # Flask 后端主程序（所有 API 和核心逻辑）
├── templates/
│   ├── index.html              # 主界面 HTML
│   └── vcomm_instance.html     # VCOMM 查询界面 HTML
├── static/                     # 存放 favicon.png 等静态资源
├── requirements.txt            # 核心依赖清单（精简）
├── piplist.txt                 # 全量环境包列表（仅供参考）
├── web_config_v2.json          # 前端配置缓存（运行后自动生成）
├── downloaded_logs/            # 默认下载保存目录（自动创建）
└── README.md                   # 本文档
```

---

## 🤝 贡献与反馈

欢迎提交 Issue 和 Pull Request。

**反馈建议**：
- 请提供 `app_backend.py` 控制台输出的完整错误日志
- 描述复现步骤和预期行为
- 附上相关截图（如有）

---

## 📄 许可证

[MIT](LICENSE)

---

> **最后更新**：2026年9月  
> **版本**：v2.0  
> **维护者**：运维团队
