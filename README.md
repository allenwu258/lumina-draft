# Lumina Draft

单用户、本地运行的 Web 创作工具，支持演示文稿和多尺寸视觉设计。输入文字需求、编辑并确认大纲，再逐页生成 HTML，转换为 SceneIR 并在画布中预览。

Python 后端负责 LangGraph 编排、模型调用、校验、任务执行和持久化；React 前端负责交互、进度展示与预览。PPT 和 Agent 模式走 `ppt_create`，由 Director 和 Page Composer 生成固定 **800 × 450（16:9）** 的页面，可导出 PPTX。设计生成走 `design_create`，由 DesignPlanner 和 DesignPageComposer 使用独立提示词和模型路由，复用 SQLite、任务队列、事件、Chromium Worker 和 Canvas 渲染器。

## 当前能力

- **内容策划**：生成作品 Brief、逐页大纲、全局配色、字体和布局规范。
- **大纲流式展示**：分栏显示供应商返回的 reasoning 文本或摘要与 JSON 正文；支持折叠查看、自动跟随滚动，手动向上滚动时暂停跟随。
- **大纲编辑与确认**：完整 JSON 解析并通过结构校验后进入编辑；确认后才启动页面生成。
- **多尺寸设计**：适合海报、社交卡片及系列设计。支持 9:16、3:4、1:1、16:9、4:3 与自定义宽高。每份作品在创建时固定画布，所有页尺寸相同；可指定 1–20 页，留空由大纲决定。自定义宽高须为 320–1920 的整数，面积上限为 2,073,600 px²。
- **页面并行生成**：结合全局规范、当前页任务与相邻页摘要生成独立 HTML；新页面使用受控的 Tailwind CSS v4 utility 子集，预览由后端注入本地编译样式，不依赖 CDN。按所选模型并发分配调用名额，等待页面与正在生成页面分开展示。
- **页面查看**：默认由自研 Canvas 直接渲染持久化 SceneIR，另提供 HTML 预览和 HTML 源码；保留原始 HTML 作为生成来源与对照。当前画布只预览，不提供编辑。
- **PPTX 导出**：完整作品可导出整本 PowerPoint。已有 SceneIR 直接复用，旧 HTML 按需补转换并保存；普通文字和图形优先可编辑，复杂纯几何使用 SVG，模糊阴影与无法忠实表达的局部内容使用透明图片；报告区分矢量文字、局部图片及兼容预览。任务持久化，刷新后继续查询和下载。
- **恢复与重试**：作品、任务和 SSE 事件落库，支持浏览器刷新、断线回放、取消生成与失败重试。
- **模型配置与统计**：独立 Provider 管理页，支持多连接、多模型、主动发现、思考档位、模型并发、业务 Override、模型测试与调用详情；作品按冻结配置生成。

初版提供文字内容与 HTML/CSS、安全内联 SVG 排版及 PPTX 导出，尚未实现配图、生图、附件、Tauri 客户端或多用户服务。

## 使用流程

1. 在“模型与 API”的 Provider 管理页创建连接，获取或手动添加模型并运行测试；再到业务路由设置全局默认模型及思考预算，可分别覆盖 PPT 大纲、PPT HTML、设计大纲和设计 HTML。作品创建时冻结当前工作流的配置。
2. 在创作模式中选择“PPT 生成”或“设计生成”，输入主题、受众、目标和内容要求。设计另选画布与可选页数，然后生成大纲；也可使用 `Ctrl/Cmd + Enter`。
3. 查看 Director 的 reasoning 与 JSON 正文流。流文本仅供查看，尚未完成的大纲不能确认。
4. 等待完整说明书校验通过，检查并编辑逐页内容，再点击“确认并生成”。
5. 查看各页的 HTML 生成与排队转换进度；默认进入新版 Canvas 预览，也可切换 HTML 预览和源码。转换失败只重试转换，不重新调用模型。
6. 遇到失败可查看错误并重试。页面阶段重试保留成功页，仅重新执行未成功的页面。
7. 全部页面内容完成后点击“导出 PPTX”，待准备及导出完成后下载；刷新后可再次下载同一文件。导出不调用模型。

页数由 Director 根据内容自主确定，当前结构契约支持 **1–20 页**。创建接口没有 `page_count` 参数；当前提示词也不把用户文字中的指定页数作为约束或偏好。

## 快速启动

需要 Python 3.11+、Node.js 22.12+、npm，以及可用的系统凭据存储。以下命令在**仓库根目录**的 PowerShell 中运行。

### 安装依赖

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r backend/requirements.txt
npm ci
npm run build:scene
npm run browser:install
```

### 启动后端

```powershell
.venv/Scripts/python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

### 启动前端

在另一个终端中，从仓库根目录运行：

```powershell
npm run dev -- --port 5173
```

| 服务 | 地址 |
| --- | --- |
| Web 工作台 | http://127.0.0.1:5173/ |
| 后端接口文档 | http://127.0.0.1:8000/docs |
| 存活检查 | http://127.0.0.1:8000/api/v1/health/live |
| 就绪检查 | http://127.0.0.1:8000/api/v1/health/ready |

前端开发服务器将 `/api` 代理到 `http://127.0.0.1:8000`。请使用以上命令启动 Vite：直接在仓库根目录运行 Vite 且未指定前端根目录，会找不到正确的页面入口和代理配置。

后端使用单 Uvicorn worker，服务绑定回环地址。当前队列和事件通知按本机单进程设计，不适用于多 worker、公网或多用户部署。

## HTML → SceneIR → Canvas

HTML 校验成功后，HTML、转换意图和事件在同一个事务保存。独立的 SQLite FIFO 转换队列只有一个活动槽，不占模型并发额度。FastAPI 监督一个 Node 子进程和一个热 Chromium；每页使用新 BrowserContext，结束后释放。后端关闭或重启时，同版本中断转换回到队列并更换执行令牌；旧版本未完成任务取消并按冻结来源创建当前版本替代任务。浏览器崩溃等基础设施错误最多自动重试一次；内容错误需要显式重试。

`scene_conversion_jobs` 保存冻结 HTML/CSS、来源哈希和执行状态；`page_scenes` 保存不可变 SceneIR JSON 与资源引用。PageTask 只指向当前任务和场景。旧任务必须同时匹配执行令牌、当前任务引用与 HTML 哈希，才能发布结果。资产先验证并安装，再提交场景、当前引用和事件。

转换使用与 HTML 预览相同的本地 Tailwind CSS 字节和作品冻结的画布尺寸，采用 DPR 1、zh-CN 和 Asia/Shanghai。浏览器计算 Flex/Grid；转换器读取实际几何、文本片段、绘制顺序和裁剪。Canvas 直接绘制 SceneIR，不读取 HTML、DOM 报告或 RenderPlan，也不重新换行。PPT 写出 SceneIR 0.4 / dom-canvas-0.4.0，同时兼容读取 0.3；设计写出支持可变尺寸的 0.5 / dom-canvas-0.5.0，提供 Canvas、HTML 和源码预览。SVG 保留为规范化 image/svg+xml 资产，包含已固化的颜色、字体、局部引用，并显式引用受预算限制的 PNG 兼容资产；Canvas 直接绘制 SVG 来源以适应缩放与 DPR。文字清单通过 text / asset / hidden 目标记录唯一归属，SVG 内文字本轮保留矢量，不拆成独立文本框。

当前覆盖受控 profile 中的布局、背景、圆角实线边框、中文/英文富文本、大小写、字距、基础表格、SVG、透明组和 overflow 裁剪。Box shadow 使用 Canvas 模糊近似并记录提示。合并表格、滤镜、渐变、伪元素文字、变换、复杂文字排版等尚未支持的内容会明确失败，可继续查看 HTML。当前以同一台机器的系统字体为准，字体宽度明显失配时不会发布成功首帧；跨机器字体分发和 Office 一致性留给后续。

已有成功 HTML 页在第一次预览或导出时按需转换，不批量迁移历史作品。预览、生成完成钩子与导出共用 `ensure_page_scene`：先查询匹配来源与当前转换目标的有效 Scene，命中后不因当前 CSS 或字体环境变化而隐式重转；缺失才进入转换队列。同来源升级沿用旧任务冻结的 CSS，保留旧场景显示，成功后才切换。快照分开返回 display_scene、conversion 和 up_to_date；GET 不入队，失败需显式重试。缺少 HTML 的旧场景可继续预览与导出并报告兼容提示；有来源时不能用旧结果掩盖升级失败。旧页缺少来源哈希时统一计算并登记。转换成功后，HTML 对照票据绑定该 Scene 的冻结来源和 CSS。新版预览区分排队、转换、资源加载、客户端绘制错误，可分别重试转换或渲染。

所有 npm 依赖都在仓库根目录安装，已移除前端单独的锁文件。全新安装或修改共享包后，启动后端前先运行 `npm run build:scene`；修改共享渲染器后需重新构建该包，Vite 会加载构建产物。Chromium 使用锁定的 Playwright 版本，通过 `npm run browser:install` 显式安装，不在请求时下载。依赖缺失的原因可在健康接口的 `scene_worker` 字段和转换错误中查看。

默认资源在 `data/scenes/`，可用 `SCENE_DATA_DIR` 指定绝对目录。资源目录必须与 SQLite 一起备份；不要只复制数据库。Worker 无额外 HTTP 端口，通过带 request ID 和 attempt token 的 JSONL 与 Python 通信。Windows 使用 Job Object 约束并回收自己的 Worker/Chromium 进程树，不操作用户浏览器。仍只支持单后端进程。

## SceneIR → PPTX

导出只消费已保存 SceneIR 与其声明资产，不读取 HTML 排版。RenderPlan 在导出时按页生成，仅是可重新计算的内存结构；Canvas 预览继续直接消费 SceneIR。Node 完成字体、规划与资产校验，PptxGenJS 4.0.1 在现有 Chromium 的隔离 Context 内统一使用标准 `addText/addShape/addImage` 接口写出，800×450 对应 13⅓×7.5 英寸，统一 `inch = px/60`、`pt = px×1.2`。

`pptx_exports` 一张表保存冻结页序、来源与最终 Scene 引用、状态、错误及文件结果。状态为 `PREPARING / QUEUED / RUNNING / SUCCEEDED / FAILED`。准备阶段复用 Scene 转换且不占执行槽；转换和整本导出共享一个 Node/Chromium 执行槽，不占 Provider 并发。准备期间缺 Scene 的页被重新生成会报告来源冲突；最终清单固定后，后续页面修改不会混入本次导出。重复点击复用等价任务，失败需显式重试，中断 RUNNING 在重启后重新排队并更换令牌。

文字按保存的显示行/片段输出，关闭自动换行和缩放；字体测量包含可变字体 wght，并与 PowerPoint 的普通/粗体输出匹配。可证明位置的混合字体拆成小片段；不支持的片段才局部栅格化。整页矩形裁剪交给页面视口，完全越界内容报告不可见。纯几何圆角、边框、内部裁剪和透明组合通过小型 Scene→SVG 函数输出，阴影尽量单独转成 PNG、主体保留矢量；其余组合使用共享 SceneRenderer 局部合成。禁止静默整页截图。表格第一版是固定单元格图形与文字，不是原生 PowerPoint table。不提供选页、导出设置、字体嵌入或跨机器像素一致性保证。

导出请求和 latest_export 缓存同时绑定当前转换目标与 scene-pptx-0.2.0；旧文件仍可按 ID 下载。候选文件先独立检查 ZIP/OPC、页序、文字片段覆盖、SVG 主资源哈希、PNG 兼容资源有效性、资源引用和几何，再原子保存并发布成功。report_json 保存每处 SVG / PNG 的原因、节点和边界以及文字编辑性统计。默认文件在 `data/exports/`，可用 `PPTX_EXPORT_DIR` 指定绝对目录，与数据库和 Scene 资产一同备份。下载接口只读取成功文件；文件丢失/损坏明确报错，不在 GET 中重转或重建。

开发时可用 `powershell.exe -NoProfile -STA -File services/slide-worker/scripts/check-office.ps1 -Pptx <绝对文件路径> -OutputDirectory <已存在的空目录>` 在本机 PowerPoint 中检查文字与页面 PNG。脚本只管理自己创建的 Office 实例；已有 PowerPoint 进程时停止该项检查。Office 不是导出运行依赖。

## 模型与 API 配置

### Provider 与模型目录

工作台右上角“模型设置”或侧栏“模型与 API”进入独立页面：

- `/settings/providers`：管理连接、API Key、模型目录与模型测试。
- `/settings/routing`：配置全局默认与两个 PPT 业务 Override。
- `/settings/calls`：按游标查看调用记录，检查实际模型、预算、耗时、用量和错误。

每个 Provider 配置显示名称、API 根地址、认证方式、协议和启用状态。当前仅执行 **OpenAI Responses**；其他协议在 UI 中为不可选的扩展项。API 根地址不要包含 `/responses` 或 `/models`，后端自动追加。默认使用 HTTPS；本机回环地址允许 HTTP，可信局域网私有 IP 需显式启用 HTTP 开关。拒绝 URL 用户信息、查询参数、片段和重定向。

API Key 可以留空：无认证服务选择“无认证”；Bearer 模式未配置 Key 时可保存，但不能测试或用于业务。Key 只写入系统凭据库，不向前端回传、不写 SQLite 或浏览器存储。编辑时输入框为空表示保留现有 Key；替换会生成新版本，旧作品继续使用历史版本。显式“撤销全部凭据”或切换无认证会使该 Provider 的当前及历史 Key 失效。

“获取可用模型”仅在点击时发送一次 `GET /models`，展示候选供勾选导入，不自动覆盖已有配置。模型目录不支持发现时可手动添加；接口未提供的上下文和输出长度保持为空，不按名称猜测。模型可编辑远程 ID、显示名、上下文、最大输出元数据、思考档位和独立并发限制。新模型默认并发为 1，并声明多模态与工具支持，来源为 `assumed`；当前生成仍仅使用文本。

模型并发限制为 **1–32**，同一模型的 Director、页面生成和该模型测试共享额度；不同模型互不争抢。页面进入“生成中”表示它已取得模型调用名额，等待中的页面显示“等待模型调用名额”。排队按 Run 轮转、Run 内按进入顺序处理；提高模型上限会唤醒等待队列，降低上限不打断已开始的请求，并在活动调用降到新上限以下后继续接纳。模型发现发生在模型选定之前，不占用模型并发额度。

任务调度器最多同时执行 `MAX_ACTIVE_RUNS` 个 Run（默认 8），并跨模型轮转挑选任务；该上限与每个模型自己的调用并发分别生效。Scheduler 等待超时由 `SCHEDULER_QUEUE_TIMEOUT_SECONDS` 控制，LLM 调用等待模型名额超时由 `MODEL_QUEUE_TIMEOUT_SECONDS` 控制，默认均为 1800 秒；请求开始后的供应商调用截止时间为 900 秒。重启时未完成 Job 会重新入队并重新计算 Scheduler 等待时间。

升级旧数据库时，迁移会将每个 Provider 当前并发限制复制到其所有绑定模型，随后删除 Provider 并发列；迁移记录确保只执行一次。并发协调器是进程内实现，SQLite 部署须保持单后端进程/单 Uvicorn worker。

页面状态区分等待名额、生成中、HTML 校验和完成，只有模型请求阶段转圈。调用账本区分 `QUEUED` 与 `RUNNING`；排队时间独立记录，首 Token 耗时从取得名额开始计算。请求自身的 900 秒截止不包含前置排队；模型发现 30 秒与每项测试 45 秒仍包含各自请求的等待时间。

### 模型测试与业务路由

先保存连接和模型，再点击模型行的“测试”。测试通过正式生成使用的 Gateway/Responses 适配器，依次检查文本流和严格 JSON Schema，发送两次低输出上限请求（每次 `max_output_tokens=64`，省略 reasoning）。界面会提示真实测试可能产生费用。测试不验证具体推理档位、多模态或工具能力。测试结果绑定连接、凭据、远程 ID 和档位映射；相关配置变更后需重新测试。

路由按 Provider 分组选择模型，思考预算与模型独立继承：

| 业务 | 模型与预算 |
| --- | --- |
| 全局默认 `default` | 必须明确选择模型和档位 |
| PPT 大纲 `ppt_outline` | 可分别继承或覆盖默认模型和预算，需通过文本流与结构化检查 |
| PPT HTML `ppt_html` | 可分别继承或覆盖，需通过文本流检查 |

默认档位是 `off / low / medium / high`，可新增模型专属档位并设置 API effort 映射；`off` 表示省略整个 `reasoning`。继承预算在目标模型中不可用时，保存明确报错，不静默改档。新作品创建前验证三个路由就绪，并冻结解析后的 Provider、模型、凭据版本与预算；后续配置修改不改变已有作品。停用/归档连接或模型、撤销凭据会阻止其后续调用，包括排队中的请求。

归档保留历史数据，被路由引用时返回 409，需先切换路由。模型归档后可使用相同远程 ID 重新添加为新目录项。编辑命令校验 `expected_revision`，配置冲突返回 409。

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
| `MAX_ACTIVE_RUNS` | Scheduler 同时执行的 Run 数 | `8` |
| `SCHEDULER_QUEUE_TIMEOUT_SECONDS` | Job 等待 Scheduler 的超时 | `1800` |
| `MODEL_QUEUE_TIMEOUT_SECONDS` | LLM 等待模型并发名额的超时 | `1800` |

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
| 页面并行数 | 按所选模型并发 1–32 分配，单作品最多 20 页 |
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
    provider_api.py # Provider/Model CRUD、发现、测试、路由与调用记录
    provider_management.py # 配置解析、测试指纹和共享并发协调
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
    main.tsx       # 工作台、设置页导航、SSE 消费和页面预览
    ProviderSettings.tsx # Provider 管理工作区
    ProviderEditor.tsx / ModelEditor.tsx # 连接和模型编辑
    RoutingSettings.tsx / CallHistory.tsx # 业务路由与调用记录
    provider-api.ts / provider-settings.css # 类型、请求与设置样式
    styles.css
    generation-settings.css
  vite.config.ts   # React 插件与 /api 开发代理
  package.json
packages/scene-core/ # SceneIR 0.3 / 0.4 契约、类型与引用/几何校验
packages/scene-renderer/ # 只消费 SceneIR 的 Canvas 2D 渲染器
services/slide-worker/ # 常驻 Node/Chromium，SceneIR 转换与 PptxGenJS 导出
package.json / package-lock.json # npm workspaces 与唯一安装锁文件
data/              # 默认 SQLite 数据目录，运行时创建，不纳入版本控制
```

技术栈：Node.js / Playwright Chromium / Canvas 2D；Python / FastAPI / LangGraph / SQLAlchemy / Pydantic / httpx；React / TypeScript / Vite；SQLite 与系统 keyring。

SQLite 保存作品、版本化大纲、运行记录、页面任务和 HTML、持久化任务队列、SSE 事件、模型调用用量、供应商路由和生成设置。默认路径相对于后端启动时的工作目录，因此应始终从仓库根目录启动后端。

可通过 `DATABASE_URL` 指定其他 SQLAlchemy 连接串，但目前仅验证 SQLite。在线数据库还需补充驱动、版本迁移、并发与事务验证；启动时为现有 SQLite 执行可重入的增量 Schema 升级，并在 `schema_migrations` 记录 Provider 管理数据迁移。旧配置的 ID、凭据引用和作品快照保留；旧 `auto` 对应 off（省略 reasoning），`none` 与 `max` 保留为模型专属兼容档位。迁移中断后可重启续做，不重复导入。当前保留规范化的 Provider/Endpoint/LogicalModel/Binding 存储关系，未引入 Alembic，也未验证其他数据库迁移。升级前应停止服务并备份 SQLite 与系统凭据。SQLite 请使用本机磁盘和单进程写入。

## 主要接口

统一前缀为 `/api/v1`；请求模型和参数约束可查看运行中的 `/docs`，未声明响应模型的接口请结合实现核对响应字段。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET / POST | `/settings/llm/providers` | 查询、创建 Provider |
| GET / PATCH / DELETE | `/settings/llm/providers/{id}` | 详情、更新、归档 |
| PUT / DELETE | `/settings/llm/providers/{id}/credential` | 版本化替换 / 撤销全部凭据 |
| POST | `/settings/llm/providers/{id}/discover-models` | 主动发现候选，需 `expected_revision` |
| GET / POST | `/settings/llm/providers/{id}/models` | 模型目录、添加模型 |
| PATCH / DELETE | `/settings/llm/models/{id}` | 更新 / 归档模型 |
| PUT | `/settings/llm/models/{id}/reasoning-tiers` | 保存思考档位 |
| POST | `/settings/llm/models/{id}/test` | 文本流与 JSON Schema 检查 |
| GET | `/settings/llm/business-routes` | 路由及生效配置 |
| PUT | `/settings/llm/business-routes/{code}` | 保存默认 / 业务 Override |
| GET | `/settings/llm/calls`、`/settings/llm/calls/{id}` | 游标列表 / 脱敏详情 |
| GET / PUT | `/settings/llm/default`、`/settings/generation` | 旧客户端兼容接口，界面已使用新接口 |
| POST | `/settings/llm/default/test` | 兼容接口，内部执行同一模型双项测试 |
| GET / POST | `/presentations` | 作品列表、创建并排队生成说明书 |
| GET | `/presentations/{id}` | 作品状态、大纲、页面与流文本快照 |
| PUT | `/presentations/{id}/outline` | 保存大纲新版本 |
| POST | `/presentations/{id}/confirm` | 确认大纲并排队生成页面 |
| POST | `/presentations/{id}/retry` | 重试失败或已取消的生成任务 |
| GET | `/presentations/{id}/events` | SSE 订阅，支持 `after` 与 `Last-Event-ID` |
| GET | `/presentations/{id}/usage` | 当前作品会话用量 |
| POST | `/presentations/{id}/exports` | 创建/复用整本 PPTX 导出，提交 expected_source_hash 与可选 retry |
| GET | `/exports/{id}` | 导出准备进度、状态、错误及下载地址 |
| GET | `/exports/{id}/file` | 下载已成功的不可变 PPTX 文件 |
| POST | `/runs/{id}/cancel` | 请求取消运行 |
| POST | `/page-tasks/{id}/scene-conversions` | 幂等确保转换或显式重试，要求 expected_html_hash |
| GET | `/scenes/{id}` | 获取指定不可变 SceneIR，支持 ETag |
| GET | `/scenes/{id}/assets/{asset_id}` | 获取该场景已声明的受控资产 |
| GET | `/page-tasks/{id}/html` | 获取已成功页面的原始 HTML |
| POST | `/page-tasks/{id}/preview-tickets` | 创建短期预览票据 |
| GET | `/previews/{id}?ticket=...` | 使用票据预览页面 |

创建作品支持 `Idempotency-Key` 请求头；保存和确认大纲使用 `outline_id` 与 `content_hash` 防止基于过期版本操作。创建、确认和重试接口将任务入队，不等待完整模型调用完成；前端通过快照和 SSE 跟进状态。

## 开发验证

从仓库根目录运行：

```powershell
.venv/Scripts/python.exe -m pytest backend/tests -q
npm run build
npm run browser:install
npm run test:scene
npm run test:e2e
npm --prefix frontend run format:providers:check
git diff --check
```

常规后端测试使用临时 SQLite、模拟凭据存储与假模型，不调用收费 API；覆盖生成与确认闭环、幂等性、过期大纲、用量、推理力度、reasoning/正文分离、SSE 游标回放及 HTML 安全契约。新增 Provider 测试覆盖 CRUD、版本冲突、凭据轮换/撤销、路由继承与冻结、并发取消、排队期间撤销、发现失败和旧库迁移。Playwright 自动启动独立临时数据库与假模型后端，覆盖配置→发现→测试→路由→作品生成，以及 Key 清空、390px 窄屏与键盘焦点；默认使用独立的 18000/15173 端口，可通过 E2E_API_PORT/E2E_WEB_PORT 覆盖；新增验证 Provider 为 1/20 时实际请求数、页面转圈数和刷新恢复一致。不会访问真实 Provider 或系统凭据库。前端构建执行 TypeScript 检查和 Vite 打包；构建产物不会由当前后端自动托管。

Provider 新模块格式化使用 `npm --prefix frontend run format:providers`；后端新模块使用 `black`，可通过 `.venv/Scripts/python.exe -m pip install -r backend/requirements-dev.txt` 安装开发依赖。

Scene 专项测试使用真实 Chromium 与独立 HTML 截图比较，检查文字轮廓、形状、表格、SVG、DPR 与确定性；队列测试注入可控转换故障，验证单槽、幂等、恢复、取消与发布拦截。浏览器端到端覆盖默认 Canvas、HTML 对照、源码和刷新复用场景，不调用真实模型。

## 常见问题

- **前端 404 或 API 代理失败**：使用 `npm run dev -- --port 5173` 启动，确认后端位于 8000 端口。若修改端口，需同步调整开发代理与后端允许的前端来源。
- **找不到作品或生成设置恢复默认**：确认后端是否从仓库根目录启动，以及 `DATABASE_URL` 是否指向原来的数据库文件。
- **模型配置不完整或凭据保存失败**：确认模型已启用、API Key 已保存，以及操作系统 keyring 可用；保存 API Key 不会写回明文配置文件。
- **没有 reasoning 文本**：先确认供应商是否返回对应事件。模型支持推理力度，并不保证接口提供可见的 reasoning 增量。
- **流中断、超时或结构校验失败**：查看界面的任务错误和模型调用记录。接口可连接不等于支持 JSON Schema、所选推理力度和完整生成；重试会重新发起模型请求。

## 许可证

本项目采用 [MIT License](LICENSE)。
