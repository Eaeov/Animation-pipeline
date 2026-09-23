"""Stage 协议与公共数据结构."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..context import RunContext


@dataclass
class StageResult:
    """阶段执行结果 (轻量, 只传路径与元数据, 不传大对象)."""

    stage: str
    clip_id: str
    outputs: list[Path] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    reused: bool = False


@dataclass
class ClipArtifact:
    """一个片段的完整产物引用, 供评估阶段使用."""

    clip_id: str
    source: Path | None = None
    normalized: Path | None = None
    character: Path | None = None
    aligned_character: Path | None = None
    transferred: Path | None = None
    final: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            k: (str(v) if isinstance(v, Path) else v)
            for k, v in self.__dict__.items()
        }


@runtime_checkable
class Stage(Protocol):
    """阶段协议.

    约定:
      - ``run`` 必须幂等 (manifest 命中即返回缓存, force=True 强制重跑)
      - 只通过 RunContext 与文件系统交互
      - 失败抛 PipelineError 子类, 附带 stage/clip_id
    """

    name: str

    def run(self, ctx: RunContext, clip_id: str, force: bool = False) -> StageResult:
        ...
