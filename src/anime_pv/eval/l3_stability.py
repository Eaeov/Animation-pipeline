"""L3 稳定性评估 —— 本题的胜负手.

## 为什么需要这一层

题目原文:
> 侧重 pipeline 的可复用和稳定性, 对片段 A 和片段 B 用同样的 pipeline
> 都跑出 75 分的效果 > 对片段 A/片段 B 特调出 90 分的效果

这意味着: **单片段高分没有价值, 跨片段一致性才有价值。**
前两层 (L1/L2) 只能算出"片段 X 得了多少分", 无法回答"这套 pipeline 稳不稳"。
所以必须有一层专门度量"同一 pipeline 在不同片段上的表现离散度"。

## 度量设计

1. **跨片段方差 / 标准差 (std)**
   同一指标在 A/B/C 上的分数标准差。std 越小越稳。
   直接对应题目的"同样的 pipeline 都跑出 75 分"。

2. **最差片段分 (worst_clip)**
   木桶效应: 特调 pipeline 的典型特征是"平均分高但最差片段崩"。
   报 worst 比报 mean 更诚实, 也更能暴露过拟合。

3. **一致性指数 (consistency_index)**
   = mean / (mean + k*std), k=2。同时惩罚低分与高方差。
   设计意图: 两个 pipeline 若 mean 相同, std 小的胜出;
   若 std 相同, mean 高的胜出。把题目口径编码成一个可比较的标量。

4. **稳定性等级** (便于快速判读)
   std <= 5  : 优秀 (跨片段高度一致)
   std <= 10 : 良好
   std <= 20 : 一般 (存在明显片段依赖)
   std >  20 : 差 (pipeline 对该类片段不鲁棒)

## 诚实性条款

- 只统计**实际测到**的指标 (value is not None), 缺失不计入也不补零。
- 样本数 < 2 时明确返回"无法计算方差", 不伪造结论。
- 同时输出每个片段的原始分, 让读者能自己复核, 而不是只信一个聚合数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

STABILITY_LEVELS = [(5.0, "优秀"), (10.0, "良好"), (20.0, "一般"), (float("inf"), "差")]


@dataclass
class StabilityResult:
    """稳定性分析结果."""

    per_dim: dict[str, dict[str, Any]] = field(default_factory=dict)
    overall: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict:
        return {"per_dim": self.per_dim, "overall": self.overall, "note": self.note}


def _level(std: float) -> str:
    for th, name in STABILITY_LEVELS:
        if std <= th:
            return name
    return "差"


class StabilityEvaluator:
    """跨片段稳定性分析器.

    输入: {clip_id: {dim: score}} 的嵌套字典 (来自 L1/L2 的结果)。
    输出: 每个维度的跨片段统计 + 整体一致性指数。
    """

    dim = "cross_clip_stability"
    layer = "L3"

    #: 一致性指数的方差惩罚系数. k=2 表示 std 每增加 1 分, 相当于均值降低 2 分.
    K = 2.0

    def __init__(self, k: float | None = None) -> None:
        if k is not None:
            self.K = k

    def analyze(self, scores: dict[str, dict[str, float | None]]) -> StabilityResult:
        """scores 结构: {clip_id: {dim: value_or_None}}."""
        if len(scores) < 2:
            return StabilityResult(
                note=f"只有 {len(scores)} 个片段, 无法计算跨片段方差 "
                     f"(稳定性评估至少需要 2 个片段, 建议 3 个)"
            )

        all_dims: set[str] = set()
        for clip_scores in scores.values():
            all_dims |= set(clip_scores)

        per_dim: dict[str, dict[str, Any]] = {}
        for d in sorted(all_dims):
            vals: dict[str, float] = {}
            for clip, s in scores.items():
                v = s.get(d)
                if v is not None:
                    vals[clip] = float(v)
            if len(vals) < 2:
                per_dim[d] = {
                    "measured_clips": list(vals),
                    "std": None, "mean": None, "worst": None, "best": None,
                    "range": None, "level": "无法计算",
                    "note": f"仅 {len(vals)} 个片段测到该指标",
                }
                continue
            arr = np.array(list(vals.values()))
            std = float(arr.std(ddof=0))
            per_dim[d] = {
                "per_clip": {k: round(v, 2) for k, v in vals.items()},
                "measured_clips": sorted(vals),
                "mean": round(float(arr.mean()), 2),
                "std": round(std, 2),
                "worst": round(float(arr.min()), 2),
                "best": round(float(arr.max()), 2),
                "range": round(float(arr.max() - arr.min()), 2),
                "level": _level(std),
            }

        # 整体: 对所有已测维度的均值做二次聚合
        overall = self._aggregate(per_dim, scores)
        note = self._build_note(per_dim, overall)
        return StabilityResult(per_dim=per_dim, overall=overall, note=note)

    # ------------------------------------------------------------------ 内部

    def _aggregate(
        self, per_dim: dict[str, dict[str, Any]], scores: dict[str, dict[str, float | None]]
    ) -> dict[str, Any]:
        means = [v["mean"] for v in per_dim.values() if v.get("mean") is not None]
        stds = [v["std"] for v in per_dim.values() if v.get("std") is not None]
        worsts = [v["worst"] for v in per_dim.values() if v.get("worst") is not None]
        if not means:
            return {"available": False, "note": "没有足够数据做整体聚合"}

        mean_all = float(np.mean(means))
        std_all = float(np.mean(stds)) if stds else 0.0
        ci = mean_all / (mean_all + self.K * std_all) if (mean_all + self.K * std_all) > 0 else 0.0

        # 每片段的总分 (该片段所有已测维度的均值) —— 用于直接看 A/B 差多少
        clip_totals: dict[str, float] = {}
        for clip, s in scores.items():
            vs = [float(v) for v in s.values() if v is not None]
            if vs:
                clip_totals[clip] = round(float(np.mean(vs)), 2)

        result: dict[str, Any] = {
            "available": True,
            "mean_across_dims": round(mean_all, 2),
            "mean_std_across_dims": round(std_all, 2),
            "worst_dim_score": round(float(min(worsts)), 2) if worsts else None,
            "consistency_index": round(ci, 4),
            "consistency_index_formula": "mean / (mean + k*std), k=%.1f" % self.K,
            "level": _level(std_all),
            "clip_totals": clip_totals,
            "clip_spread": (round(max(clip_totals.values()) - min(clip_totals.values()), 2)
                            if len(clip_totals) >= 2 else None),
            "measured_dims": len(means),
        }
        return result

    def _build_note(self, per_dim: dict[str, dict[str, Any]], overall: dict[str, Any]) -> str:
        if not overall.get("available"):
            return "数据不足, 无法给出稳定性结论。"

        lines = [
            "【跨片段稳定性结论】",
            f"  一致性指数 (CI) = {overall['consistency_index']}  "
            f"[{overall['consistency_index_formula']}]",
            f"  跨片段平均标准差 = {overall['mean_std_across_dims']} 分  等级: {overall['level']}",
            f"  各片段综合分 = {overall['clip_totals']}",
        ]
        if overall.get("clip_spread") is not None:
            spread = overall["clip_spread"]
            verdict = (
                "★ 片段间差异小, 说明 pipeline 可复用性好 (符合题目 P1 要求)"
                if spread <= 10 else
                "⚠ 片段间存在明显差异, 说明 pipeline 对片段内容敏感, 可复用性存疑"
            )
            lines.append(f"  片段极差 = {spread} 分 -> {verdict}")

        unstable = [d for d, v in per_dim.items()
                    if v.get("std") is not None and v["std"] > 15]
        if unstable:
            lines.append(f"  ⚠ 高方差维度 (std>15): {unstable}")
        lines.append("")
        lines.append("  解读: CI 越高越好 (越接近 1)。"
                     "它同时惩罚\"分数低\"和\"方差大\", 对应题目"
                     "\"A/B 同 pipeline 都 75 分 > 特调 90 分\" 的口径。")
        return "\n".join(lines)
