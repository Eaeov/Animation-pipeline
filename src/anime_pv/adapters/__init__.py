"""适配器注册表.

新增模型只需两步:
  1. 在 adapters/ 下新增实现 VideoTransferAdapter 协议的文件
  2. 在本文件 ADAPTERS 里登记

stages/ 通过 ``build_adapter(name, cfg)`` 获取实例, 因此永远不需要改动.
"""

from __future__ import annotations

from typing import Any, Callable

from ..config import ApiConfig
from ..errors import ConfigError
from .base import TaskStatus, TransferResult, VideoTransferAdapter
from .dashscope import DashScopeAnimateMixAdapter
from .mock import MockAdapter

ADAPTERS: dict[str, Callable[..., VideoTransferAdapter]] = {
    "dashscope": DashScopeAnimateMixAdapter,
    "mock": MockAdapter,
}


def build_adapter(name: str, cfg: ApiConfig | None = None, **kwargs: Any) -> VideoTransferAdapter:
    """按名字构造适配器实例."""
    if name not in ADAPTERS:
        raise ConfigError(
            f"未知适配器 {name!r}, 可用: {sorted(ADAPTERS)}",
            stage="adapter",
        )
    if name == "dashscope":
        if cfg is None:
            raise ConfigError("dashscope 适配器需要 ApiConfig", stage="adapter")
        return ADAPTERS[name](cfg, **kwargs)  # type: ignore[call-arg]
    return ADAPTERS[name](**kwargs)


__all__ = [
    "ADAPTERS",
    "build_adapter",
    "VideoTransferAdapter",
    "TransferResult",
    "TaskStatus",
    "DashScopeAnimateMixAdapter",
    "MockAdapter",
]
