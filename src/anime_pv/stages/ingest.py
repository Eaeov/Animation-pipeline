"""[阶段1] ingest —— 原始 PV 切片与归一化.

职责:
  1. 读 configs/clips.yaml 的切分方案
  2. 从原始 PV 裁出片段 (重编码保证时长精确)
  3. 归一化规格 (最长边/帧率/像素格式) —— 这是 A/B 可比性的基础
  4. 预检平台约束 (时长/分辨率/宽高比/大小), 不合规立即报错而非等 API 拒绝

不做: 不做内容理解, 不做人设图处理.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..config import REPO_ROOT
from ..context import RunContext, StageTimer, sha256_obj
from ..errors import ConfigError, InputValidationError
from ..utils import video as V
from .base import StageResult


class IngestStage:
    name = "ingest"

    # ------------------------------------------------------------ 切分方案

    def _plan(self) -> dict[str, Any]:
        path = REPO_ROOT / "configs" / "clips.yaml"
        if not path.exists():
            raise ConfigError(
                "缺少 configs/clips.yaml, 请先填写片段切分方案", stage=self.name
            )
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not data.get("clips"):
            raise ConfigError("configs/clips.yaml 的 clips 为空", stage=self.name)
        return data

    # ------------------------------------------------------------ 主流程

    def run(self, ctx: RunContext, clip_id: str, force: bool = False) -> StageResult:
        plan = self._plan()
        clip_cfg = plan["clips"].get(clip_id)
        if not clip_cfg:
            raise ConfigError(
                f"configs/clips.yaml 中没有片段 {clip_id!r}", stage=self.name, clip_id=clip_id
            )

        src_rel = (plan.get("source") or {}).get("video", "")
        src = (REPO_ROOT / src_rel) if not Path(src_rel).is_absolute() else Path(src_rel)

        params = {
            "start": float(clip_cfg.get("start", 0.0)),
            "duration": float(clip_cfg.get("duration", 6.0)),
            "normalize_max_side": 1280,
            "normalize_fps": 24,
            "source": str(src),
        }
        if not src.exists():
            raise InputValidationError(
                f"原始 PV 不存在: {src}。请放入 assets/source/ 并更新 configs/clips.yaml",
                stage=self.name, clip_id=clip_id,
            )

        input_hash = sha256_obj({**params, "src_hash": V.probe(src).to_dict()})
        if hit := ctx.cached_ok(clip_id, self.name, input_hash, force):
            return StageResult(self.name, clip_id,
                               outputs=[Path(p) for p in hit.get("outputs", [])],
                               metadata=hit.get("extra", {}), reused=True)

        out_dir = ctx.stage_dir(clip_id, self.name)
        raw_clip = out_dir / "clip_raw.mp4"
        norm_clip = out_dir / "clip.mp4"

        with StageTimer(ctx, clip_id, self.name, input_hash, params) as t:
            src_info = V.probe(src)
            if params["duration"] <= 0:
                raise InputValidationError("duration 必须 > 0", stage=self.name, clip_id=clip_id)
            if params["start"] + params["duration"] > src_info.duration + 0.05:
                raise InputValidationError(
                    f"片段 {clip_id} 超出源视频范围: "
                    f"start+duration={params['start'] + params['duration']:.2f}s > "
                    f"源时长 {src_info.duration:.2f}s",
                    stage=self.name, clip_id=clip_id,
                )

            V.cut(src, raw_clip, start=params["start"], duration=params["duration"], reencode=True)
            V.normalize(
                raw_clip, norm_clip,
                max_side=params["normalize_max_side"],
                fps=params["normalize_fps"],
            )

            info = V.probe(norm_clip)
            problems = ctx.cfg.limits.check_video(
                info.duration, info.width, info.height, info.size_mb, norm_clip.suffix
            )
            if problems:
                raise InputValidationError(
                    f"片段 {clip_id} 不满足平台约束: " + "; ".join(problems),
                    stage=self.name, clip_id=clip_id,
                )

            # 抽帧供后续评估与人工检视
            frames = V.extract_frames(norm_clip, out_dir / "frames", every_s=0.5, max_frames=60)

            # 保留中间产物作为可诊断证据
            raw_clip.unlink(missing_ok=True)

            t.outputs = [str(norm_clip.relative_to(ctx.run_dir))]
            t.extra = {
                "source_info": src_info.to_dict(),
                "clip_info": info.to_dict(),
                "frames": len(frames),
                "plan": {k: clip_cfg.get(k) for k in ("role", "complexity", "rationale")},
            }

        return StageResult(
            self.name, clip_id, outputs=[norm_clip], metadata=t.extra
        )
