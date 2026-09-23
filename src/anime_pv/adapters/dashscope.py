"""百炼 wan2.2-animate-mix 视频换人适配器.

这是本管线的核心模型适配器. 关键实现点:
- 输入素材必须公网可访问 URL => 本适配器负责 OSS/图床上传中转 (见 _publish)
- 异步任务两步式: submit 拿 task_id -> poll 轮询
- 结果 URL 仅 24h 有效 => 立即下载转存
- 失败不扣费 => 重试策略可以相对激进
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import requests

from ..config import ApiConfig
from ..errors import AdapterError, ConfigError, TaskFailedError, TaskTimeoutError
from .base import TaskStatus, TransferResult


class DashScopeAnimateMixAdapter:
    """万相-视频换人 (wan2.2-animate-mix) 适配器.

    Args:
        cfg: API 配置 (含 base_url / model / 轮询参数).
        publisher: 可选的素材发布器, 把本地文件变成公网 URL.
            默认实现为 ``DataUriPublisher`` (base64 data uri), 若平台不接受,
            请换用 ``OssPublisher`` (见 cdn.py).
    """

    name = "dashscope"

    def __init__(self, cfg: ApiConfig, publisher: Any | None = None) -> None:
        self.cfg = cfg
        self.publisher = publisher or DataUriPublisher()
        self._session = requests.Session()

    # ------------------------------------------------------------------ 内部

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }

    def _url(self, path: str) -> str:
        return self.cfg.base_url.rstrip("/") + path

    def _post_with_retry(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        last_exc: BaseException | None = None
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                resp = self._session.post(
                    url, headers=self._headers(), json=payload, timeout=60
                )
                data = resp.json()
                if resp.status_code == 200:
                    return data
                # 4xx 鉴权/参数类错误不重试
                if 400 <= resp.status_code < 500:
                    raise AdapterError(
                        f"提交任务失败 HTTP {resp.status_code}: {data}",
                        stage="transfer",
                        request_id=data.get("request_id"),
                    )
                last_exc = AdapterError(f"HTTP {resp.status_code}: {data}", stage="transfer")
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
            if attempt < self.cfg.max_retries:
                time.sleep(2 ** attempt)  # 指数退避
        raise AdapterError(
            f"提交任务重试 {self.cfg.max_retries} 次仍失败: {last_exc}",
            stage="transfer",
            cause=last_exc if isinstance(last_exc, BaseException) else None,
        )

    # ------------------------------------------------------------------ 协议

    def submit(
        self,
        image_path: Path,
        video_path: Path,
        *,
        mode: str = "wan-std",
        watermark: bool = False,
        **kwargs: Any,
    ) -> str:
        if not self.cfg.has_key:
            raise ConfigError("DASHSCOPE_API_KEY 未设置", stage="transfer")

        image_url = self.publisher.publish(image_path)
        video_url = self.publisher.publish(video_path)

        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "input": {
                "image_url": image_url,
                "video_url": video_url,
                "watermark": watermark,
            },
            "parameters": {
                "mode": mode,
                "check_image": self.cfg.check_image,
            },
        }
        data = self._post_with_retry(self._url(self.cfg.submit_path), payload)
        output = data.get("output", {})
        task_id = output.get("task_id")
        if not task_id:
            raise AdapterError(
                f"响应中缺少 task_id: {data}",
                stage="transfer",
                request_id=data.get("request_id"),
            )
        return str(task_id)

    def poll(self, task_id: str) -> TaskStatus:
        url = self._url(self.cfg.query_path.format(task_id=task_id))
        try:
            resp = self._session.get(
                url,
                headers={"Authorization": f"Bearer {self.cfg.api_key}"},
                timeout=30,
            )
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise AdapterError(f"查询任务失败: {exc}", stage="transfer", cause=exc)

        out = data.get("output", {}) or {}
        usage = data.get("usage", {}) or {}
        results = out.get("results", {}) or {}
        return TaskStatus(
            task_id=task_id,
            status=str(out.get("task_status", "UNKNOWN")),
            video_url=results.get("video_url"),
            billable_seconds=float(usage.get("video_duration", 0.0) or 0.0),
            code=out.get("code") or data.get("code"),
            message=out.get("message") or data.get("message"),
            raw=data,
        )

    def download(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._session.get(url, stream=True, timeout=300) as resp:
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=8192):
                        fh.write(chunk)
        except requests.RequestException as exc:
            raise AdapterError(f"下载结果视频失败: {exc}", stage="transfer", cause=exc)
        return dest

    def wait(self, task_id: str) -> TaskStatus:
        """阻塞轮询至终态. 视频生成约需数分钟, 建议间隔 15s."""
        deadline = time.time() + self.cfg.poll_timeout_s
        last: TaskStatus | None = None
        while time.time() < deadline:
            last = self.poll(task_id)
            if last.done:
                return last
            time.sleep(self.cfg.poll_interval_s)
        raise TaskTimeoutError(
            f"任务 {task_id} 超过 {self.cfg.poll_timeout_s}s 未完成"
            f"(最后状态 {last.status if last else 'unknown'})",
            stage="transfer",
            request_id=task_id,
        )

    def transfer(
        self,
        image_path: Path,
        video_path: Path,
        *,
        mode: str = "wan-std",
        dest: Path,
        **kwargs: Any,
    ) -> TransferResult:
        task_id = self.submit(image_path, video_path, mode=mode, **kwargs)
        status = self.wait(task_id)
        if status.status != "SUCCEEDED" or not status.video_url:
            raise TaskFailedError(
                f"任务失败 [{status.code}] {status.message}",
                stage="transfer",
                request_id=task_id,
            )
        self.download(status.video_url, dest)
        return TransferResult(
            video_path=dest,
            task_id=task_id,
            billable_seconds=status.billable_seconds,
            mode=mode,
            raw_response=status.raw,
        )


class DataUriPublisher:
    """把本地文件编码为 data URI 的发布器 (离线兜底).

    注意: 百炼文档要求公网 HTTP(S) URL, data URI 可能不被接受.
    真机联调若报参数错误, 请改用 ``cdn.OssPublisher``.
    """

    name = "data-uri"

    def publish(self, path: Path) -> str:
        raw = path.read_bytes()
        mime = _guess_mime(path)
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


class PassthroughPublisher:
    """已上传到公网的素材直接透传 URL, 不重复上传."""

    name = "passthrough"

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    def publish(self, path: Path) -> str:
        key = path.name
        if key not in self.mapping:
            raise ConfigError(f"素材 {key} 没有对应的公网 URL 映射", stage="transfer")
        return self.mapping[key]


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(ext, "application/octet-stream")
