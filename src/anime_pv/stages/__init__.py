"""阶段注册表.

新增阶段: 新建 stages/<name>.py 实现 Stage 协议, 在 PIPELINE 与 _REGISTRY 登记.
阶段间必须解耦 —— 每个阶段都能通过 ``cli stage --stage X`` 独立运行.
"""

from __future__ import annotations

from .base import ClipArtifact, Stage, StageResult
from .assemble import AssembleStage
from .character import CharacterStage
from .ingest import IngestStage
from .style_align import StyleAlignStage
from .transfer import TransferStage

# 顺序即执行顺序, 也是 runs/ 目录前缀编号的依据
PIPELINE: list[str] = ["ingest", "character", "style_align", "transfer", "assemble"]

_REGISTRY: dict[str, Stage] = {
    "ingest": IngestStage(),
    "character": CharacterStage(),
    "style_align": StyleAlignStage(),
    "transfer": TransferStage(),
    "assemble": AssembleStage(),
}


def get_stage(name: str) -> Stage:
    if name not in _REGISTRY:
        from ..errors import StageError

        raise StageError(f"未知阶段 {name!r}, 可用: {PIPELINE}", stage=name)
    return _REGISTRY[name]


__all__ = ["PIPELINE", "get_stage", "Stage", "StageResult", "ClipArtifact"]
