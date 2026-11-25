# MemoryS

**本地优先、人工治理的 AI Agent 记忆系统。**  
基于纯 Markdown（原生兼容 Obsidian）+ Git + Stdio MCP + `<50ms` 本地召回 Hook，**Python 运行时零第三方依赖**。

[English](README.md) | 简体中文

---

## 为什么做 MemoryS？

大多数 AI 记忆工具都在追求「全自动记录」。用久了就会遇到三个痛点：AI 偷偷记下臆测、旧规则失效了还在污染新对话、A 项目的业务口径串到 B 项目。

MemoryS 选择一条更克制的路线：**Agent 只能提案，人类确认发布。**

```text
  Claude Code / Codex / Cursor / 任意 MCP 客户端
        │                                 ▲
        │ memory_propose（仅写待审草稿）    │ <50ms 本地 Hook & 只读 MCP
        ▼                                 │ (memory_search / read / trace)
  ┌─────────────────────┐        ┌──────────────────────────┐
  │ SQLite 待审队列      │        │  Obsidian Vault + Git    │
  │（内置密钥与禁记拦截） │───────▶│  （唯一正式真相源）       │
  └─────────────────────┘ 人类确认│  • 标准 YAML Frontmatter  │
                          发布闸门│  • supersedes / as_of    │
                                 │  • SOURCES.md 原话溯源    │
                                 └──────────────────────────┘
```

### 核心特点

| 能力 | MemoryS | 自动采集插件 | 托管向量记忆 |
|---|---|---|---|
| 人工发布 | MCP 工具读取正式卡片、提交草稿。用户审阅后发布，Git 记录变更 | 自动写入 | 自动写入 |
| 按日期查询 | 支持生效日、截止日和卡片替代链 | 较少支持 | 支持有限 |
| 原话溯源 | 每条规则都可以关联到 `SOURCES.md` 中的原话 | 通常仅保留摘要 | 返回文本片段 |
| 本地召回 | Hook 仅使用 Python 标准库 | 常需调用模型 | 依赖云端或本地数据库 |
| 跨客户端使用 | Claude Code、Codex、Cursor 等可共用一个 Vault | 通常限于单个客户端 | 取决于 SDK |
| 审阅控制台 | macOS 原生应用和跨平台本地 Web 界面 | CLI 或 Web | 云端管理界面 |

## 控制台预览

**记忆库** — 浏览现行与历史记忆、替代链和 `SOURCES.md` 来源原话：

![MemoryS 记忆库](docs/screenshot-library.png)

**审阅与发布** — 在写入 Vault 并提交 Git 前，逐项对比 Agent 提交的改前/改后草稿：

![MemoryS 审阅与发布](docs/screenshot-review.png)

---

## 2 分钟快速上手

安装 **Python 3.10+** 与 **Git**。源码和 Vault 请放在两个独立目录中。各平台默认在 `~/memorys-vault` 建库。

### 1. 安装并初始化演示 Vault

```bash
git clone https://github.com/52Rain/MemoryS.git
cd MemoryS
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install .

# 创建自带时态规则示例的 Git + Obsidian Vault
mem init --demo
```

如果系统的 Python 3 启动命令是 `python3` 或 `py -3`，请用它创建虚拟环境。普通安装会构建 wheel，并将运行时、演示库和 Web 控制台装入环境。已有 wheel 时，可将文件路径传给 `python -m pip install`。开发时可用 `python -m pip install -e .` 直接使用工作树里的代码，也可以在源码目录运行 `python runtime/memory_cli.py ...`。

### 2. 体验历史日期口径回溯与溯源

```bash
# 查询 2026-05-01 当时生效的规则 -> 命中 v1（7 天宽限期）
mem search --project acme-billing --as-of 2026-05-01

# 查询 2026-07-01 生效的规则 -> 命中 v2（企业版延长至 14 天）
mem search --project acme-billing --as-of 2026-07-01

# 追溯 v2 的来源原话
mem trace d-acme-billing-grace-v2
```

### 3. 一键接入 AI 客户端

先预览配置，确认无误后加 `--apply` 自动写入（写入前自动生成 `.bak` 备份）：

```bash
mem connect claude            # 预览配置片段
mem connect claude --apply    # 写入 ~/.claude.json 与 ~/.claude/settings.json

mem connect codex --apply     # 写入 ~/.codex/config.toml 与 ~/.codex/hooks.json
mem connect cursor --apply    # 写入 ~/.cursor/mcp.json
```

### 4. 打开审阅与记忆库控制台

**跨平台本地 Web 控制台（Linux / Windows / macOS）：**

```bash
mem ui
```

wheel 已包含 HTML、JavaScript 和 CSS。要试用另一份本地构建，可运行 `mem ui --ui-dir /path/to/dist`。Web 版可在设置中输入 Vault 路径。用 Obsidian 打开卡片需要系统已注册 Obsidian 链接，Linux 通过 `xdg-open` 打开外部应用。

**macOS 原生应用**需要 macOS 13+、Swift 命令行工具、可用 `python3` 启动的 Python 3.10+，以及 Node.js 22+（最低 22.12）。WKWebView 外壳使用系统钥匙串和目录选择器。`App.swift` 共 235 行，单独的图标生成脚本有 15 行。Linux 和 Windows 使用 Web 控制台。

```bash
cd desktop-app
npm ci
npm run package
open build.noindex/MemoryS.app
```

---

## Vault、召回与审阅

### Vault 目录

```text
~/memorys-vault/
├── START.md                        # Vault 入口
├── PROJECTS.md                     # 自动生成的项目索引
├── SOURCES.md                      # JSONL 格式的来源原话
├── notes/
│   ├── preferences/                # 常驻个人偏好
│   ├── methods/                    # 跨项目通用方法
│   └── domain/                     # 领域知识
└── projects/
    └── acme-billing/
        ├── START.md                # 项目别名和本地工作目录
        ├── handoff.md              # 项目交接信息
        └── d-acme-billing-*.md     # 有版本记录的项目决策
```

已有中文 Vault 可继续使用 `项目索引.md` 和 `来源记录.md`。系统也识别历史草稿状态，包括「待确认」「已发布」「已忽略」和「受限」。

### 本地召回

每次提交提示词时，`UserPromptSubmit` Hook 在本地读取 Vault。它先按字数预算加载 `shared/preferences` 中的现行偏好，再用当前工作目录、提示词中的项目名或别名匹配项目。对于 `shared/methods` 和 `shared/domain` 中的卡片，Hook 按关键词相关程度选取。

### MCP 工具

- `memory_search` 按关键词、项目、作用域或历史日期搜索。
- `memory_read` 读取正文、元数据和卡片替代关系。
- `memory_trace` 根据来源引用查找 `SOURCES.md` 中的原话。
- `memory_propose` 将草稿提交到 SQLite 待审队列，支持生效日期、替代目标、引文、触发词和客户端名。提交草稿和发布前都会检查已知密钥格式和禁记指令。

**MCP 工具层只读隔离**适用于正式 Vault 内容。Agent 可通过 MCP 提交草稿，用户在控制台审阅后发布。同一操作系统账号下的程序仍保留原有文件权限。

### 可选的 AI 冲突检查

可在控制台配置兼容 OpenAI Chat Completions 的端点。发起检查时，该端点会收到草稿和同一作用域的现行卡片。模型意见供审阅参考，发布可独立进行。

## 配置与环境变量

MemoryS 按以下顺序查找 Vault。

1. 全局参数 `mem --vault <path>`。
2. 环境变量 `MEMORYS_VAULT`。
3. MemoryS 配置文件中保存的 `vault_path`。
4. 设置了 `MEMORYS_PROJECT_ROOT` 时，检查 `$MEMORYS_PROJECT_ROOT/MemoryS` 是否为已有 Vault。
5. 依次检查 `~/memorys-vault` 和旧目录 `~/MemoryS`。在源码环境中，只读命令还可以使用 `examples/demo-vault`。

| 配置 | 用途 |
|---|---|
| `MEMORYS_CONFIG_DIR` | 指定 `config.json` 的目录。macOS/Linux 默认使用 `$XDG_CONFIG_HOME/memorys` 或 `~/.config/memorys`，Windows 默认使用 `%APPDATA%\MemoryS`。 |
| `MEMORYS_APP_DATA` | 指定 SQLite 和工作区锁的目录。macOS 默认使用 `~/Library/Application Support/MemoryS`，Linux 使用 `$XDG_DATA_HOME/memorys` 或 `~/.local/share/memorys`，Windows 使用 `%LOCALAPPDATA%\MemoryS`。 |
| `MEMORYS_UI_DIR` | 指定含 `app.js` 和 `app.css` 的构建目录。`mem ui --ui-dir` 优先。 |
| `MEMORYS_API_URL`、`MEMORYS_API_KEY` | Web 版的可选模型端点和密钥。环境密钥仅用于该基地址。未设置端点变量时，使用已保存的模型地址。 |
| `CODEX_HOME` | 替换 `mem connect codex --apply` 默认使用的 `~/.codex` 目录。 |

## 开发与测试

在源码目录运行 Python 测试。构建前端需要 **Node.js 22+（最低 22.12）** 和 npm。

```bash
python -B -m unittest discover -v -s tests
python -B tests/check_install.py
node tests/check_markdown.mjs
cd desktop-app
npm ci
npm run build
```

发布新版时，把新构建的 `dist/app.js` 和 `dist/app.css` 复制到 `runtime/ui/`。wheel 包含这两个由 Git 跟踪的文件，CI 会与重新构建的产物比对。

有关贡献流程与安全边界，见[贡献指南](CONTRIBUTING.md)和[安全说明](SECURITY.md)。

## 开源协议

[MIT](LICENSE)
