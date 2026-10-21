# comfyui_seedream

Seedream 生图 ComfyUI 节点（URL + 图片双参考输入，**Apifox 风格直调版**），基于 `261016 — seedream 生图 API 对接文档`实现。

## 设计原则（v1.1.0）

节点 = 纯调用工具，**不做任何输入输出加工**：

- 入参与 `POST /seedream-image` 请求体**一一对应、原样直传**，无客户端预校验/截断
- 输出为提交接口、状态接口的**原始响应体文本**
- 错误**原样抛出**：异常消息 = `HTTP {状态码}: {原始响应体}`，所见即接口返回
- 节点只负责三件事：组装请求体（含 IMAGE 必需的 base64 编码）、轮询、下载结果图

## 节点

| 节点 | 说明 |
|---|---|
| **Seedream Config (URL/Image)** | appId / apiKey / 环境 / base_url / 轮询参数 |
| **Seedream Image (URL/Image)** | 输出 `image`（IMAGE）、`submit_response`（提交接口原始响应体）、`result_response`（状态接口原始响应体） |

## 参数与 API 字段对照

| 节点参数 | API 字段 | 说明 |
|---|---|---|
| `prompt` | `prompt` | 直传，≤600 字符由服务端 422 校验 |
| `size` | `size` | 直传：`1K`/`1.5K`/`2K` 或 `宽x高`（如 `2048x2048`） |
| `output_format` | `output_format` | `jpeg` / `png` |
| `watermark` | `watermark` | true / false |
| `seed` | `taskId` | seed + 时间戳派生 taskId（API 无种子概念，改 seed 即重新生成） |
| `url1~10` + `image1~10` | `images` | 参考图 URL 与 IMAGE 张量混用，原样放入数组；超 10 张由服务端 422 |

## 凭证配置（三选一，优先级从高到低）

1. 连接 Config 节点；2. 环境变量 `SEEDREAM_APP_ID` / `SEEDREAM_API_KEY` / `SEEDREAM_BASE_URL`；3. 插件目录 `config.json`（模板见 `config.example.json`）

## 默认链路

| environment | base_url |
|---|---|
| dev | `http://www.tongganagent.cn/api/v2/tryon`（必须 http，该域名 https 证书与域名不匹配） |
| staging | `http://127.0.0.1:18001/api/v2/tryon`（仅部署机，一般需手动覆盖） |
| prod | `https://www.tongganai.com/api/v2/tryon` |

## 已知服务端问题（与节点无关）

`GET /seedream-image/status` 在**任务存在时固定返回 HTTP 500**（任务不存在时正常返回 401 信封）。
已用最小复现确认：认证通过 + taskId 存在于 Redis → 500；taskId 不存在 → 401。
属服务端任务查询成功路径的 bug（疑似响应序列化），待 ai-engine 修复后本节点即可端到端跑通。

## 安装

将 `comfyui_seedream` 文件夹复制到 ComfyUI 的 `custom_nodes/` 目录，重启即可。

## 文件清单

```
comfyui_seedream/
├── __init__.py
├── nodes.py               # 两个节点 + 网络层 + 工具函数
├── pyproject.toml
├── requirements.txt
├── config.example.json
└── README.md
```
