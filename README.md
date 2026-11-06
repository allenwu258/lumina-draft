# Lumina Draft

单用户 Web 演示文稿创作工具。用户提交文字需求后，Director 生成结构化说明书；用户可编辑并确认，Page Composer 随后并行生成 800 × 450 HTML 页面。后端执行 Agent、模型调用、任务与事件持久化；React 只负责交互和显示。

## 启动

需要 Python 3.11+、Node.js 20+，以及可用的系统凭据存储。以下命令在仓库根目录的 PowerShell 中运行：

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r backend/requirements.txt
npm --prefix frontend ci
.venv/Scripts/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

在另一个终端运行：

```powershell
npm --prefix frontend run dev -- --port 5173
```

打开 http://127.0.0.1:5173/。后端交互式接口文档位于 http://127.0.0.1:8000/docs。服务仅供本机单用户使用，请保留回环地址与单 Uvicorn worker。前端开发代理固定指向 8000 端口。

## 配置模型

通过工作台右上角“模型设置”配置 Responses Base URL、远程模型 ID 和 API Key。Base URL 为 API 根路径，适配器追加 `/responses`。仅支持 HTTPS，回环地址例外。Key 写入系统凭据存储，不返回前端，也不写入 SQLite。第一次启动时如存在 `~/Downloads/ds-api.txt`，可从该文件读取 `base_url`、`model`、`api_key` 初始化；也可通过 `DS_API_FILE` 指定路径或使用 `DS_API_BASE_URL`、`DS_API_MODEL`、`DS_API_KEY` 环境变量。初始化后修改请使用设置页面。

同一设置面板中的“推理力度”全局应用于大纲和页面生成，默认 `high`。修改并保存后仅影响新建作品；`自动` 会在请求中省略 `reasoning.effort`。

点击“测试连接”会发起一次真实模型请求，产生供应商费用。作品开始生成时会冻结当前模型地址、模型 ID 和凭据引用。两个 Agent 共用 `default` 路由。已发请求的供应商计费无法撤回。

正式生成请求不设置输入或输出 token 预算；供应商或模型自身的上下文和输出限制仍然适用。作品会话中展示供应商返回的输入、输出及总 token 用量，包含失败和重试中有返回用量的调用；`≥` 表示部分调用未返回对应数据，实际用量可能更高。连接测试不计入会话统计。现有的请求字符数、输出字节数及 HTML 安全准入限制仍然生效，它们不是 token 计数限制。

Director 生成大纲时，工作台会将 Responses 流中的 reasoning 文本与 JSON 正文分栏实时显示，并持久化以支持刷新恢复；供应商未返回 reasoning 增量时会显示明确提示。流文本仅作查看，最终 JSON 完整解析和校验后才开放结构化大纲编辑与确认。DeepSeek Responses 的事件行为以[官方文档](https://api-docs.deepseek.com/guides/responses_api#streaming)为准。

默认数据库为工作目录的 `data/lumina.db`；可用 `DATABASE_URL` 指定其他 SQLAlchemy 连接串。当前仅验证 SQLite；更换在线数据库前需要补充驱动、迁移、并发与事务测试。请勿在网络共享目录多进程写 SQLite。

## 测试

```powershell
.venv/Scripts/python -m pytest backend/tests -q
npm --prefix frontend run build
```

常规测试使用假模型，不访问收费 API。当前版本面向本机单用户使用，不支持公网或多用户部署；尚不支持 PPTX 导出、图片、附件、生图或 Tauri 封装。

## 许可证

本项目采用 [MIT License](LICENSE)。
