<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT License">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/Version-3.4.0-brightgreen.svg" alt="v3.4.0">
</p>

<h1 align="center">发票夹子 🧾</h1>

<p align="center">
  <strong>轻量级发票识别与管理工具</strong><br>
  本地优先 · 三级引擎降级 · 标签系统 · 智能凑票 · 一键导出<br>
  支持 SQLite / PostgreSQL · Web UI + CLI 双入口
</p>

---

## ✨ 功能概览

| 功能 | 说明 |
|------|------|
| 🖥️ **Web UI** | FastAPI 可视化界面，拖拽上传、表格编辑、一键导出 |
| ⌨️ **CLI + MCP** | 命令行批量处理 + AI Agent 接口 (MCP协议) |
| 🔍 **三级识别引擎** | 百度 OCR → 大模型视觉 → 本地 OCR，自动降级 |
| 📁 **智能归档** | `年份/日期_金额_销售方_发票号.pdf` 自动整理 |
| 🏷️ **标签系统** | 自定义彩色标签，快速分类和筛选发票 |
| 🎯 **智能凑票** | 给定目标金额，自动找出最优发票组合 |
| 📊 **灵活导出** | 选择指定发票，合并PDF/源文件ZIP/附件打包 |
| 🗄️ **双数据库** | SQLite（默认）或 PostgreSQL，配置即切 |

---

## 🚀 快速开始

### 安装

```bash
# 从 Wheel 包安装
pip install dist/invoice_manager-3.4.0-py3-none-any.whl
```

### 启动 Web UI

```bash
# 正常启动（带进程锁）
uv run python -m invoice_clipper.__run__

# 调试模式（跳过进程锁）
uv run python -m invoice_clipper.__run__ --debug

# 或直接启动（无锁）
uv run python -m invoice_clipper
```

浏览器打开 http://localhost:8000

### CLI 日常使用

```bash
# 扫描监控目录中的发票
uv run python invoice_clipper/__main__.py scan

# 列出所有发票
uv run python invoice_clipper/__main__.py list

# 按条件查询
uv run python invoice_clipper/__main__.py query --from 2025-01-01 --to 2025-12-31 --seller "科技公司"

# 处理单个文件
uv run python invoice_clipper/__main__.py process invoice.pdf

# 标记排除/恢复报销
uv run python invoice_clipper/__main__.py exclude 3
uv run python invoice_clipper/__main__.py include 3

# 导出报销
uv run python invoice_clipper/__main__.py export --from 2025-03 --format both
```

---

## 🤖 MCP AI Agent 接口

本项目内置 MCP (Model Context Protocol) Server，将发票管理能力暴露为 AI Agent 可调用的工具，
兼容 Claude Desktop、Cursor、Trae 等支持 MCP 协议的客户端。

### 启动方式

MCP Server 有两种运行形态，按需选择：

**1. 随 Web UI 一起启动（推荐，默认 HTTP 模式）**

```bash
uv run python -m invoice_clipper.__run__
# MCP 接口挂载在 http://127.0.0.1:8000/mcp
```

Web UI 与 MCP 共享同一进程，配置和数据库自动初始化。

**2. 独立运行 MCP Server**

```bash
# 默认 HTTP（Streamable HTTP），监听 http://127.0.0.1:8100/mcp
uv run python -m invoice_clipper.mcp_server

# 指定传输模式：http | sse | stdio
uv run python -m invoice_clipper.mcp_server --transport stdio
```

传输模式由 `config.yaml` 的 `mcp.transport` 决定，可用 `--transport` 参数覆盖：

```yaml
mcp:
  transport: http   # http (Streamable HTTP, 默认) | sse | stdio
```

### 客户端接入示例

**Claude Desktop / Cursor / Trae 等（stdio 模式）**

在客户端的 MCP 配置中加入：

```json
{
  "mcpServers": {
    "invoice-manager": {
      "command": "uv",
      "args": ["run", "--directory", "<项目路径>", "python", "-m", "invoice_clipper.mcp_server", "--transport", "stdio"]
    }
  }
}
```

**HTTP 模式接入**

启动 Web UI 或独立 MCP Server 后，在客户端填入 URL：
`http://127.0.0.1:8000/mcp`（随 Web UI）或 `http://127.0.0.1:8100/mcp`（独立运行）。

### 可用工具

MCP Server 暴露以下工具，覆盖发票管理全流程：

| 分类 | 工具 | 说明 |
|------|------|------|
| 📥 扫描处理 | `scan_invoices` | 扫描监控目录，批量识别新发票并入库 |
| 📥 扫描处理 | `process_file` | 处理单个发票文件（PDF/OFD/图片），返回识别结果 |
| 📋 查询 | `list_invoices` | 列出全部发票，支持关键词搜索与状态筛选 |
| 📋 查询 | `query_invoices_tool` | 多条件查询（日期/卖家/买家/项目/归属人） |
| 📋 查询 | `get_invoice` | 获取单张发票完整信息（含附件） |
| 📋 查询 | `get_invoice_stats` | 统计：总数、可报销数、总金额、可报销金额 |
| ✏️ 编辑 | `update_invoice_tool` | 更新发票字段（项目/归属人/备注/金额等，按需传入） |
| ✏️ 编辑 | `exclude_invoice` | 标记发票为「不报销」 |
| ✏️ 编辑 | `include_invoice` | 恢复发票为「可报销」 |
| 🗑️ 删除 | `delete_invoice_tool` | 删除发票及其附件文件（不可恢复） |
| 🏷️ 标签 | `list_tags` / `create_tag` / `delete_tag_tool` | 标签的增删查 |
| 🏷️ 标签 | `get_invoice_tags_tool` / `set_invoice_tags_tool` | 查询/设置发票的标签 |
| 📂 归属 | `list_projects` / `create_project` / `delete_project_tool` | 归属项目的增删查 |
| 📂 归属 | `list_persons` / `create_person` / `delete_person_tool` | 归属人的增删查 |
| 📤 导出 | `export_invoices_excel` | 按条件导出 Excel，返回文件路径 |
| 📤 导出 | `export_invoices_pdf` | 按条件导出合并 PDF，返回文件路径 |

所有工具返回 JSON 字符串，含 `success` / `error` / `warning` 字段表示执行结果。
查询、统计、导出类工具返回的数据结构与 Web UI 一致，可参考「Web UI 页面」章节。

---

## 📦 安装方式

### Wheel 包（推荐）

```bash
pip install dist/invoice_manager-3.4.0-py3-none-any.whl
```

安装后可用命令：

| 命令 | 说明 |
|------|------|
| `invoice-manager scan/list/...` | CLI 操作 |
| `invoice-manager-web` | 启动 Web UI |

### 源码开发

```bash
git clone https://github.com/yourname/invoice-manager.git
cd invoice-manager
uv sync
# 首次运行自动生成配置
uv run python -m invoice_clipper
```

---

## ⚙️ 配置

### 配置文件搜索顺序

1. `INVOICE_MANAGER_CONFIG` 环境变量（最高优先级）
2. `{INVOICE_ROOT}/config/config.yaml`
3. `~/.config/invoice-manager/config.yaml`
4. 包相对路径 `config/config.yaml`（开发模式）

首次运行自动复制示例配置并初始化数据库。

报销单默认导出到用户 `Documents/发票夹子/exports`。可在配置文件的 `storage.export_dir` 指定其他目录；收件箱中识别失败的原文件保存在 `storage.base_dir/failed_imports`，供之后重试。

### 识别引擎

```yaml
ocr:
  # 百度 OCR（推荐，准确率高）
  baidu:
    enabled: true
    api_key: "your_api_key"
    secret_key: "your_secret_key"

  # 大模型视觉（OpenAI 兼容）
  llm:
    enabled: false
    api_key: "your_llm_api_key"
    base_url: "https://api.deepseek.com/v1"
    model: "deepseek-chat"

  # 本地 OCR（完全离线，需安装 PaddleOCR）
  text_ocr:
    enabled: false
```

---

## 🖥️ Web UI 页面

| 页面 | 路由 | 功能 |
|------|------|------|
| 📥 收件箱 | `/inbox` | 上传发票、核对待确认项、查看识别失败项并重试 |
| 📋 发票库 | `/list` | 搜索、状态与标签筛选、编辑及批量操作 |
| ✏️ 编辑详情 | `/list/{id}` | 全字段编辑 + 附件管理 + 标签选择 |
| 📊 报销单 | `/export` | 凑票、选票、导出与历史文件下载 |
| ⚙️ 设置 | `/settings` | 管理归属项目、归属人和标签 |

---

## 🗂️ 项目结构

```
invoice-manager/
├── pyproject.toml                # 项目元数据 + 构建配置
├── invoice_clipper/              # 核心 Python 包
│   ├── __init__.py               # load_config + 所有模块导出
│   ├── __main__.py               # pip 入口点
│   ├── __run__.py                # 进程锁启动入口（支持 --debug）
│   ├── web.py                    # FastAPI Web UI (v3.4.0)
│   ├── mcp_server.py             # MCP AI Agent 接口
│   ├── database.py               # 数据库调度层
│   ├── db_backends.py            # SQLite + PostgreSQL 后端
│   ├── processor.py              # 发票处理主流程
│   ├── workflow.py               # 状态、报销单与失败导入业务操作
│   ├── exporter.py               # Excel / PDF / ZIP 导出
│   ├── file_utils.py             # 文件转换与归档
│   ├── matcher.py                # 智能凑票算法
│   ├── config.example.yaml       # 配置模板
│   ├── static/
│   │   └── style.css             # 完整样式系统
│   ├── templates/                # Jinja2 模板
│   │   ├── base.html             # 布局 + 导航
│   │   ├── scan.html             # 收件箱与上传
│   │   ├── list.html             # 发票库
│   │   ├── edit.html             # 编辑 + 附件 + 标签
│   │   ├── export.html           # 凑票、导出与历史记录
│   │   └── settings.html         # 归属与标签设置
│   └── engines/                  # 识别引擎
│       ├── base.py               # 抽象基类
│       ├── _utils.py             # 共享工具函数
│       ├── text_ocr.py           # 本地 OCR
│       ├── baidu_ocr.py          # 百度 OCR
│       └── llm_vision.py         # 大模型视觉
└── dist/
    └── invoice_manager-3.4.0-py3-none-any.whl
```

---

## 🔧 依赖

核心依赖（见 `pyproject.toml`）：

| 包 | 用途 |
|----|------|
| `fastapi` / `uvicorn` / `jinja2` | Web UI 框架 |
| `openpyxl` | Excel 导出 |
| `PyMuPDF` | PDF 处理 + 图片转 PDF |
| `httpx` | API 请求 |
| `mcp` | MCP AI Agent 协议 |
| `python-multipart` | 文件上传 |
| `pyyaml` | 配置解析 |

可选依赖：
- `easyofd` → OFD 电子发票格式支持
- `psycopg2-binary` → PostgreSQL 支持
- `paddlepaddle` + `paddleocr` → 本地离线 OCR

---

## 🎯 v3.4.0 新增功能

| 功能 | 说明 |
|------|------|
| 🤖 **MCP 文档完善** | 补充 MCP AI Agent 接口完整文档（启动方式、客户端接入、18 个工具清单） |
| ✅ **导出标记已报销** | 导出时可勾选「标记为已报销」，已报销发票不再参与下次凑票 |
| 🐛 **凑票下载 404 修复** | 修复智能凑票导出源文件 ZIP 返回 404 的问题 |
| 🎨 **工作台 UI 重构** | 统一收件箱、发票库、报销单和设置的应用壳层与详情页 |
| 🧹 **移除旧入口** | 收敛为 `/inbox`、`/list`、`/export`、`/settings` 四个主工作区 |
| 🔎 **金额筛选** | 发票库支持按价税合计精确筛选，也可直接在搜索框输入金额 |

---

## 📄 开源协议

MIT License
