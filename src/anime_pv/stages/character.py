"""[阶段2] character —— 人设素材规格化.

职责:
  1. 定位人设图 (assets/character/<clip_id>.* 或 char_<clip_id>.png 等命名约定)
  2. 规整到平台约束 (尺寸/宽高比/大小/格式)
  3. 记录原始信息与变换过程, 供复现

不做: 不做画幅对齐 (那是阶段3), 不做人脸检测 (留作扩展点).

命名约定 (二选一, 便于 A/B 各自指定人设):
  assets/character/A.png / B.png        <- 推荐
  assets/character/char_A.png
若只有一个通用人设, 则 assets/character/char.png 对所有 clip 生效.
"""

from __future__ import annotations

from pathlib import Path

from ..context import RunContext, StageTimer, sha256_obj
from ..errors import InputValidationError
from ..utils import image as I
from .base import StageResult

_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


class CharacterStage:
    name = "character"

    def _find(self, ctx: RunContext, clip_id: str) -> Path:
        base = ctx.cfg.assets_dir / "character"
        if not base.exists():
            raise InputValidationError(
                f"人设图目录不存在: {base}", stage=self.name, clip_id=clip_id
            )
        candidates = [
            base / f"{clip_id}.png", base / f"{clip_id}.jpg", base / f"{clip_id}.jpeg",
            base / f"{clip_id}.webp",
            base / f"char_{clip_id}.png", base / f"char_{clip_id}.jpg",
            base / f"char.png", base / f"char.jpg",
        ]
        for c in candidates:
            if c.exists():
                return c
        found = sorted(p for p in base.iterdir() if p.suffix.lower() in _IMG_EXT)
        if len(found) == 1:
            return found[0]
        raise InputValidationError(
            f"片段 {clip_id} 找不到人设图。期望 {base}/{clip_id}.png "
            f"(或 char.png 作为通用人设)。当前目录有: {[p.name for p in found]}",
            stage=self.name, clip_id=clip_id,
        )

    def run(self, ctx: RunContext, clip_id: str, force: bool = False) -> StageResult:
        src = self._find(ctx, clip_id)
        params = {"max_side": 2048, "min_side": 256, "max_mb": 5.0}
        src_info = I.info(src)
        input_hash = sha256_obj({**params, "src": src_info.to_dict()})

        if hit := ctx.cached_ok(clip_id, self.name, input_hash, force):
            out = Path(hit["outputs"][0])
            return StageResult(self.name, clip_id, outputs=[out],
                               metadata=hit.get("extra", {}), reused=True)

        out_dir = ctx.stage_dir(clip_id, self.name)
        out = out_dir / "character.jpg"

        with StageTimer(ctx, clip_id, self.name, input_hash, params) as t:
            I.normalize(
                src, out,
                max_side=params["max_side"], min_side=params["min_side"],
                max_mb=params["max_mb"],
            )
            after = I.info(out)
            problems = ctx.cfg.limits.check_image(
                after.width, after.height, after.size_mb, out.suffix
            )
            if problems:
                raise InputValidationError(
                    f"人设图规整后仍不合规: " + "; ".join(problems),
                    stage=self.name, clip_id=clip_id,
                )
            t.outputs = [str(out.relative_to(ctx.run_dir))]
            t.extra = {"source": src_info.to_dict(), "normalized": after.to_dict()}

        return StageResult(self.name, clip_id, outputs=[out], metadata=t.extra)
