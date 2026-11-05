<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT License">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/Version-3.6.0-brightgreen.svg" alt="v3.6.0">
</p>

<h1 align="center">发票夹子 🧾</h1>

<p align="center">
  <strong>轻量级发票识别与管理工具</strong><br>
  本地优先 · 三级引擎降级 · 标签系统 · 智能凑票 · 一键导出<br>
  支持 SQLite / PostgreSQL · Web UI + MCP 双入口
</p>

---

## ✨ 功能概览

| 功能 | 说明 |
|------|------|
| 🖥️ **Web UI** | FastAPI 可视化界面，拖拽上传、表格编辑、一键导出 |
| ⌨️ **MCP 接口** | AI Agent 接口（MCP 协议），可调用扫描、查询、编辑、导出全流程 |
| 🔍 **三级识别引擎** | 百度 OCR → 大模型视觉 → 本地 OCR，自动降级 |
| 📁 **智能归档** | `年份/日期_金额_销售方_发票号.pdf` 自动整理 |
| 🏷️ **标签系统** | 自定义彩色标签，快速分类和筛选发票 |
| 🎯 **智能凑票** | 给定目标金额，自动找出最优发票组合 |
| 📊 **灵活导出** | 选择指定发票，合并PDF/源文件ZIP/附件打包 |
| ↩️ **报销单可修正** | 财务退回后移出发票、重算金额、重新生成文件，已报销发票可退回 |
| 🗄️ **双数据库** | SQLite（默认）或 PostgreSQL，配置即切 |

---

## 🚀 快速开始

### 安装

```bash
# 从 Wheel 包安装
pip install dist/invoice_manager-3.6.0-py3-none-any.whl
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

MCP Server 暴露以下 23 个工具，覆盖发票管理全流程：

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
pip install dist/invoice_manager-3.6.0-py3-none-any.whl
```

安装后可用命令：

| 命令 | 说明 |
|------|------|
| `invoice-manager-web` | 启动 Web UI（含 MCP 接口） |

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
| 🗂️ 报销记录 | `/reimbursements` | 报销单列表，按状态筛选 |
| ↩️ 报销单详情 | `/reimbursements/{id}` | 移出/补充发票、改状态与备注、重新生成文件、处理记录 |
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
│   ├── web.py                    # FastAPI Web UI (v3.6.0)
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
│   │   ├── reimbursements.html   # 报销记录列表
│   │   ├── reimbursement.html    # 报销单详情与修正
│   │   └── settings.html         # 归属与标签设置
│   └── engines/                  # 识别引擎
│       ├── base.py               # 抽象基类
│       ├── _utils.py             # 共享工具函数
│       ├── text_ocr.py           # 本地 OCR
│       ├── baidu_ocr.py          # 百度 OCR
│       └── llm_vision.py         # 大模型视觉
└── dist/
    └── invoice_manager-3.6.0-py3-none-any.whl
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

## 🎯 v3.6.0 新增功能

| 功能 | 说明 |
|------|------|
| 📄 **发票库分页** | 列表改为服务端分页与排序，可选 20/50/100/200 条每页；排序与筛选可同时生效 |
| 🔢 **金额区间筛选** | 金额输入支持 `100`、`>=100`、`<=100`、`100-200` 四种写法，搜索框回归纯文本检索，不再兼做金额匹配 |
| 🗑️ **失败记录可清理** | 识别失败项可单条删除（连同原文件），启动时自动清理超过 30 天的记录，并显示占用体积 |
| 🛡️ **导入异常隔离** | 单个文件识别异常不再导致整批上传 500，失败项照常进入失败记录 |
| 🤖 **MCP 导出对齐** | MCP 导出改用 `storage.export_dir`，并像 Web 一样记录报销单与导出模式 |
| ⚡ **初始化去重** | 同一进程内重复初始化不再重跑建表 DDL；识别引擎按配置缓存复用 |
| 🧹 **冗余清理** | 删除 10 组无引用样式、6 个无调用函数、多处重复实现（发票查询、导出编排、导出目录），README 移除已不存在的 CLI 章节 |
| 🔔 **上传进度可见** | 改成逐文件上传：显示「正在处理 3/8：xxx.pdf」与每张结果，失败当场能看到原因；刷新不会重传 |
| 🔁 **导出不再重复生成** | 导出完成后跳转到结果页（`/export?batch=N`），双击「开始导出」或刷新都不会多建一张单据 |
| 🎨 **界面可达性** | 四种发票状态四种颜色；51 处 label 补上 `for` 关联；移动端顶栏补全五个页面的导航；底部操作条不再压住分页 |
| 🔍 **核对辅助** | 发票详情顶部列出需要人工确认的点：金额与税额能否对上、号码位数、日期合理区间、必填缺失；点问题直接跳到对应字段 |
| 📱 **手机端卡片化** | 发票库与报销单明细在 760px 以下改为卡片列表，不再横向滚动 |
| ✅ **自检脚本** | `uv run python tools/selfcheck.py`，零依赖覆盖字段检查、金额解析、按分取整与报销单合计 |

---

## 🎯 v3.5.0 新增功能

| 功能 | 说明 |
|------|------|
| ↩️ **报销单可修正** | 新增报销记录页：财务退回后勾选报不了的发票移出报销单，合计金额自动重算 |
| 🔓 **已报销发票可退回** | 「已报销」不再是单行道：从报销单移出时可选择恢复为可报销或标记为已排除，切换在事务内完成 |
| ➕ **补充发票** | 已提交的报销单可以直接补入新的可报销发票，批次为「已报销」时新票自动同步状态 |
| 🔄 **重新生成文件** | 明细变动后一键按当前发票名单重新导出 Excel / 合并 PDF / 源文件 ZIP，沿用原导出模式 |
| 🗒️ **备注与处理记录** | 报销单可记备注（如财务反馈），每次移出、补充、改状态、重新生成都留下时间线 |
| 🧹 **导出健壮性** | 修复未归档发票被当成目录打包进 ZIP、空压缩包残留、`mark_reported=0` 仍被当成勾选的问题 |

---

## 🎯 v3.4.1 新增功能

| 功能 | 说明 |
|------|------|
| 🎯 **智能凑票必选发票** | 凑票时支持手动指定「必须包含」的发票，每套候选方案都会强制包含这些发票并打 ★ 标记 |
| 🎨 **必选发票动态列表 UI** | 凑票页新增「添加 / 批量」按钮的动态行列表，支持单行输入或批量粘贴发票号码 |
| 🔗 **发票库一键选为必选** | 发票库批量工具栏新增「选为必选发票」按钮，勾选后跳转凑票页并自动填入号码 |
| 🧮 **必选发票凑票算法** | 新增 `find_multiple_candidates_with_required` 函数，先扣除必选金额再用剩余池凑剩余目标 |

---

## 🎯 v3.4.0 新增功能

| 功能 | 说明 |
|------|------|
| 🤖 **MCP 文档完善** | 补充 MCP AI Agent 接口完整文档（启动方式、客户端接入、23 个工具清单） |
| ✅ **导出标记已报销** | 导出时可勾选「标记为已报销」，已报销发票不再参与下次凑票 |
| 🐛 **凑票下载 404 修复** | 修复智能凑票导出源文件 ZIP 返回 404 的问题 |
| 🎨 **工作台 UI 重构** | 统一收件箱、发票库、报销单和设置的应用壳层与详情页 |
| 🧹 **移除旧入口** | 收敛为 `/inbox`、`/list`、`/export`、`/settings` 四个主工作区 |
| 🔎 **金额筛选** | 发票库支持按价税合计精确筛选，也可直接在搜索框输入金额 |

---

## 📄 开源协议

MIT License
