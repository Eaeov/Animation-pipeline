"""评估报告聚合与输出.

把 L1/L2/L3 的结果汇总成:
  - report.json  机器可读, 便于后续统计与画图
  - report.md    人可读, 直接可作为交付文档的一部分

设计原则: 报告必须能自证 —— 每个分数都能追溯到具体指标与证据文件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .base import ClipPair, Metric
from .l1_technical import L1Evaluators
from .l2_semantic import L2Evaluators
from .l3_stability import StabilityEvaluator, StabilityResult

STAGE_DIRS = {
    "ingest": "01_ingest", "character": "02_character",
    "style_align": "03_style_align", "transfer": "04_transfer",
    "assemble": "05_assemble", "eval": "06_eval",
}


@dataclass
class ClipReport:
    clip_id: str
    metrics: list[Metric] = field(default_factory=list)

    @property
    def by_layer(self) -> dict[str, list[Metric]]:
        out: dict[str, list[Metric]] = {}
        for m in self.metrics:
            out.setdefault(m.layer, []).append(m)
        return out

    def layer_mean(self, layer: str) -> float | None:
        vs = [m.value for m in self.metrics if m.layer == layer and m.value is not None]
        return round(sum(vs) / len(vs), 2) if vs else None

    def total(self) -> float | None:
        vs = [m.value for m in self.metrics if m.value is not None]
        return round(sum(vs) / len(vs), 2) if vs else None


@dataclass
class EvalReport:
    run_id: str
    clips: dict[str, ClipReport] = field(default_factory=dict)
    stability: StabilityResult = field(default_factory=StabilityResult)
    report_path: Path | None = None
    stability_note: str = ""

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "clips": {
                cid: {
                    "metrics": [m.to_dict() for m in cr.metrics],
                    "layer_means": {k: cr.layer_mean(k) for k in ("L1", "L2")},
                    "total": cr.total(),
                }
                for cid, cr in self.clips.items()
            },
            "stability": self.stability.to_dict(),
        }

    def summary_text(self) -> str:
        """控制台友好的摘要 (结论先行)."""
        lines: list[str] = []
        lines.append("=" * 68)
        lines.append(f"评估报告  run_id={self.run_id}")
        lines.append("=" * 68)

        ov = self.stability.overall
        if ov.get("available"):
            lines.append("")
            lines.append("【结论】")
            lines.append(f"  跨片段一致性指数 CI = {ov['consistency_index']} "
                         f"(等级: {ov['level']})")
            lines.append(f"  各片段综合分: {ov['clip_totals']}")
            if ov.get("clip_spread") is not None:
                lines.append(f"  片段极差: {ov['clip_spread']} 分")
        lines.append("")

        for cid, cr in self.clips.items():
            lines.append(f"[片段 {cid}]  综合 {cr.total()}")
            for layer in ("L1", "L2"):
                ms = [m for m in cr.metrics if m.layer == layer]
                if not ms:
                    continue
                lm = cr.layer_mean(layer)
                lines.append(f"  {layer} 均值: {lm}")
                for m in ms:
                    v = "N/A" if m.value is None else f"{m.value:6.1f}"
                    note = f"  ({m.note})" if m.note else ""
                    lines.append(f"    {m.dim:<24} {v}{note}")
            lines.append("")

        if self.stability_note:
            for ln in self.stability_note.splitlines():
                lines.append(ln)

        if self.report_path:
            lines.append("")
            lines.append(f"完整报告: {self.report_path}")
        return "\n".join(lines)


# --------------------------------------------------------------------- 入口


def build_pairs(run_dir: Path, clips: list[str]) -> dict[str, ClipPair]:
    """从运行目录重建 ClipPair (纯文件系统驱动, 不依赖内存态)."""
    pairs: dict[str, ClipPair] = {}
    for cid in clips:
        pair = ClipPair(clip_id=cid)
        p = run_dir / cid
        cand = {
            "reference": p / STAGE_DIRS["ingest"] / "clip.mp4",
            "generated": p / STAGE_DIRS["assemble"] / "final.mp4",
            "character": p / STAGE_DIRS["character"] / "character.jpg",
            "aligned": p / STAGE_DIRS["style_align"] / "character_aligned.png",
        }
        for attr, path in cand.items():
            setattr(pair, attr, path if path.exists() else None)
        if pair.generated is None:
            t = p / STAGE_DIRS["transfer"] / "transferred.mp4"
            pair.generated = t if t.exists() else None
        pairs[cid] = pair
    return pairs


def run_evaluation(
    run_dir: Path,
    *,
    clips: list[str] | None = None,
    layers: str = "L1,L2,L3",
) -> EvalReport:
    """执行评估, 产出报告.

    Args:
        run_dir: runs/<run_id> 目录.
        clips: 要评估的片段; None 则自动发现 (存在 assemble 产物的片段).
        layers: 逗号分隔, 如 "L1,L3" (跳过 L2 可省 VLM 调用).
    """
    want = {s.strip().upper() for s in layers.split(",") if s.strip()}

    if clips is None:
        clips = sorted(
            d.name for d in run_dir.iterdir()
            if d.is_dir() and (d / STAGE_DIRS["assemble"]).exists()
            and not d.name.startswith("_")
        )
    pairs = build_pairs(run_dir, clips)

    report = EvalReport(run_id=run_dir.name)
    eval_root = run_dir / "_eval"
    eval_root.mkdir(parents=True, exist_ok=True)

    scores: dict[str, dict[str, float | None]] = {}

    for cid, pair in pairs.items():
        cr = ClipReport(clip_id=cid)
        out_dir = eval_root / cid
        out_dir.mkdir(parents=True, exist_ok=True)

        if not pair.generated:
            cr.metrics.append(Metric(
                "pipeline_completeness", "L1", 0.0,
                note="未找到生成产物, 该片段未完成管线",
            ))
            report.clips[cid] = cr
            scores[cid] = {m.dim: m.value for m in cr.metrics}
            continue

        if "L1" in want:
            for ev in L1Evaluators.all():
                try:
                    cr.metrics.append(ev.score(pair, out_dir=out_dir))
                except Exception as exc:  # noqa: BLE001
                    cr.metrics.append(Metric(ev.dim, ev.layer, None, note=f"评估异常: {exc}"))

        if "L2" in want:
            for ev in L2Evaluators.all():
                try:
                    res = ev.score(pair, out_dir=out_dir)
                    cr.metrics.extend(res if isinstance(res, list) else [res])
                except Exception as exc:  # noqa: BLE001
                    dim = getattr(ev, "dim", type(ev).__name__)
                    cr.metrics.append(Metric(dim, "L2", None, note=f"评估异常: {exc}"))

        report.clips[cid] = cr
        scores[cid] = {m.dim: m.value for m in cr.metrics}

        # 片段级报告落盘
        (out_dir / "report.json").write_text(
            json.dumps(
                {"clip_id": cid,
                 "metrics": [m.to_dict() for m in cr.metrics],
                 "layer_means": {k: cr.layer_mean(k) for k in ("L1", "L2")},
                 "total": cr.total()},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

    if "L3" in want:
        report.stability = StabilityEvaluator().analyze(scores)
        report.stability_note = report.stability.note

    # 全局报告
    (eval_root / "report.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path = eval_root / "report.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    report.report_path = md_path
    return report


def render_markdown(report: EvalReport) -> str:
    """渲染 markdown 报告 (可直接作为交付物)."""
    L: list[str] = []
    L.append(f"# 评估报告 — `{report.run_id}`")
    L.append("")
    ov = report.stability.overall
    L.append("## 一、结论摘要")
    L.append("")
    if ov.get("available"):
        L.append(f"- **跨片段一致性指数 (CI)**: `{ov['consistency_index']}` — 等级 **{ov['level']}**")
        L.append(f"- **各片段综合分**: {ov['clip_totals']}")
        if ov.get("clip_spread") is not None:
            L.append(f"- **片段间极差**: {ov['clip_spread']} 分")
        L.append(f"- 一致性公式: `{ov['consistency_index_formula']}`")
    else:
        L.append(f"- {report.stability.note or '数据不足'}")
    L.append("")

    L.append("## 二、分层结果")
    L.append("")
    for cid, cr in report.clips.items():
        L.append(f"### 片段 {cid} — 综合分 {cr.total()}")
        L.append("")
        for layer, name in (("L1", "客观技术质量"), ("L2", "语义质量")):
            ms = [m for m in cr.metrics if m.layer == layer]
            if not ms:
                continue
            L.append(f"**{layer} {name}** (均值 {cr.layer_mean(layer)})")
            L.append("")
            L.append("| 指标 | 分数 | 原始值 | 说明 |")
            L.append("|---|---:|---:|---|")
            for m in ms:
                v = "N/A" if m.value is None else f"{m.value:.1f}"
                rv = "" if m.raw_value is None else f"{m.raw_value}"
                note = m.note or (m.detail.get("note", "") if m.detail else "")
                L.append(f"| `{m.dim}` | {v} | {rv} | {note} |")
            L.append("")

    per_dim = report.stability.per_dim
    if per_dim:
        L.append("## 三、跨片段稳定性明细 (L3)")
        L.append("")
        L.append("| 维度 | 均值 | **标准差** | 最差 | 最好 | 极差 | 等级 | 各片段 |")
        L.append("|---|---:|---:|---:|---:|---:|---|---|")
        for d, v in per_dim.items():
            if v.get("std") is None:
                L.append(f"| `{d}` | — | — | — | — | — | 无法计算 | {v.get('note','')} |")
                continue
            L.append(
                f"| `{d}` | {v['mean']} | **{v['std']}** | {v['worst']} | {v['best']} | "
                f"{v['range']} | {v['level']} | {v.get('per_clip', {})} |"
            )
        L.append("")

    if report.stability_note:
        L.append("## 四、稳定性解读")
        L.append("")
        L.append("```")
        L.append(report.stability_note)
        L.append("```")
        L.append("")

    L.append("## 五、指标口径说明")
    L.append("")
    L.append("| 层级 | 指标 | 含义 | 归一化方式 |")
    L.append("|---|---|---|---|")
    L.append("| L1 | `psnr` | 像素保真 | 20–40dB → 0–100 |")
    L.append("| L1 | `ssim` | 结构相似 | ×100 |")
    L.append("| L1 | `temporal_stability` | 帧间亮度跳变 (闪脸检测) | 闪烁量 6.0 → 0 分 |")
    L.append("| L1 | `motion_smoothness` | 光流变异系数 | CV 0.8 → 0 分 |")
    L.append("| L1 | `sharpness` | 锐度保持比 | 比值 0.2–0.8 → 0–100 |")
    L.append("| L1 | `color_fidelity` | HSV 加权色差 | 距离 40 → 0 分 |")
    L.append("| L2 | `identity_consistency` | 外貌与人设一致性 | VLM 0–100 |")
    L.append("| L2 | `motion_preservation` | 动作保留度 | VLM 0–100 |")
    L.append("| L2 | `scene_preservation` | 场景保留度 | VLM 0–100 |")
    L.append("| L2 | `artifact_level` | 伪影严重度 (反向) | VLM 0–100 |")
    L.append("| L3 | `cross_clip_stability` | 跨片段方差 | 见 L3 模块 |")
    return "\n".join(L)
