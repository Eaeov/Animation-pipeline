"""PV 分析模块测试 —— `scan` 命令的正确性.

为什么这些测试重要:
    scan 是"素材就绪后的第一步", 它推荐的切片方案**直接决定**
    后续所有换人结果的质量。如果它把剧烈运动/噪点镜头推荐为 baseline,
    后面做得再对也是白搭。

    这里用**合成的多镜头 PV** 验证三件事:
      1. 能正确检测出镜头切换 (不多不少)
      2. 能把"有主体"和"无主体(噪点/空镜)"区分开
      3. A/B/C 的推荐逻辑符合题面要求 (A/B 同规格, C 是压力测试)

运行: pytest tests/test_analyze.py -v
"""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from anime_pv.analyze import (  # noqa: E402
    Shot,
    _subject_score,
    detect_shots,
    measure_shot,
    scan,
    suggest_scheme,
)


# --------------------------------------------------------------------- 素材构造


def _draw_subject(frame: np.ndarray, cx: int, cy: int, r: int) -> None:
    """画一个位于中央、有明确明暗层次的主体 (模拟角色)."""
    h = frame.shape[0]
    cv2.ellipse(frame, (cx, cy), (r, int(r * 1.25)), 0, 0, 360, (200, 190, 175), -1)
    cv2.circle(frame, (cx - int(r * 0.4), cy - int(r * 0.22)), max(2, r // 6), (30, 30, 30), -1)
    cv2.circle(frame, (cx + int(r * 0.4), cy - int(r * 0.22)), max(2, r // 6), (30, 30, 30), -1)
    cv2.ellipse(frame, (cx, cy + int(r * 0.45)), (max(3, r // 3), max(2, r // 8)),
                0, 0, 180, (80, 55, 55), -1)
    cv2.rectangle(frame, (cx - int(r * 1.1), cy + int(r * 1.2)), (cx + int(r * 1.1), h),
                  (150, 110, 90), -1)


@pytest.fixture()
def multi_shot_pv(tmp_path):
    """造一支 4 镜头 PV: 3 个静止/慢推(有主体) + 1 个剧烈噪点(无主体)."""
    path = tmp_path / "pv.mp4"
    w, h, fps = 480, 270, 24
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    rng = np.random.default_rng(7)

    # 镜头 1: 静止, 主体大 (2.5s = 60帧)
    for _ in range(60):
        f = np.full((h, w, 3), 60, dtype=np.uint8)
        cv2.rectangle(f, (0, 0), (w, 50), (45, 60, 90), -1)
        _draw_subject(f, 240, 125, 52)
        vw.write(f)
    # 镜头 2: 静止, 主体中等, 背景不同 (3s)
    for _ in range(72):
        f = np.full((h, w, 3), 130, dtype=np.uint8)
        cv2.rectangle(f, (0, h - 60), (w, h), (90, 120, 70), -1)
        _draw_subject(f, 225, 135, 36)
        vw.write(f)
    # 镜头 3: 慢推, 主体由小变大 (3s)
    for i in range(72):
        f = np.full((h, w, 3), 35, dtype=np.uint8)
        _draw_subject(f, 248, 130, 26 + i // 8)
        vw.write(f)
    # 镜头 4: 剧烈噪点, 无主体 (3s)
    for i in range(72):
        f = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
        cv2.circle(f, (int(40 + i * 6) % w, 80 + (i * 13) % 130), 34, (250, 250, 250), -1)
        vw.write(f)

    vw.release()
    return path


# --------------------------------------------------------------------- 测试


class TestShotDetection:
    """镜头检测必须准确 —— 少检会切在镜头中间, 多检会产生碎片段."""

    def test_detects_all_four_shots(self, multi_shot_pv):
        shots = detect_shots(multi_shot_pv)
        # 4 个镜头, 允许 ±1 的边界抖动
        assert 3 <= len(shots) <= 5, (
            f"镜头数异常: 期望约 4, 实得 {len(shots)} -> "
            f"{[(round(s.start,2), round(s.end,2)) for s in shots]}"
        )

    def test_shots_are_contiguous_and_ordered(self, multi_shot_pv):
        shots = detect_shots(multi_shot_pv)
        for i in range(1, len(shots)):
            # 时间上必须单调递增且首尾相接
            assert shots[i].start >= shots[i - 1].end - 0.15, (
                f"镜头 {i-1}->{i} 有重叠或乱序"
            )
        assert shots[0].start == pytest.approx(0.0, abs=0.2)


class TestSubjectScore:
    """显著性代理必须能区分"有主体"与"噪点/空镜"."""

    def test_plain_frame_has_no_subject(self):
        f = np.full((270, 480, 3), 128, dtype=np.uint8)
        has, area = _subject_score(f)
        assert not has, "纯色空镜不应被判定为有主体"

    def test_noise_frame_rejected(self):
        """回归: 全屏噪点曾拿到 73 分 / 34.8% 主体占比 —— 必须被抑制."""
        rng = np.random.default_rng(1)
        f = rng.integers(0, 255, (270, 480, 3), dtype=np.uint8)
        has, _ = _subject_score(f)
        assert not has, "全屏噪点画面必须判为无主体 (噪声抑制判据失效)"

    def test_centered_subject_detected(self):
        f = np.full((270, 480, 3), 60, dtype=np.uint8)
        _draw_subject(f, 240, 125, 55)
        has, area = _subject_score(f)
        assert has, "中央有明确主体时未能检出"
        assert area > 0, "检出主体但占比为 0"


class TestSuitabilityScoring:
    """剧烈运动的镜头不该被推荐为 baseline."""

    def test_high_motion_scores_low(self, multi_shot_pv):
        from anime_pv.analyze import detect_shots as ds

        shots = ds(multi_shot_pv)
        ms = [measure_shot(multi_shot_pv, s) for s in shots]
        noisy = max(ms, key=lambda m: m.motion)          # 噪点镜头
        calm = min(ms, key=lambda m: m.motion)           # 最静止的镜头
        assert noisy.suitability < calm.suitability, (
            f"拒检失效: 剧烈运动镜头 {noisy.suitability} 未低于静止镜头 "
            f"{calm.suitability}"
        )
        assert noisy.suitability <= 30, (
            f"剧烈运动镜头适配度仍偏高: {noisy.suitability}"
        )


class TestSuggestScheme:
    """A/B/C 推荐逻辑必须符合题面: A/B 同规格, C 是压力测试."""

    def test_scheme_roles_are_correct(self, multi_shot_pv):
        result = scan(multi_shot_pv)
        sug = result.suggested
        assert "A" in sug and "B" in sug, "必须推荐出 A 与 B (题面要求 ≥2 片段)"
        assert sug["A"]["role"] == "baseline"
        assert sug["B"]["role"] == "baseline"
        if "C" in sug:
            assert sug["C"]["role"] == "stress", "C 必须是压力测试角色"

    def test_all_durations_within_platform_limits(self, multi_shot_pv):
        result = scan(multi_shot_pv)
        for cid, v in result.suggested.items():
            d = v["duration"]
            assert 2.0 < d <= 30.0, f"片段 {cid} 时长 {d}s 违反平台约束 2s<d≤30s"
            assert v["start"] >= 0

    def test_baseline_differs_from_stress_in_motion(self, multi_shot_pv):
        """C 的运动量应显著高于 A —— 否则压力测试没有意义."""
        result = scan(multi_shot_pv)
        if "C" not in result.suggested:
            pytest.skip("未推荐 C 片段")
        by_idx = {m.shot.index: m for m in result.shots}
        a_m = by_idx[result.suggested["A"]["_shot_index"]].motion
        c_m = by_idx[result.suggested["C"]["_shot_index"]].motion
        assert c_m > a_m, f"压力样本运动量 {c_m} 未高于基线 {a_m}"

    def test_empty_input_returns_warning(self):
        sug, warns = suggest_scheme([])
        assert sug == {}
        assert warns, "无候选镜头时必须给出提示"