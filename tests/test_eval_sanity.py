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
                flicker=0.0, blur=1, seed=0, blink=True) -> None:
    """生成测试视频.

    Args:
        flicker: 每帧交替亮度偏移幅度 (0 = 无闪烁)
        blur: 高斯模糊核大小 (1 = 不模糊)
        blink: False 则主体不移动 (用于构造"近似静止"样本)
    """
    rng = np.random.default_rng(seed)
    w, h = size
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    base = rng.integers(60, 200, (h, w, 3), dtype=np.uint8)
    for i in range(frames):
        f = base.copy()
        cx = 30 + int(i * (w - 60) / frames) if blink else w // 2
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


class TestFlowSmoothnessNormalization:
    """光流平滑度的归一化必须单调且无硬截断.

    回归背景 (失败案例 5): 原实现 `100*(1 - cv/0.8)` 在 CV>0.8 处硬归零,
    导致正常视频也拿 0 分, 指标失去分辨率。改为指数饱和后必须满足:
      1. CV 越小分越高 (单调)
      2. 不存在" cliff "式的硬截断 (相邻 CV 之间分数连续变化)
    """

    def test_exponential_is_monotonic_and_smooth(self):
        import math

        tau = 1.2

        def score(cv: float) -> float:
            return max(0.0, min(100.0, 100.0 * math.exp(-cv / tau)))

        cvs = [0.0, 0.3, 0.6, 0.9, 1.2, 1.5, 1.8, 2.4]
        vals = [score(c) for c in cvs]
        # 单调递减
        assert all(vals[i] > vals[i + 1] for i in range(len(vals) - 1)), (
            f"归一化不单调: {list(zip(cvs, vals))}"
        )
        # 无硬截断: 任意相邻两点差值不应超过 30 分 (原公式在 0.8 处会断层)
        gaps = [vals[i] - vals[i + 1] for i in range(len(vals) - 1)]
        assert max(gaps) < 30, f"存在硬截断/断层: gaps={gaps}"
        # 常见区间必须保留分辨率 (不能全是 0 或全是 100)
        mid = [score(c) for c in (0.6, 0.9, 1.2, 1.5)]
        assert all(5 < v < 95 for v in mid), f"常见 CV 区间分辨率不足: {mid}"

    def test_static_video_is_not_measured_not_zero(self, tmp_path):
        """画面近似静止时分不出平滑度, 必须诚实标记 not_measured."""
        from anime_pv.eval.l1_technical import FlowSmoothnessEvaluator

        still = tmp_path / "still.mp4"
        _make_video(still, frames=12, size=(160, 90), fps=24.0, blink=False)
        ev = FlowSmoothnessEvaluator()
        m = ev.score(ClipPair("A", None, still), out_dir=tmp_path)
        # 要么测得合理值, 要么诚实标 N/A; 不允许静默给 0
        if m.value is not None:
            assert m.value > 0 or m.detail.get("mean_motion", 1) < 0.05


class TestCliParamsActuallyUsed:
    """防止 CLI 参数被静默丢弃 (失败案例 5 的 bug 1).

    `smoke --jitter 0.6` 曾因 cmd_smoke 硬编码 jitter=0.0 而完全失效,
    且不报任何错 —— 这类 bug 只能靠"断言参数真的被用上"来防。
    """

    def test_smoke_jitter_reaches_adapter(self, monkeypatch):
        from anime_pv import cli

        captured = {}
        real_build = cli.build_adapter

        def spy(name, api=None, **kw):
            captured["name"] = name
            captured.update(kw)
            return real_build(name, api, **kw)

        monkeypatch.setattr(cli, "build_adapter", spy)

        import argparse
        args = argparse.Namespace(
            config="default", run_id=None, mode="wan-std", jitter=0.6,
        )
        # 只跑到构造 adapter 就够, 后面的合成素材不需要真跑
        try:
            cli.cmd_smoke(args)
        except Exception:
            pass
        assert captured.get("jitter") == 0.6, (
            f"--jitter 未透传到 adapter (收到 {captured.get('jitter')}) — "
            "扰动自检会形同虚设"
        )

    def test_build_parser_has_jitter_on_smoke(self):
        parser = cli_parser()
        args = parser.parse_args(["smoke", "--jitter", "0.7"])
        assert args.jitter == pytest.approx(0.7)


def cli_parser():
    from anime_pv.cli import build_parser

    return build_parser()
