"""管线结构测试 —— 保证架构契约不被破坏.

这些测试不调用任何付费 API, 秒级完成, 可放进 CI.
它们守护的是 AGENTS.md / ARCHITECTURE.md 里定义的架构约束。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

from anime_pv.adapters import ADAPTERS, build_adapter  # noqa: E402
from anime_pv.config import PipelineConfig, PlatformLimits  # noqa: E402
from anime_pv.errors import ConfigError, InputValidationError  # noqa: E402
from anime_pv.stages import PIPELINE, get_stage  # noqa: E402


class TestPipelineStructure:
    def test_all_stages_registered(self):
        assert PIPELINE == ["ingest", "character", "style_align", "transfer", "assemble"]

    def test_every_stage_instantiable(self):
        for name in PIPELINE:
            stage = get_stage(name)
            assert stage.name == name

    def test_unknown_stage_raises(self):
        with pytest.raises(Exception):
            get_stage("nonexistent")


class TestNoClipSpecificBranch:
    """NFR-1 的自动化守卫: stages 里不得出现针对具体片段的特判.

    这是"可复用性"从口头承诺变成可执行检查的关键一步。
    """

    def test_no_clip_id_equality_branch(self):
        stages_dir = SRC / "anime_pv" / "stages"
        patterns = ['clip_id == "A"', "clip_id == 'A'", 'clip_id == "B"', "clip_id == 'B'"]
        violations = []
        for py in stages_dir.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            for pat in patterns:
                if pat in text:
                    violations.append(f"{py.name}: {pat}")
        assert not violations, (
            "检测到片段特判分支, 违反可复用性要求 (题目 P1):\n" + "\n".join(violations)
        )


class TestPlatformLimits:
    """平台约束校验器的边界测试 —— 这些数字直接来自官方文档."""

    def setup_method(self):
        self.lim = PlatformLimits()

    def test_valid_video_passes(self):
        assert self.lim.check_video(6.0, 1280, 720, 20.0, ".mp4") == []

    def test_too_short_rejected(self):
        # 文档: 不小于 2s
        problems = self.lim.check_video(1.5, 1280, 720, 20.0, ".mp4")
        assert any("时长" in p for p in problems)

    def test_too_long_rejected(self):
        # 文档: 不大于 30s
        problems = self.lim.check_video(31.0, 1280, 720, 20.0, ".mp4")
        assert any("时长" in p for p in problems)

    def test_oversize_dimension_rejected(self):
        # 文档: 宽高都需在 [200, 2048]
        problems = self.lim.check_video(6.0, 4096, 720, 20.0, ".mp4")
        assert any("宽" in p for p in problems)

    def test_oversize_file_rejected(self):
        problems = self.lim.check_video(6.0, 1280, 720, 250.0, ".mp4")
        assert any("大小" in p for p in problems)

    def test_extreme_aspect_rejected(self):
        # 文档: 宽高比 1:3 - 3:1
        problems = self.lim.check_video(6.0, 2000, 400, 20.0, ".mp4")
        assert any("宽高比" in p for p in problems)

    def test_valid_image_passes(self):
        assert self.lim.check_image(1024, 1024, 2.0, ".png") == []

    def test_oversize_image_rejected(self):
        problems = self.lim.check_image(1024, 1024, 8.0, ".png")
        assert any("大小" in p for p in problems)


class TestAdapterRegistry:
    def test_mock_always_available(self):
        assert "mock" in ADAPTERS
        adapter = build_adapter("mock")
        assert adapter.name == "mock"

    def test_unknown_adapter_raises_config_error(self):
        with pytest.raises(ConfigError):
            build_adapter("does-not-exist")

    def test_dashscope_requires_config(self):
        with pytest.raises(ConfigError):
            build_adapter("dashscope", None)


class TestMockAdapterIsDeterministic:
    """Mock 必须确定性 —— 否则评估自检失去意义."""

    def test_same_input_same_task_id(self, tmp_path):
        from anime_pv.adapters.mock import MockAdapter

        img = tmp_path / "c.png"
        vid = tmp_path / "v.mp4"
        img.write_bytes(b"x")
        vid.write_bytes(b"y")
        a = MockAdapter(jitter=0.0)
        t1 = a.submit(img, vid, mode="wan-std")
        t2 = a.submit(img, vid, mode="wan-std")
        assert t1 == t2

    def test_missing_input_raises(self, tmp_path):
        from anime_pv.adapters.mock import MockAdapter

        a = MockAdapter()
        with pytest.raises(Exception):
            a.submit(tmp_path / "nope.png", tmp_path / "nope.mp4", mode="wan-std")


class TestStabilityMath:
    """L3 稳定性数学 —— 直接对应题目 "75分 > 特调90分" 的口径."""

    def test_consistent_beats_volatile_despite_lower_mean(self):
        from anime_pv.eval.l3_stability import StabilityEvaluator

        ev = StabilityEvaluator()
        # 场景: 稳定 75/75/76  vs  特调 95/95/45
        consistent = ev.analyze({
            "A": {"identity": 75.0}, "B": {"identity": 75.0}, "C": {"identity": 76.0},
        })
        volatile = ev.analyze({
            "A": {"identity": 95.0}, "B": {"identity": 95.0}, "C": {"identity": 45.0},
        })
        ci_c = consistent.overall["consistency_index"]
        ci_v = volatile.overall["consistency_index"]
        assert ci_c > ci_v, (
            f"题目口径要求稳定优先: 稳定组 CI={ci_c} 应高于特调组 CI={ci_v}"
        )

    def test_std_computed_correctly(self):
        from anime_pv.eval.l3_stability import StabilityEvaluator

        r = StabilityEvaluator().analyze({
            "A": {"d": 80.0}, "B": {"d": 70.0},
        })
        assert r.per_dim["d"]["mean"] == 75.0
        assert r.per_dim["d"]["std"] == 5.0
        assert r.per_dim["d"]["range"] == 10.0
        assert r.per_dim["d"]["worst"] == 70.0

    def test_single_clip_cannot_compute_stability(self):
        from anime_pv.eval.l3_stability import StabilityEvaluator

        r = StabilityEvaluator().analyze({"A": {"d": 80.0}})
        assert not r.overall.get("available")
        assert "无法计算" in r.note

    def test_missing_metric_excluded_not_zeroed(self):
        """诚实性: 未测到的维度不能被当成 0 分计入."""
        from anime_pv.eval.l3_stability import StabilityEvaluator

        r = StabilityEvaluator().analyze({
            "A": {"d": 80.0},
            "B": {"d": None},
            "C": {"d": 70.0},
        })
        assert r.per_dim["d"]["mean"] == 75.0  # 只用 A/C, 不是 (80+0+70)/3
