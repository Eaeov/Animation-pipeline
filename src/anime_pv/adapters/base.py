"""适配器协议定义.

所有外部模型调用的抽象层. stages/ 只依赖本文件的协议, 不依赖任何具体实现.
新增模型 = 新增一个实现该协议的文件 + 在 __init__.py 注册, 不动 stages/.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass
class TransferResult:
    """一次换人生成的结果."""

    video_path: Path
    task_id: str
    billable_seconds: float = 0.0
    mode: str = ""
    raw_response: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    reused: bool = False  # True 表示命中缓存未真实调用


@dataclass
class TaskStatus:
    """异步任务状态."""

    task_id: str
    status: str  # PENDING | RUNNING | SUCCEEDED | FAILED | CANCELED | UNKNOWN
    video_url: str | None = None
    billable_seconds: float = 0.0
    code: str | None = None
    message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.status in {"SUCCEEDED", "FAILED", "CANCELED"}


@runtime_checkable
class VideoTransferAdapter(Protocol):
    """视频换人适配器协议.

    只需实现 submit / poll 两个方法; transfer 有默认模板实现.
    """

    name: str

    def submit(
        self,
        image_path: Path,
        video_path: Path,
        *,
        mode: str,
        **kwargs: Any,
    ) -> str:
        """提交异步任务, 返回 task_id."""
        ...

    def poll(self, task_id: str) -> TaskStatus:
        """查询一次任务状态 (不阻塞)."""
        ...

    def download(self, url: str, dest: Path) -> Path:
        """下载结果视频."""
        ...

    def transfer(
        self,
        image_path: Path,
        video_path: Path,
        *,
        mode: str,
        dest: Path,
        **kwargs: Any,
    ) -> TransferResult:
        """模板方法: submit -> 轮询 -> 下载. 一般无需重写."""
        ...
