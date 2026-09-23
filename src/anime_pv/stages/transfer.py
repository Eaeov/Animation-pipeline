"""[阶段4] transfer —— 调用换人模型生成.

职责:
  1. 从上游阶段目录读取对齐后的人设图 + 归一化片段
  2. 通过适配器调用换人模型 (默认 dashscope / wan2.2-animate-mix)
  3. 落盘结果 + 记录计费秒数与 request_id

关键设计:
  - 素材必须先变成公网可访问 URL。百炼要求 HTTP(S) URL, 因此这里通过
    ``publisher`` 抽象处理。当前提供 DataUri(兜底) / Passthrough(已上传) 两种,
    OSS 上传见 adapters/cdn.py (需配置 AK), 属于扩展点。
  - 幂等: 同 run_id 重跑命中 manifest 则跳过, 避免重复扣费。
  - 计费统计写入 manifest, 便于最后核算 token/费用。
"""

from __future__ import annotations

from pathlib import Path

from ..context import RunContext, StageTimer, sha256_obj
from ..errors import StageError
from ..utils import image as I
from ..utils import video as V
from .base import StageResult


class TransferStage:
    name = "transfer"

    def _inputs(self, ctx: RunContext, clip_id: str) -> tuple[Path, Path]:
        a_dir = ctx.stage_dir(clip_id, "style_align")
        i_dir = ctx.stage_dir(clip_id, "ingest")
        aligned = a_dir / "character_aligned.png"
        if not aligned.exists():
            aligned = ctx.stage_dir(clip_id, "character") / "character.jpg"
        clip = i_dir / "clip.mp4"
        if not aligned.exists():
            raise StageError(
                f"未找到人设图产物, 请先运行 character/style_align 阶段",
                stage=self.name, clip_id=clip_id,
            )
        if not clip.exists():
            raise StageError(
                f"未找到片段产物 {clip}, 请先运行 ingest 阶段",
                stage=self.name, clip_id=clip_id,
            )
        return aligned, clip

    def run(self, ctx: RunContext, clip_id: str, force: bool = False) -> StageResult:
        if ctx.adapter is None:
            raise StageError("RunContext.adapter 为空, 无法执行生成", stage=self.name, clip_id=clip_id)

        image, clip = self._inputs(ctx, clip_id)
        v_info = V.probe(clip)
        params = {
            "adapter": ctx.adapter_name,
            "mode": ctx.mode,
            "model": ctx.cfg.api.model,
            "check_image": ctx.cfg.api.check_image,
        }
        input_hash = sha256_obj({
            **params,
            "image": I.info(image).to_dict(),
            "clip": v_info.to_dict(),
        })

        if hit := ctx.cached_ok(clip_id, self.name, input_hash, force):
            out = Path(hit["outputs"][0])
            return StageResult(self.name, clip_id, outputs=[out],
                               metadata=hit.get("extra", {}), reused=True)

        out_dir = ctx.stage_dir(clip_id, self.name)
        out = out_dir / "transferred.mp4"

        with StageTimer(ctx, clip_id, self.name, input_hash, params) as t:
            result = ctx.adapter.transfer(
                image, clip, mode=ctx.mode, dest=out,
                watermark=False,
            )
            if not out.exists():
                raise StageError(
                    f"适配器返回成功但产物不存在: {out}",
                    stage=self.name, clip_id=clip_id, request_id=result.task_id,
                )
            info = V.probe(out)
            t.outputs = [str(out.relative_to(ctx.run_dir))]
            t.extra = {
                "task_id": result.task_id,
                "request_id": result.request_id,
                "billable_seconds": result.billable_seconds,
                "estimated_cny": round(result.billable_seconds * _unit_price(ctx.mode), 3),
                "output_info": info.to_dict(),
                "raw_response": result.raw_response,
                "adapter": ctx.adapter_name,
            }
            # 存原始响应, 便于失败溯源
            (out_dir / "raw_response.json").write_text(
                __import__("json").dumps(result.raw_response, ensure_ascii=False, indent=2,
                                         default=str),
                encoding="utf-8",
            )

        return StageResult(self.name, clip_id, outputs=[out], metadata=t.extra)


def _unit_price(mode: str) -> float:
    """每秒钟单价 (元). 来自官方计费页, 变更时更新此表."""
    return 0.9 if mode == "wan-pro" else 0.6
