"""
ComfyUI 自定义节点：Seedream 生图（Apifox 风格直调版）

接口文档：261016 — seedream 生图 API 对接文档
- 提交任务：POST {base_url}/seedream-image
- 查询状态：GET {base_url}/seedream-image/status?taskId={seedreamTaskId}
- 认证：X-App-Id / X-Api-Key 请求头
- 模型：seedream-image-v5.0-pro（腾讯云 TokenHub 模型广场）

设计原则（v1.1.0）：节点 = Apifox，仅做调用。
- 入参原样直传 API，不做客户端预校验/截断/改写
- 接口响应原样输出（提交响应、状态响应各一个输出口，即原始报文）
- 错误原样抛出：异常消息 = "HTTP {状态码}: {原始响应体}"，不做二次封装
- 节点只负责三件事：组装请求体（含 IMAGE 必需的 base64 编码）、轮询、下载结果图

参考图输入：url1~url10（URL）与 image1~image10（IMAGE 张量，编码为
JPEG base64 data URI）可混用，全部原样放入 images 数组，超 10 张由服务端 422。
"""

import base64
import io
import json
import os
import time

import numpy as np
import requests
import torch
import urllib3
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

CATEGORY = "api/Seedream"

# 默认链路（对应部署环境的网关白名单地址，见对接文档 §2.2）
# 注意：dev 必须用 http —— www.tongganagent.cn 的 https 证书 CN=pc.tongganai.com，
# 与域名不匹配，严格校验必然 SSL 失败；http 可达且路由正常（401 信封实测）
DEFAULT_DEV_URL = "http://www.tongganagent.cn/api/v2/tryon"
DEFAULT_STAGING_URL = "http://127.0.0.1:18001/api/v2/tryon"
DEFAULT_PROD_URL = "https://www.tongganai.com/api/v2/tryon"

# 配置文件（可选）：插件目录下的 config.json
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# 错误消息中响应体截取上限（避免工作流弹窗被超长报文刷屏）
ERROR_BODY_LIMIT = 2000

# ---------------------------------------------------------------------------
# 网络层
# ---------------------------------------------------------------------------

# 传输层重试（429/5xx 自动退避），不改变任何请求/响应内容
API_RETRY_STRATEGY = Retry(
    total=5,
    backoff_factor=1.0,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS", "TRACE"],
    raise_on_status=False,
)

DOWNLOAD_RETRY_STRATEGY = Retry(
    total=8,
    backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "OPTIONS"],
    raise_on_status=False,
)


class SSLAdapter(HTTPAdapter):
    """自定义适配器，兼容旧服务器 TLS 设置"""
    def init_poolmanager(self, *args, **kwargs):
        import ssl
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        kwargs['ssl_context'] = context
        return super().init_poolmanager(*args, **kwargs)


def _create_session(retry_strategy: Retry, verify: bool = True) -> requests.Session:
    """创建带传输层重试的 Session"""
    session = requests.Session()
    session.verify = verify
    session._retry_strategy = retry_strategy
    adapter = SSLAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """发送请求；仅对证书校验失败做一次性降级（传输层问题，与业务无关）"""
    try:
        return session.request(method, url, **kwargs)
    except requests.exceptions.SSLError as e:
        if "CERTIFICATE_VERIFY_FAILED" not in str(e):
            raise
        print(f"[Seedream] 警告: {url.split('/api')[0]} 证书校验失败"
              f"（服务器证书与域名不匹配），已降级为跳过证书校验继续请求。")
        strategy = getattr(session, "_retry_strategy", API_RETRY_STRATEGY)
        insecure = _create_session(strategy, verify=False)
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return insecure.request(method, url, **kwargs)


def _http_error(resp: requests.Response) -> RuntimeError:
    """构造 Apifox 风格错误：HTTP 状态码 + 原始响应体，零加工"""
    body = (resp.text or "")[:ERROR_BODY_LIMIT]
    return RuntimeError(f"HTTP {resp.status_code}: {body}")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _url_to_tensor(url: str, timeout: int = 120, max_retries: int = 8) -> torch.Tensor:
    """下载结果图 URL -> ComfyUI IMAGE tensor (1,H,W,C)

    结果 URL 仅 12 小时有效（上游 COS 预签名），成功后立即下载转存。
    """
    session = _create_session(DOWNLOAD_RETRY_STRATEGY)
    last_exception = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, timeout=timeout, stream=True)
            resp.raise_for_status()
            content = resp.content
            pil = Image.open(io.BytesIO(content)).convert("RGB")
            arr = np.asarray(pil).astype(np.float32) / 255.0
            return torch.from_numpy(arr)[None, ...]
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                urllib3.exceptions.SSLError,
                urllib3.exceptions.ProtocolError) as e:
            last_exception = e
            wait = min(2 ** attempt, 60)
            print(f"[Seedream] 下载图片失败 (尝试 {attempt}/{max_retries}): {type(e).__name__}: {e}")
            time.sleep(wait)
        except Exception as e:
            raise RuntimeError(f"下载图片失败: {e}") from e

    raise RuntimeError(
        f"下载图片失败，已重试 {max_retries} 次。"
        f"最后错误: {type(last_exception).__name__}: {last_exception}\n"
        f"URL: {url}"
    ) from last_exception


def _tensor_to_data_uri(tensor: torch.Tensor, quality: int = 90) -> str:
    """ComfyUI IMAGE tensor (B,H,W,C) -> JPEG base64 data URI

    images 字段只接受 URL/图片数据；管线内 IMAGE 必需的编码步骤。
    """
    if tensor is None:
        raise ValueError("IMAGE 输入为空")
    arr = tensor[0].cpu().numpy()  # 取 batch 首帧 (H,W,C)，float32 0~1
    if arr.ndim != 3 or arr.shape[2] not in (3, 4):
        raise ValueError(f"IMAGE 张量格式异常: shape={arr.shape}，期望 (H,W,3/4)")
    if arr.shape[2] == 4:          # RGBA -> RGB（白底合成，避免黑边）
        rgb = arr[..., :3]
        alpha = arr[..., 3:4]
        arr = rgb * alpha + (1.0 - alpha)
    arr = (np.clip(arr, 0.0, 1.0) * 255.0).round().astype(np.uint8)
    pil = Image.fromarray(arr, "RGB")
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _blank_image(size: int = 512) -> torch.Tensor:
    return torch.zeros((1, size, size, 3), dtype=torch.float32)


def _load_file_config() -> dict:
    """从 config.json 读取默认凭证（可选）"""
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _mask_secret(s: str) -> str:
    """凭据脱敏：只显示前 6 位（仅用于控制台日志）"""
    if not s:
        return "(空)"
    return (s[:6] + "****") if len(s) > 6 else "****"


# ---------------------------------------------------------------------------
# 节点 1：API 配置
# ---------------------------------------------------------------------------

class SeedreamAPIConfig:
    """Seedream 接口凭证与调用参数配置"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "app_id": ("STRING", {"default": "", "multiline": False,
                                      "placeholder": "app_xxxxxxxx"}),
                "api_key": ("STRING", {"default": "", "multiline": False,
                                       "placeholder": "sk-xxxxxxxx"}),
                "environment": (["dev", "staging", "prod"], {"default": "dev"}),
                "base_url": ("STRING", {"default": "", "multiline": False,
                                        "placeholder": "留空则按 environment 使用默认地址"}),
                "poll_interval": ("INT", {"default": 3, "min": 1, "max": 60,
                                           "tooltip": "状态轮询间隔（秒）"}),
                "max_wait": ("INT", {"default": 300, "min": 30, "max": 3600,
                                      "tooltip": "最长等待时间（秒）"}),
            }
        }

    RETURN_TYPES = ("SEEDREAM_API_CONFIG",)
    RETURN_NAMES = ("api_config",)
    FUNCTION = "build"
    CATEGORY = CATEGORY
    DESCRIPTION = "Seedream 接口配置（appId / apiKey / 环境 / 轮询参数）"

    def build(self, app_id, api_key, environment, base_url,
              poll_interval, max_wait):
        if not base_url.strip():
            defaults = {
                "dev": DEFAULT_DEV_URL,
                "staging": DEFAULT_STAGING_URL,
                "prod": DEFAULT_PROD_URL,
            }
            base_url = defaults.get(environment, DEFAULT_DEV_URL)
        config = {
            "app_id": app_id.strip(),
            "api_key": api_key.strip(),
            "base_url": base_url.strip().rstrip("/"),
            "poll_interval": int(poll_interval),
            "max_wait": int(max_wait),
        }
        return (config,)


# ---------------------------------------------------------------------------
# 节点 2：Seedream 生图
# ---------------------------------------------------------------------------

class SeedreamImage:
    """提交 Seedream 生图任务并轮询等待结果（Apifox 风格直调）

    参数与 POST /seedream-image 请求体一一对应，原样直传：
    prompt / size / output_format / watermark / images（url1~10 + image1~10 混用）。
    输出为两个接口的原始响应体文本；错误原样抛出（HTTP 状态码 + 原始报文）。
    """

    MAX_IMAGES = 10

    @classmethod
    def INPUT_TYPES(cls):
        optional = {
            "api_config": ("SEEDREAM_API_CONFIG",),
        }
        for i in range(1, cls.MAX_IMAGES + 1):
            optional[f"url{i}"] = ("STRING", {"forceInput": True,
                                              "tooltip": "参考图 URL"})
        for i in range(1, cls.MAX_IMAGES + 1):
            optional[f"image{i}"] = ("IMAGE", {"forceInput": True,
                                               "tooltip": "参考图（自动编码 base64）"})
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "size": ("STRING", {"default": "2K", "multiline": False,
                                     "tooltip": "档位 1K/1.5K/2K，或显式 宽x高 如 2048x2048"}),
                "output_format": (["jpeg", "png"], {"default": "jpeg"}),
                "watermark": ("BOOLEAN", {"default": False}),
                "skip_error": ("BOOLEAN", {"default": False}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff,
                                  "control_after_generate": True,
                                  "tooltip": "用于派生 taskId，改 seed 即重新生成"}),
            },
            "optional": optional,
        }

    RETURN_TYPES = ("IMAGE", "STRING", "STRING")
    RETURN_NAMES = ("image", "submit_response", "result_response")
    FUNCTION = "generate"
    CATEGORY = CATEGORY
    DESCRIPTION = ("Seedream 生图（直调）：输出结果图、提交接口原始响应、状态接口原始响应")

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_config(api_config) -> dict:
        """优先使用 api_config 输入；未连接时回退到环境变量 / config.json"""
        if api_config and api_config.get("app_id") and api_config.get("api_key"):
            return api_config

        file_cfg = _load_file_config()
        return {
            "app_id": os.environ.get("SEEDREAM_APP_ID", file_cfg.get("app_id", "")),
            "api_key": os.environ.get("SEEDREAM_API_KEY", file_cfg.get("api_key", "")),
            "base_url": os.environ.get(
                "SEEDREAM_BASE_URL",
                file_cfg.get("base_url", DEFAULT_DEV_URL)).rstrip("/"),
            "poll_interval": int(file_cfg.get("poll_interval", 3)),
            "max_wait": int(file_cfg.get("max_wait", 300)),
        }

    @staticmethod
    def _collect_references(kwargs) -> list:
        """收集参考输入：URL 原样传入 + IMAGE 编码为 data URI，全部原样入数组"""
        references = []
        for i in range(1, SeedreamImage.MAX_IMAGES + 1):
            url = kwargs.get(f"url{i}")
            if url and isinstance(url, str) and url.strip():
                references.append(url.strip())
        for i in range(1, SeedreamImage.MAX_IMAGES + 1):
            tensor = kwargs.get(f"image{i}")
            if tensor is not None:
                data_uri = _tensor_to_data_uri(tensor)
                print(f"[Seedream] image{i} -> dataURI ({len(data_uri) // 1024} KB)")
                references.append(data_uri)
        return references

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def generate(self, prompt, size, output_format, watermark,
                 skip_error, seed, api_config=None, **kwargs):
        try:
            return self._run(prompt, size, output_format, watermark,
                             seed, api_config, kwargs)
        except Exception as e:
            if not skip_error:
                raise
            print(f"[Seedream] 生图失败（skip_error 已开启）: {e}")
            return (_blank_image(), "", str(e))

    def _run(self, prompt, size, output_format, watermark,
             seed, api_config, kwargs):
        cfg = self._resolve_config(api_config)
        if not cfg["app_id"] or not cfg["api_key"]:
            raise ValueError(
                "缺少 appId / apiKey：请连接 api_config 节点，"
                "或设置环境变量 SEEDREAM_APP_ID / SEEDREAM_API_KEY，"
                "或在插件目录创建 config.json")

        # taskId：seed + 时间戳派生，保证同一 seed 可复现、不同 seed 触发重跑
        task_id = int((seed + int(time.time() * 1000)) % 2_000_000_000)

        # 请求体与 API 字段一一对应，原样直传
        body = {
            "taskId": task_id,
            "prompt": prompt,
            "size": size,
            "output_format": output_format,
            "watermark": bool(watermark),
        }
        images = self._collect_references(kwargs)
        if images:
            body["images"] = images

        headers = {
            "Content-Type": "application/json",
            "X-App-Id": cfg["app_id"],
            "X-Api-Key": cfg["api_key"],
        }

        print(f"[Seedream] POST {cfg['base_url']}/seedream-image "
              f"(app_id={cfg['app_id']} key={_mask_secret(cfg['api_key'])})")
        print(f"[Seedream] body: {json.dumps({**body, 'images': f'<{len(images)} 张>'}, ensure_ascii=False)}"
              [:500])

        # 1. 提交任务（错误原样抛出）
        session = _create_session(API_RETRY_STRATEGY)
        submit_resp = _request(session, "POST",
                               f"{cfg['base_url']}/seedream-image",
                               json=body, headers=headers, timeout=30)
        if submit_resp.status_code != 200:
            raise _http_error(submit_resp)
        submit_text = submit_resp.text

        try:
            submit_data = json.loads(submit_text)
            seedream_task_id = submit_data["data"]["seedreamTaskId"]
        except (ValueError, KeyError, TypeError):
            raise RuntimeError(f"提交响应缺少 seedreamTaskId，原始响应: {submit_text[:ERROR_BODY_LIMIT]}")

        print(f"[Seedream] 已提交 seedreamTaskId={seedream_task_id}")

        # 2. 轮询（错误原样抛出，不做容错包装）
        status_url = f"{cfg['base_url']}/seedream-image/status"
        poll_headers = {"X-App-Id": cfg["app_id"], "X-Api-Key": cfg["api_key"]}
        deadline = time.time() + cfg["max_wait"]
        final_text = ""
        last_status = None

        while time.time() < deadline:
            time.sleep(cfg["poll_interval"])
            poll_resp = _request(session, "GET", status_url,
                                 params={"taskId": seedream_task_id},
                                 headers=poll_headers, timeout=30)
            if poll_resp.status_code != 200:
                raise _http_error(poll_resp)
            final_text = poll_resp.text

            try:
                final_data = json.loads(final_text)
            except ValueError:
                raise RuntimeError(f"状态响应不是 JSON，原始响应: {final_text[:ERROR_BODY_LIMIT]}")

            status = (final_data.get("data") or {}).get("status", "")
            if status != last_status:
                print(f"[Seedream] 任务状态: {status}")
                last_status = status
            if status == "success":
                break
            if status == "failed":
                raise RuntimeError(final_text[:ERROR_BODY_LIMIT])
            if (final_data.get("code") or 200) != 200:
                raise RuntimeError(final_text[:ERROR_BODY_LIMIT])

        else:
            raise TimeoutError(
                f"轮询超时（{cfg['max_wait']}s）任务仍在处理: {seedream_task_id}")

        # 3. 下载结果图（URL 仅 12h 有效）
        image_urls = (final_data.get("data") or {}).get("imageUrls") or []
        if not image_urls:
            raise RuntimeError(f"任务成功但未返回图片 URL，原始响应: {final_text[:ERROR_BODY_LIMIT]}")

        print(f"[Seedream] 下载 {len(image_urls)} 张结果图...")
        frames = [_url_to_tensor(u) for u in image_urls]
        image_batch = torch.cat(frames, dim=0)

        return (image_batch, submit_text, final_text)


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

NODE_CLASS_MAPPINGS = {
    "SeedreamAPIConfig": SeedreamAPIConfig,
    "SeedreamImage": SeedreamImage,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SeedreamAPIConfig": "Seedream Config (URL/Image)",
    "SeedreamImage": "Seedream Image (URL/Image)",
}
