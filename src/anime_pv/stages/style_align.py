"""[阶段3] style_align —— 人设图与目标片段画幅对齐.

为什么单独设一个阶段 (而不是塞进 character):
  官方 FAQ 明确指出 —— "确保输入图片与参考视频中人物画幅占比相似" 是
  提升换人质量的首要建议; "画幅不匹配是最常见的弱结果原因".
  这意味着: 如果不对齐, A 片段可能因为巧合匹配而高分, B 片段崩掉 ——
  直接违反本题 P1 (可复用 > 单片段高分).

  因此把"对齐"做成显式的、对 A/B 一视同仁的独立步骤, 是本题设计取舍的核心体现.

职责:
  按参考视频的宽高比, 把人设图等比缩放后补边到同比例画幅.
  只做画幅对齐 (外部约束), 不做人物检测裁剪 (留作扩展点, 见 ARCHITECTURE 第6节).
"""

from __future__ import annotations

from pathlib import Path

from ..context import RunContext, StageTimer, sha256_obj
from ..errors import StageError
from ..utils import image as I
from ..utils import video as V
from .base import StageResult


class StyleAlignStage:
    name = "style_align"

    def _inputs(self, ctx: RunContext, clip_id: str) -> tuple[Path, Path]:
        """从上游阶段目录读取产物 (不依赖内存态, 保证可独立运行)."""
        c_dir = ctx.stage_dir(clip_id, "character")
        i_dir = ctx.stage_dir(clip_id, "ingest")
        char = c_dir / "character.jpg"
        clip = i_dir / "clip.mp4"
        if not char.exists():
            raise StageError(
                f"未找到人设图产物 {char}, 请先运行 character 阶段",
                stage=self.name, clip_id=clip_id,
            )
        if not clip.exists():
            raise StageError(
                f"未找到片段产物 {clip}, 请先运行 ingest 阶段",
                stage=self.name, clip_id=clip_id,
            )
        return char, clip

    def run(self, ctx: RunContext, clip_id: str, force: bool = False) -> StageResult:
        char, clip = self._inputs(ctx, clip_id)
        v_info = V.probe(clip)
        params = {"canvas": [v_info.width, v_info.height], "pad": "white-center"}
        input_hash = sha256_obj({
            **params,
            "char": I.info(char).to_dict(),
            "clip": v_info.to_dict(),
        })

        if hit := ctx.cached_ok(clip_id, self.name, input_hash, force):
            return StageResult(self.name, clip_id,
                               outputs=[Path(hit["outputs"][0])],
                               metadata=hit.get("extra", {}), reused=True)

        out_dir = ctx.stage_dir(clip_id, self.name)
        out = out_dir / "character_aligned.png"

        with StageTimer(ctx, clip_id, self.name, input_hash, params) as t:
            I.align_to_clip(char, clip, out, canvas=(v_info.width, v_info.height))
            after = I.info(out)
            # 对齐后仍需满足平台图片约束
            problems = ctx.cfg.limits.check_image(
                after.width, after.height, after.size_mb, ".png"
            )
            if problems:
                raise StageError(
                    "对齐后的人设图不合规: " + "; ".join(problems),
                    stage=self.name, clip_id=clip_id,
                )
            t.outputs = [str(out.relative_to(ctx.run_dir))]
            t.extra = {
                "clip_canvas": [v_info.width, v_info.height],
                "aligned": after.to_dict(),
                "note": "按参考视频宽高比补边, 提升换人质量并保证 A/B 条件一致",
            }

        return StageResult(self.name, clip_id, outputs=[out], metadata=t.extra)
