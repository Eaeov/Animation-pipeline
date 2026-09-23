"""评估器自检 —— 保证评估方案本身可信.

这是本评估方案设计中最关键的一环:
  用 MockAdapter 制造**已知缺陷**, 验证评估器能否检出。

如果评估器对人为制造的模糊/闪烁毫无反应, 说明评估器是坏的,
那么所有分数都不可信 —— 这比"评估分数低"严重得多。

运行: pytest tests/test_eval_sanity.py -v
"""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from anime_pv.eval.base import ClipPair  # noqa: E402
from anime_pv.eval.l1_technical import (  # noqa: E402
    SharpnessEvaluator,
    TemporalFlickerEvaluator,
)


def _make_video(path, *, frames=48, size=(320, 180), fps=24.0,
                flicker=0.0, blur=1, seed=0) -> None:
    """生成测试视频.

    Args:
        flicker: 每帧交替亮度偏移幅度 (0 = 无闪烁)
        blur: 高斯模糊核大小 (1 = 不模糊)
    """
    rng = np.random.default_rng(seed)
    w, h = size
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    base = rng.integers(60, 200, (h, w, 3), dtype=np.uint8)
    for i in range(frames):
        f = base.copy()
        cx = 30 + int(i * (w - 60) / frames)
        cv2.circle(f, (cx, h // 2), 22, (230, 180, 140), -1)
        cv2.rectangle(f, (cx - 8, h // 2 + 20), (cx + 8, h - 4), (80, 90, 150), -1)
        if blur > 1:
            f = cv2.GaussianBlur(f, (blur, blur), 0)
        if flicker:
            shift = int(flicker * ((i % 2) * 2 - 1))
            f = cv2.convertScaleAbs(f, alpha=1.0, beta=shift)
        writer.write(f)
    writer.release()


@pytest.fixture()
def tmp_clips(tmp_path):
    """构造: 参考视频 / 干净生成 / 闪烁生成 / 模糊生成."""
    ref = tmp_path / "ref.mp4"
    clean = tmp_path / "clean.mp4"
    flick = tmp_path / "flicker.mp4"
    blurry = tmp_path / "blurry.mp4"
    _make_video(ref, flicker=0.0, blur=1)
    _make_video(clean, flicker=0.0, blur=1)
    _make_video(flick, flicker=1.0, blur=1)      # 亮度 ±40 交替 -> 强闪烁
    _make_video(blurry, flicker=0.0, blur=21)    # 强模糊
    return {"ref": ref, "clean": clean, "flicker": flick, "blurry": blurry}


class TestTemporalFlickerDetectsDefect:
    """核心自检: 时序闪烁指标必须能检出厂造的闪烁."""

    def test_clean_scores_higher_than_flicker(self, tmp_clips, tmp_path):
        ev = TemporalFlickerEvaluator()
        clean = ev.score(
            ClipPair("A", tmp_clips["ref"], tmp_clips["clean"]), out_dir=tmp_path
        )
        flick = ev.score(
            ClipPair("A", tmp_clips["ref"], tmp_clips["flicker"]), out_dir=tmp_path
        )
        assert clean.value is not None and flick.value is not None
        assert clean.value > flick.value, (
            f"评估器失效: 干净视频 ({clean.value}) 未高于闪烁视频 ({flick.value})"
        )
        # 干净视频应接近满分
        assert clean.value > 90, f"干净视频得分偏低: {clean.value}"
        # 闪烁视频应显著下降
        assert flick.value < clean.value - 20, (
            f"对闪烁的惩罚不足: {clean.value} -> {flick.value}"
        )


class TestSharpnessDetectsBlur:
    """锐度指标必须能检出厂造的模糊."""

    def test_clean_scores_higher_than_blurry(self, tmp_clips, tmp_path):
        ev = SharpnessEvaluator()
        clean = ev.score(
            ClipPair("A", tmp_clips["ref"], tmp_clips["clean"]), out_dir=tmp_path
        )
        blurry = ev.score(
            ClipPair("A", tmp_clips["ref"], tmp_clips["blurry"]), out_dir=tmp_path
        )
        assert clean.value is not None and blurry.value is not None
        assert clean.value > blurry.value, (
            f"评估器失效: 干净 ({clean.value}) 未高于模糊 ({blurry.value})"
        )


class TestNotMeasuredIsHonest:
    """缺少素材时必须标记 not_measured, 不能用 0 分冒充."""

    def test_missing_input_returns_none(self, tmp_path):
        ev = TemporalFlickerEvaluator()
        m = ev.score(ClipPair("A", None, None), out_dir=tmp_path)
        assert m.value is None
        assert m.note, "标记 not_measured 时必须给出原因"
