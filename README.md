# Lumina Draft

单用户、本地运行的 Web 演示文稿创作工具。输入文字需求，由 Director 生成结构化作品说明书；用户编辑并确认后，Page Composer 并行生成固定 **800 × 450（16:9）** 的 HTML 页面。

Python 后端负责 Agent 编排、模型调用、校验、任务执行和持久化；React 前端负责交互、进度展示与页面预览。当前使用 LangGraph 执行 Director 和 Page Composer 两类任务，采用“先确认大纲，再生成页面”的最小闭环。

## 当前能力

- **内容策划**：生成作品 Brief、逐页大纲、全局配色、字体和布局规范。
- **大纲流式展示**：分栏显示供应商返回的 reasoning 文本或摘要与 JSON 正文；支持折叠查看、自动跟随滚动，手动向上滚动时暂停跟随。
- **大纲编辑与确认**：完整 JSON 解析并通过结构校验后进入编辑；确认后才启动页面生成。
- **页面并行生成**：结合全局规范、当前页任务与相邻页摘要生成独立 HTML，单个页面生成任务最多并行 3 页。
- **页面查看**：展示各页状态，提供 HTML 源码与通过校验的页面预览；保留原始 HTML，不转换成可编辑的结构化画布。
- **恢复与重试**：作品、任务和 SSE 事件落库，支持浏览器刷新、断线回放、取消生成与失败重试。
- **模型配置与统计**：两个 Agent 共用 `default` 路由，支持推理力度设置、连接测试、最近调用记录和作品会话 token 用量统计。

初版仅提供文字内容与 HTML/CSS、安全内联 SVG 排版，尚未实现配图、生图、附件、PPTX 导出、Tauri 客户端或多用户服务。

## 使用流程

1. 在“模型与 API”中保存 Responses 接入地址、远程模型 ID 和 API Key，按需设置推理力度。
2. 输入主题、受众、目标和内容要求，点击“生成说明书”，也可使用 `Ctrl/Cmd + Enter`。
3. 查看 Director 的 reasoning 与 JSON 正文流。流文本仅供查看，尚未完成的大纲不能确认。
4. 等待完整说明书校验通过，检查并编辑逐页内容，再点击“确认并生成”。
5. 查看各页的并行生成进度；完成的页面可切换预览与源码。
6. 遇到失败可查看错误并重试。页面阶段重试保留成功页，仅重新执行未成功的页面。

页数由 Director 根据内容自主确定，当前结构契约支持 **1–20 页**。创建接口没有 `page_count` 参数；当前提示词也不把用户文字中的指定页数作为约束或偏好。

## 快速启动

需要 Python 3.11+、Node.js 20+、npm，以及可用的系统凭据存储。以下命令在**仓库根目录**的 PowerShell 中运行。

### 安装依赖

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r backend/requirements.txt
npm --prefix frontend ci
```

### 启动后端

```powershell
.venv/Scripts/python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

### 启动前端

在另一个终端中，从仓库根目录运行：

```powershell
npm --prefix frontend run dev -- --port 5173
```

| 服务 | 地址 |
| --- | --- |
| Web 工作台 | http://127.0.0.1:5173/ |
| 后端接口文档 | http://127.0.0.1:8000/docs |
| 存活检查 | http://127.0.0.1:8000/api/v1/health/live |
| 就绪检查 | http://127.0.0.1:8000/api/v1/health/ready |

前端开发服务器将 `/api` 代理到 `http://127.0.0.1:8000`。请使用以上命令启动 Vite：直接在仓库根目录运行 Vite 且未指定前端根目录，会找不到正确的页面入口和代理配置。

后端使用单 Uvicorn worker，服务绑定回环地址。当前队列和事件通知按本机单进程设计，不适用于多 worker、公网或多用户部署。

## 模型与 API 配置

### 接入协议与路由

在工作台右上角的“模型设置”或侧栏“模型与 API”中配置：

| 字段 | 说明 |
| --- | --- |
| 名称 | 本地展示名称 |
| Base URL | Responses API 根地址，适配器追加 `/responses`；不要填写完整的 Responses 请求地址 |
| 模型 ID | 接入方实际支持的远程模型标识 |
| API Key | 保存到操作系统凭据存储；SQLite 仅保存凭据引用，不向前端返回 Key |

目前仅实现 **OpenAI Responses 格式**的适配器。数据模型保留“业务路由 → 逻辑模型 → 模型绑定 → 接入点/供应商”的关系，初版界面管理一套 `default` 配置，Director 与 Page Composer 共用该路由。配置接口支持启用/停用，当前界面仅展示启用状态。Chat Completions、Anthropic Messages、Gemini 和生图协议尚未接入。

接入地址使用 HTTPS；本机 `localhost` 和 `127.0.0.1` 允许 HTTP。目标接口需支持流式 Responses，Director 请求还会携带 JSON Schema 输出格式。

先保存配置，再点击“测试连接”。测试会发起一次真实请求，固定使用 `reasoning.effort=none` 和 `max_output_tokens=32`；它仅验证基本调用是否成功，不能替代完整的大纲与页面生成验证。

### 推理力度

同一面板的“生成设置”支持：

| 选项 | 请求行为 |
| --- | --- |
| 自动 | 省略 `reasoning` 参数，由供应商决定 |
| None | `reasoning.effort=none` |
| Low | `reasoning.effort=low` |
| High | `reasoning.effort=high`，首次初始化默认值 |
| Max | `reasoning.effort=max` |

推理力度同时应用于大纲和页面生成。新作品会保存模型地址、模型 ID、协议、凭据引用及推理力度的配置快照；修改生成设置仅影响之后新建的作品，已有作品的重试继续使用其快照。具体选项是否可用取决于接入的供应商与模型。

### 首次初始化

也可在第一次启动前通过环境变量或本地文本文件提供初始配置。环境变量优先于文件中的同名配置：

| 环境变量 | 用途 | 默认行为 |
| --- | --- | --- |
| `DATABASE_URL` | SQLAlchemy 数据库连接串 | `sqlite:///./data/lumina.db` |
| `DS_API_FILE` | 初始模型配置文件路径 | `~/Downloads/ds-api.txt` |
| `DS_API_BASE_URL` | 初始 Responses 根地址 | 读取文件；未提供时使用 `https://api.deepseek.com` |
| `DS_API_MODEL` | 初始远程模型 ID | 读取文件；未提供时使用 `deepseek-flash` |
| `DS_API_KEY` | 初始 API Key | 读取文件；未提供则需在界面配置 |
| `APP_NAME` | 后端应用名称 | `Lumina Draft` |

文本文件使用 UTF-8 编码，每行采用 `键: 值` 格式，例如：

```text
base_url: https://api.example.com/v1
model: your-model-id
api_key: your-api-key
```

以上地址和模型 ID 仅为格式示例，请替换为实际接入信息。初始配置只在相关数据库记录不存在时导入；初始化后请使用界面修改，重启不会用环境变量覆盖已保存配置。当前应用不会自动加载 `.env` 文件，环境变量需由启动终端或进程环境提供。

## 流式生成与恢复

数据链路为：**供应商 Responses SSE → 后端适配器 → 事件持久化 → 作品 SSE → React**。

- **Director 双通道**：将 `response.reasoning_text.delta`、`response.reasoning_summary_text.delta` 归一化为 reasoning 增量；将 `response.output_text.delta` 作为大纲正文增量。reasoning 不参与最终 JSON 解析。
- **按供应商实际输出展示**：供应商可能仅返回正文或不公开 reasoning；界面允许 reasoning 为空，不补造思考内容。推理力度与是否返回可见 reasoning 是不同的能力。
- **后端校验后发布**：供应商完成事件到达后，Director 正文还需通过 JSON 解析与 Pydantic 结构校验，随后才发布可编辑的大纲。
- **页面增量**：页面生成时后端持久化 HTML 增量；最终完整 HTML 通过安全与画布契约校验后，才作为成功页面提供预览。
- **断线与刷新**：浏览器通过 `EventSource` 接收事件，重连使用 `Last-Event-ID`；刷新时先获取作品快照，再从 `last_event_seq` 之后订阅。快照中的流文本限定在该游标内，避免与回放重复拼接。
- **服务重启**：后台队列重新排队中断的任务，并保留已成功的页面。它会重新发起未完成的模型调用，并非从供应商上一次的 token 位置续写；Director 重启执行时会重置当前流文本。

生成结束后仍可展开查看 Director 的生成记录。取消生成会尝试停止本地执行，但供应商对已发请求产生的费用不会因此撤回。

## 用量与运行边界

正式大纲和页面生成**不设置输入、输出 token 预算，也不发送 `max_output_tokens`**。供应商或模型自身的上下文和输出限制仍然适用。

作品会话统计供应商返回的输入、输出与总 token 用量，包含失败、重试等有用量返回的调用。显示 `≥` 表示部分调用未返回对应统计，显示值只是已知下界；`—` 表示没有可用数据。连接测试不计入作品会话用量，但仍可能产生实际 API 费用。

现有非 token 限制继续生效：

| 项目 | 当前约束 |
| --- | --- |
| 用户需求 | 最多 20,000 字符 |
| 作品页数 | 1–20 页，页面编号从 1 连续递增 |
| 页面并行数 | 单个页面生成任务最多 3 页 |
| 正文输出 | 每次调用最多 256 KiB UTF-8 文本 |
| Director reasoning | 每次调用单独最多 256 KiB UTF-8 文本 |
| HTML 契约 | 完整文档、唯一 `id="ppt-page"` 画布、声明 `width:800px` 和 `height:450px` |

页面 HTML 不允许脚本、事件处理器、外部资源、图片或其他主动内容；使用安全检查、预览票据、CSP 与沙箱 iframe 展示。页面 HTML 契约校验失败时，当前实现会携带错误反馈再生成一次；供应商调用错误等失败通过界面重试处理。

## 项目结构

```text
backend/
  app/
    main.py        # FastAPI 接口、配置、作品快照、SSE 与预览
    runtime.py     # LangGraph 任务、模型网关、持久化队列与事件
    providers.py   # Responses 流式适配与供应商错误归一化
    db.py          # SQLAlchemy 数据模型、SQLite 初始化与路由配置
    schemas.py     # 请求、说明书和生成设置的结构契约
    prompts.py     # Director / Page Composer 系统提示词
    rendering.py   # HTML 安全校验、画布契约与内容哈希
    secrets.py     # 系统凭据存储
    config.py      # 环境变量与初始配置读取
  tests/           # Provider 适配与核心生成闭环测试
  requirements.txt
frontend/
  src/
    main.tsx       # 工作台、模型设置、SSE 消费和页面预览
    styles.css
    generation-settings.css
  vite.config.ts   # React 插件与 /api 开发代理
  package.json
data/              # 默认 SQLite 数据目录，运行时创建，不纳入版本控制
```

技术栈：Python / FastAPI / LangGraph / SQLAlchemy / Pydantic / httpx；React / TypeScript / Vite；SQLite 与系统 keyring。

SQLite 保存作品、版本化大纲、运行记录、页面任务和 HTML、持久化任务队列、SSE 事件、模型调用用量、供应商路由和生成设置。默认路径相对于后端启动时的工作目录，因此应始终从仓库根目录启动后端。

可通过 `DATABASE_URL` 指定其他 SQLAlchemy 连接串，但目前仅验证 SQLite。在线数据库还需补充驱动、版本迁移、并发与事务验证；现有初始化使用 `create_all`，不承担完整的数据库版本迁移。SQLite 请使用本机磁盘和单进程写入。

## 主要接口

统一前缀为 `/api/v1`；请求模型和参数约束可查看运行中的 `/docs`，未声明响应模型的接口请结合实现核对响应字段。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET / PUT | `/settings/llm/default` | 查询、保存默认模型配置 |
| POST | `/settings/llm/default/test` | 测试已保存的模型连接 |
| GET / PUT | `/settings/generation` | 查询、保存推理力度 |
| GET | `/settings/llm/calls` | 最近调用记录 |
| GET / POST | `/presentations` | 作品列表、创建并排队生成说明书 |
| GET | `/presentations/{id}` | 作品状态、大纲、页面与流文本快照 |
| PUT | `/presentations/{id}/outline` | 保存大纲新版本 |
| POST | `/presentations/{id}/confirm` | 确认大纲并排队生成页面 |
| POST | `/presentations/{id}/retry` | 重试失败或已取消的生成任务 |
| GET | `/presentations/{id}/events` | SSE 订阅，支持 `after` 与 `Last-Event-ID` |
| GET | `/presentations/{id}/usage` | 当前作品会话用量 |
| POST | `/runs/{id}/cancel` | 请求取消运行 |
| GET | `/page-tasks/{id}/html` | 获取已成功页面的原始 HTML |
| POST | `/page-tasks/{id}/preview-tickets` | 创建短期预览票据 |
| GET | `/previews/{id}?ticket=...` | 使用票据预览页面 |

创建作品支持 `Idempotency-Key` 请求头；保存和确认大纲使用 `outline_id` 与 `content_hash` 防止基于过期版本操作。创建、确认和重试接口将任务入队，不等待完整模型调用完成；前端通过快照和 SSE 跟进状态。

## 开发验证

从仓库根目录运行：

```powershell
.venv/Scripts/python.exe -m pytest backend/tests -q
npm --prefix frontend run build
git diff --check
```

常规后端测试使用临时 SQLite、模拟凭据存储与假模型，不调用收费 API；覆盖生成与确认闭环、幂等性、过期大纲、用量、推理力度、reasoning/正文分离、SSE 游标回放及 HTML 安全契约。前端构建执行 TypeScript 检查和 Vite 打包；构建产物不会由当前后端自动托管。

## 常见问题

- **前端 404 或 API 代理失败**：使用 `npm --prefix frontend run dev -- --port 5173` 启动，确认后端位于 8000 端口。若修改端口，需同步调整开发代理与后端允许的前端来源。
- **找不到作品或生成设置恢复默认**：确认后端是否从仓库根目录启动，以及 `DATABASE_URL` 是否指向原来的数据库文件。
- **模型配置不完整或凭据保存失败**：确认模型已启用、API Key 已保存，以及操作系统 keyring 可用；保存 API Key 不会写回明文配置文件。
- **没有 reasoning 文本**：先确认供应商是否返回对应事件。模型支持推理力度，并不保证接口提供可见的 reasoning 增量。
- **流中断、超时或结构校验失败**：查看界面的任务错误和模型调用记录。接口可连接不等于支持 JSON Schema、所选推理力度和完整生成；重试会重新发起模型请求。

## 许可证

本项目采用 [MIT License](LICENSE)。
