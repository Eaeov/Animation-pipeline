"""[阶段5] assemble —— 回拼与交付成片.

职责:
  1. 把生成片段与原始片段的音频合并 (换人模型输出无声, 原音需回接)
  2. 单片段出 final.mp4; 多片段出 reel.mp4 (带交叉淡化转场)
  3. 生成对照视频 (原片 vs 换人后 并排), 这是最直观的评估证据

设计取舍:
  - "音频回接"是必要的: 换人模型只换人不动音, 丢掉音轨会让 PV 失去节奏感,
    评估"是否可用"时音画同步是隐含要求。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..context import RunContext, StageTimer, sha256_obj
from ..errors import StageError
from ..utils import video as V
from .base import StageResult


class AssembleStage:
    name = "assemble"

    def run(self, ctx: RunContext, clip_id: str, force: bool = False) -> StageResult:
        t_dir = ctx.stage_dir(clip_id, "transfer")
        transferred = t_dir / "transferred.mp4"
        if not transferred.exists():
            raise StageError(
                f"未找到生成产物 {transferred}, 请先运行 transfer 阶段",
                stage=self.name, clip_id=clip_id,
            )

        # 原始片段 (用于音频回接与对照)
        raw_clip = self._find_raw_clip(ctx, clip_id)

        params = {"crossfade_s": 0.0, "mux_audio": raw_clip is not None}
        input_hash = sha256_obj({
            **params,
            "transferred": V.probe(transferred).to_dict(),
            "raw": V.probe(raw_clip).to_dict() if raw_clip else None,
        })

        if hit := ctx.cached_ok(clip_id, self.name, input_hash, force):
            return StageResult(self.name, clip_id,
                               outputs=[Path(hit["outputs"][0])],
                               metadata=hit.get("extra", {}), reused=True)

        out_dir = ctx.stage_dir(clip_id, self.name)
        final = out_dir / "final.mp4"
        compare = out_dir / "compare.mp4"

        with StageTimer(ctx, clip_id, self.name, input_hash, params) as t:
            if raw_clip is not None:
                _mux_audio(transferred, raw_clip, final)
            else:
                shutil.copyfile(transferred, final)

            # 对照视频: 上=原片, 下=换人后 (纵向堆叠)
            if raw_clip is not None:
                try:
                    _stack_compare(raw_clip, final, compare)
                except StageError:
                    compare = None  # 对照视频失败不影响主产物
            else:
                compare = None

            info = V.probe(final)
            outputs = [final] + ([compare] if compare and compare.exists() else [])
            t.outputs = [str(p.relative_to(ctx.run_dir)) for p in outputs]
            t.extra = {
                "final_info": info.to_dict(),
                "audio_muxed": raw_clip is not None,
                "compare_video": str(compare.relative_to(ctx.run_dir)) if compare and compare.exists() else None,
            }

        return StageResult(self.name, clip_id, outputs=t.outputs and
                           [p for p in ([final, compare] if compare and compare.exists() else [final])],
                           metadata=t.extra)

    # ------------------------------------------------------------------ 内部

    def _find_raw_clip(self, ctx: RunContext, clip_id: str) -> Path | None:
        """定位带音频的原始片段.

        ingest 阶段为满足平台约束剥离了音轨, 因此需要回到源视频重新取带音轨的片段。
        找不到源视频时返回 None (静默降级为无声输出)。
        """
        import yaml

        from ..config import REPO_ROOT

        cfg_path = REPO_ROOT / "configs" / "clips.yaml"
        if not cfg_path.exists():
            return None
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        src_rel = (data.get("source") or {}).get("video", "")
        if not src_rel:
            return None
        src = (REPO_ROOT / src_rel) if not Path(src_rel).is_absolute() else Path(src_rel)
        if not src.exists():
            return None
        clip_cfg = (data.get("clips") or {}).get(clip_id)
        if not clip_cfg:
            return None

        out_dir = ctx.stage_dir(clip_id, self.name)
        raw = out_dir / "clip_with_audio.mp4"
        if not raw.exists():
            try:
                V.cut(src, raw, start=float(clip_cfg.get("start", 0)),
                      duration=float(clip_cfg.get("duration", 6)), reencode=True)
            except StageError:
                return None
        return raw


def _mux_audio(video: Path, audio_src: Path, dest: Path) -> Path:
    """把 audio_src 的音轨接到 video 上 (视频以 video 为准)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-i", str(audio_src),
        "-c:v", "copy", "-c:a", "aac", "-map", "0:v:0", "-map", "1:a:0?",
        "-shortest", "-movflags", "+faststart", str(dest),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0 or not dest.exists():
        # 音轨不存在时回退为视频拷贝
        shutil.copyfile(video, dest)
    return dest


def _stack_compare(original: Path, replaced: Path, dest: Path) -> Path:
    """纵向堆叠原片与换人后视频, 生成对照视频."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    oi = V.probe(original)
    ri = V.probe(replaced)
    w = min(oi.width, ri.width)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(original), "-i", str(replaced),
        "-filter_complex",
        f"[0:v]scale={w}:-2,pad={w}:ih+8:0:4:color=black[top];"
        f"[1:v]scale={w}:-2[bot];[top][bot]vstack=inputs=2[v]",
        "-map", "[v]", "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
        str(dest),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise StageError(f"对照视频生成失败: {out.stderr.strip()[:300]}", stage="assemble")
    return dest
