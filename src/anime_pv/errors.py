"""统一异常体系.

设计原则: 任何失败都必须可诊断 —— 抛出时携带 stage / clip_id / request_id,
使日志和 manifest 都能定位到具体是哪一步、哪次 API 调用、哪个片段出的问题.
"""

from __future__ import annotations

from typing import Any


class PipelineError(Exception):
    """管线所有异常的基类.

    Attributes:
        stage: 出错的阶段名 (ingest / character / style_align / transfer / assemble / eval).
        clip_id: 出错的片段 ID (A / B / C).
        request_id: 外部 API 的 request_id, 便于去百炼控制台溯源.
        cause: 底层原始异常.
    """

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        clip_id: str | None = None,
        request_id: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.clip_id = clip_id
        self.request_id = request_id
        self.cause = cause

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "message": self.message,
            "stage": self.stage,
            "clip_id": self.clip_id,
            "request_id": self.request_id,
            "cause": repr(self.cause) if self.cause else None,
        }

    def __str__(self) -> str:
        parts = [self.message]
        if self.stage:
            parts.append(f"stage={self.stage}")
        if self.clip_id:
            parts.append(f"clip={self.clip_id}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " | ".join(parts)


class ConfigError(PipelineError):
    """配置缺失或非法 (例如未设置 DASHSCOPE_API_KEY)."""


class InputValidationError(PipelineError):
    """输入素材不满足平台约束 (时长/分辨率/宽高比/大小)."""


class AdapterError(PipelineError):
    """外部模型调用失败."""


class TaskFailedError(AdapterError):
    """异步任务返回 FAILED 状态."""


class TaskTimeoutError(AdapterError):
    """轮询超时仍未完成."""


class StageError(PipelineError):
    """阶段内部逻辑错误."""
