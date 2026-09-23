"""评估基础协议."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class Metric:
    """单个评估指标.

    value 统一归一到 0-100, 便于跨指标聚合 (越高越好)。
    不适合归一化的原始值放 raw_value。
    """

    dim: str
    layer: str
    value: float | None
    raw_value: float | None = None
    detail: dict = field(default_factory=dict)
    evidence: list[Path] = field(default_factory=list)
    note: str = ""

    @property
    def measured(self) -> bool:
        return self.value is not None

    def to_dict(self) -> dict:
        return {
            "dim": self.dim,
            "layer": self.layer,
            "value": None if self.value is None else round(float(self.value), 2),
            "raw_value": self.raw_value,
            "detail": self.detail,
            "evidence": [str(p) for p in self.evidence],
            "note": self.note,
        }


@dataclass
class ClipPair:
    """一对"参考 vs 生成"素材, 供评估器消费."""

    clip_id: str
    reference: Path | None      # 原始片段 (归一化后)
    generated: Path | None      # 换人后成片
    character: Path | None = None   # 人设图
    aligned: Path | None = None     # 对齐后人设图
    ref_frames: list[Path] = field(default_factory=list)
    gen_frames: list[Path] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(self.reference and self.generated
                    and self.reference.exists() and self.generated.exists())


@runtime_checkable
class Evaluator(Protocol):
    """评估器协议. 无法计算时返回 value=None, 不要用 0 冒充."""

    dim: str
    layer: str

    def score(self, pair: ClipPair, *, out_dir: Path) -> Metric:
        ...
