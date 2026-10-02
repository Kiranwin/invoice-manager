# AGENTS.md — Bug 修复标准工作流

本文件定义本仓库的 bug 修复标准流程，供 AI Agent / 开发者执行修复时遵循。
流程分四步：**修 Bug → 验证 → 打包 → 提交并推送 Git**。

---

## 0. 前置检查

修复开始前，先确认工作区状态，避免把无关改动混入本次提交：

```bash
git status --short
git branch --show-current
```

- 如存在大量与本次修复无关的未跟踪文件（临时产物、工具目录、下载数据等），
  先将其移出工作区或加入 `.gitignore`，**不要** `git add .`。
- 所有新写入的文档/文件，编辑前先确认是否被忽略：
  ```bash
  git check-ignore -v <file>   # 有输出 = 被忽略，需先处理忽略规则
  ```

---

## 1. 修 Bug

### 1.1 定位

1. 复现问题，明确触发路径与预期行为。
2. 在代码中定位根因（路由、模板、数据层等），先用 `Grep` / `Glob` 搜索关键字符串。
3. 读取相关文件完整上下文后再修改。

### 1.2 修改原则

- **最小改动**：只改与 bug 直接相关的代码，不顺手重构无关部分。
- **保持风格**：遵循现有缩进、命名、编码约定（本项目 UTF-8、`pathlib.Path`、`tempfile`）。
- **优先复用**：已有工具函数/正则/常量直接复用，不重复造轮子。
- **不引入新依赖**：除非必要，不新增第三方包。

### 1.3 白名单/正则类修改

涉及下载白名单、文件名校验等正则时，修改后必须用真实生成的文件名样本做匹配验证：

```bash
python -c "import re; p=r'<正则>'; cases=['样例1','样例2']; [print(c, bool(re.match(p,c))) for c in cases]"
```

---

## 2. 验证

本项目**无自动化测试套件**，验证以「手工 + 最小可复现脚本」为主。

### 2.1 静态校验

```bash
# 语法 / 导入检查
uv run python -c "import invoice_clipper.web"
```

### 2.2 功能验证

1. **启动服务**：
   ```bash
   uv run python -m invoice_clipper.__run__ --debug
   ```
2. 浏览器打开 `http://localhost:8000`，按 bug 复现路径操作，确认问题消失且未引入回归。
3. 对涉及文件下载/路由的修复，直接在浏览器访问对应 URL，确认 HTTP 状态码与返回内容正确。

### 2.3 正则 / 逻辑单元验证

对纯逻辑修改（如正则、金额计算、匹配算法），用 Python 单行脚本跑边界用例，
覆盖正常输入、空输入、异常输入三类。

---

## 3. 打包

本项目使用 `hatchling` 构建，产物为 Wheel，输出到 `dist/`。

### 3.1 构建

```bash
# 安装构建工具（首次）
uv sync --extra dev

# 构建 Wheel + sdist
uv build
```

产物：`dist/invoice_manager-<version>-py3-none-any.whl`

### 3.2 版本号

修复发布前按需在 `pyproject.toml` 的 `[project].version` 递增（语义化版本）。
若本次只是内部修复、不发版，可不改版本号。

### 3.3 构建校验

```bash
# 确认 dist 下生成了 wheel
ls dist/
```

---

## 4. 提交并推送 Git

### 4.1 确认变更范围

```bash
git status --short
git diff --stat
```

仅暂存**本次修复涉及的文件**，禁止 `git add .`：

```bash
git add invoice_clipper/web.py AGENTS.md
```

### 4.2 行尾处理（Windows）

若 `git add` 时出现 `LF will be replaced by CRLF` 警告：

- 优先在仓库根目录添加/检查 `.gitattributes`，统一文本文件行尾为 LF：
  ```
  *.py text eol=lf
  *.md text eol=lf
  *.yaml text eol=lf
  ```
- 仅对本次修改文件做行尾统一，不引入大范围格式化 diff。

### 4.3 提交

commit message 格式：`fix(模块): 简述问题与修复`，正文说明根因和影响范围。

PowerShell 下**不要**用 bash heredoc，也**不要**用 `Out-File -Encoding utf8`
（会写入 BOM，导致 commit message 开头乱码）。使用临时文件 + `-F`，以无 BOM UTF-8 写入：

```powershell
$msg = @'
fix(web): 修复智能凑票下载源文件 ZIP 返回 404

下载路由白名单正则只放行 报销明细/报销发票/报销附件 前缀，
但源文件 ZIP 实际文件名前缀为 源文件_，附件 ZIP 为 发票附件_，
导致匹配失败返回 404。扩展正则白名单覆盖这两个前缀。
'@
[System.IO.File]::WriteAllText("$PWD\.git\COMMIT_MSG", $msg, [System.Text.UTF8Encoding]::new($false))
git commit -F .git\COMMIT_MSG
Remove-Item .git\COMMIT_MSG
```

### 4.4 推送

```bash
git push origin main
```

推送后确认远程已更新，并核对提交内容与本地一致。

---

## 5. 版本发布流程

当需要发布新版本（如 3.3.2 → 3.3.3）时，按以下步骤执行。

### 5.1 全局版本号更新

版本号散落在多处，**必须全部更新**，漏改会导致前端/构建显示旧版本：

```bash
# 先搜索当前版本号，确认所有引用点
grep -rn "3\.3\.2" invoice_clipper/ pyproject.toml README.md
```

需更新的文件清单：

| 文件 | 位置 | 示例 |
|------|------|------|
| `pyproject.toml` | `[project].version` | `version = "3.3.3"` |
| `invoice_clipper/__init__.py` | 模块 docstring | `发票夹子核心模块 - v3.3.3` |
| `invoice_clipper/__run__.py` | 启动入口 docstring | `启动入口（v3.3.3）` |
| `invoice_clipper/web.py` | 文件 docstring | `FastAPI + Jinja2 (v3.3.3)` |
| `invoice_clipper/web.py` | `FastAPI(version=...)` | `version="3.3.3"` |
| `invoice_clipper/web.py` | 启动打印 | `发票夹子 v3.3.3 正在启动` |
| `invoice_clipper/config.example.yaml` | 文件头注释 | `发票夹子 v3.3.3 · 配置文件模板` |
| `invoice_clipper/templates/base.html` | 导航栏版本号 | `v3.3.3` |
| `README.md` | badge | `Version-3.3.3` |
| `README.md` | pip install 命令（2 处） | `invoice_manager-3.3.3-py3-none-any.whl` |
| `README.md` | 项目结构 web.py 注释 | `FastAPI Web UI (v3.3.3)` |
| `README.md` | 项目结构 dist 文件名 | `invoice_manager-3.3.3-...whl` |
| `README.md` | 版本更新日志章节标题 | `v3.3.3 新增功能` |

更新后再次 grep 确认无遗漏：
```bash
grep -rn "3\.3\.2" invoice_clipper/ pyproject.toml README.md  # 应无输出
```

### 5.2 打包

```bash
uv build
# 确认产物
ls dist/
# → invoice_manager-<新版本>-py3-none-any.whl
# → invoice_manager-<新版本>.tar.gz
```

### 5.3 提交并打 Tag

```bash
git add -A  # 版本号更新涉及多文件，可全量暂存
git commit -m "release v<新版本>: <简述变更>"
git tag v<新版本>
git push origin main
git push origin v<新版本>
```

### 5.4 创建 GitHub Release

使用 gh CLI（路径 `C:\Program Files\GitHub CLI\gh.exe`，需 `gh auth login` 认证）：

```powershell
$env:Path = "C:\Program Files\GitHub CLI;" + $env:Path

# 准备 Release Notes（无 BOM UTF-8）
$notes = @'
## v<新版本> 更新内容

### 新功能1
描述...

### 新功能2
描述...
'@
[System.IO.File]::WriteAllText("$PWD\release_notes.md", $notes, [System.Text.UTF8Encoding]::new($false))

# 创建 Release 并上传 wheel + sdist
gh release create v<新版本> --title "v<新版本>" --notes-file release_notes.md `
    "dist\invoice_manager-<新版本>-py3-none-any.whl" `
    "dist\invoice_manager-<新版本>.tar.gz"

Remove-Item release_notes.md
```

若需更新已有 Release 的资产（如补提交后重新构建）：
```powershell
uv build
gh release upload v<新版本> "dist\invoice_manager-<新版本>-py3-none-any.whl" "dist\invoice_manager-<新版本>.tar.gz" --clobber
```

### 5.5 远程仓库迁移

如需更换远程地址：
```bash
git remote set-url origin https://github.com/<user>/<repo>.git
git remote -v  # 确认
```

---

## 快速检查清单

### Bug 修复
- [ ] 已复现 bug 并定位根因
- [ ] 修改最小化，未夹带无关改动
- [ ] 已用脚本/浏览器验证修复生效、无回归
- [ ] 已构建 `uv build` 成功（如需发版）
- [ ] `git status` 确认暂存文件仅为本次修复相关
- [ ] 行尾无 CRLF 噪声
- [ ] commit message 清晰说明问题与修复
- [ ] `git push origin main` 成功

### 版本发布
- [ ] 全局版本号已更新（grep 确认无遗漏）
- [ ] 前端导航栏显示新版本号
- [ ] `uv build` 成功生成 wheel + sdist
- [ ] commit + tag 已推送
- [ ] GitHub Release 已创建并附带 wheel
- [ ] Release 页面可正常下载
