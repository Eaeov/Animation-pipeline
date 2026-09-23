"""PV 分析 —— 自动推荐切片方案.

为什么需要这个模块:
    题面要求"截取若干片段", 但**手动数秒数**既慢又容易选到不合适的片段
    (比如镜头切换瞬间、主角被遮挡的帧)。而片段选择质量**直接决定**
    换人结果 —— 选到主角占比适中的静止镜头, 换人成功率高; 选到剧烈运镜
    或多人镜头, 必然失败。

    所以"选片段"这一步不该靠直觉, 应该靠可测量的信号:
      - 镜头切换点 (shot boundary) -> 保证片段完整包含一个镜头, 不切在切换处
      - 画面运动量          -> 排序出"静止/中速/剧烈"三档, 便于构造 A/B/C
      - 主体显著性 (代理)    -> 优先选主体清晰突出的镜头 (换人的前提)

输出: 一张按评分排序的候选片段表 + 可直接粘贴到 clips.yaml 的建议方案。

设计取舍:
    - 不引入 scenedetect 等重依赖, 用 OpenCV 直方图差分做镜头检测
      (够用且零额外依赖 —— 见 ARCHITECTURE.md "不做过度抽象")
    - **不依赖 Haar/DNN 人脸检测**。原设计想用 `cv2.CascadeClassifier`,
      但实测 OpenCV 5.0 已移除该类及 Haar 模型文件 (objdetect 模块重构),
      且 dlib/insightface 会引入重依赖。
      改用**视觉显著性代理指标** (见 `_subject_score`), 零依赖且更稳健:
        · 中心区域能量占比  -> 主体是否在画面中央 (构图常识)
        · 色彩饱和度集中度  -> 角色面部/服装通常比背景更饱和
        · 区域间对比度      -> 主体与背景的分离度
      这些信号组合足以区分"主体清晰的中近景"与"无主体的空镜/快速剪辑",
      而后者正是换人必然失败的镜头。局限见 `Measure` 中的 note。
局限性防御 (实测踩到的坑):
    纯噪声/快速剪辑画面会让"边缘密度"爆表, 被误判成"主体非常突出"。
    实测: 一段全屏随机噪点的镜头拿到适配度 73 分、主体占比 34.8%、
    锐度 46524 —— 明显误判。
    因此加了三个**噪声/异常画面抑制**判据 (见函数内注释),
    任一中招则直接判为"无主体"。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .errors import InputValidationError

# --------------------------------------------------------------------- 数据结构


@dataclass
class Shot:
    """一个镜头 (两次切换之间的连续片段)."""

    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "duration": round(self.duration, 2),
        }


@dataclass
class ShotMetrics:
    """单个镜头的可测量特征 —— 用来判断"适合换人吗"."""

    shot: Shot
    motion: float = 0.0           # 平均光流幅值 (越大运镜越剧烈)
    motion_cv: float = 0.0        # 运动变异系数 (越大越不平稳)
    subject_ratio: float = 0.0    # 含"显著主体"的帧占比 (显著性代理)
    subject_area: float = 0.0     # 主体区域占画幅比例
    sharpness: float = 0.0        # 拉普拉斯方差
    brightness: float = 0.0
    saturation: float = 0.0

    @property
    def suitability(self) -> float:
        """适配度 0-100 —— 越高越适合做换人素材.

        权重设计依据 (为什么是这几个):
          - subject_ratio 权重最高 (0.40): 换人的前提是"有明确主体在画面里",
            全是背景的空镜无从换起。这是硬门槛。
          - subject_area 次之 (0.25): 主体太小 (远景) 换完看不出效果, 也容易糊。
          - motion 反向 (0.20): 剧烈运镜会让主体位移过大, 模型跟不上 -> 减分。
          - sharpness 正向 (0.15): 原片糊则换人后更糊, 影响 L1 锐度指标。
        """
        if self.subject_ratio <= 0:
            return 0.0
        # 运动过大的镜头直接判低分: 帧间光流 > 8 说明画面剧烈跳变
        # (快速剪辑/闪烁/噪点), 换人模型必然失败, 不该推荐为 baseline
        if self.motion > 8.0:
            return round(20.0 * min(1.0, self.subject_ratio), 2)
        subj_term = 0.40 * min(1.0, self.subject_ratio)
        area_term = 0.25 * min(1.0, self.subject_area / 0.16)   # 16% 画幅算满分
        motion_term = 0.20 * max(0.0, 1.0 - self.motion / 6.0)
        # 锐度用对数缩放: 拉普拉斯方差跨度极大 (几十到几万),
        # 线性归一会被极端值主导, log 后 300 附近即接近满分
        sharp_term = 0.15 * min(1.0, math.log1p(max(0.0, self.sharpness)) / math.log1p(300.0))
        return round(100.0 * (subj_term + area_term + motion_term + sharp_term), 2)

    def to_dict(self) -> dict:
        return {
            **self.shot.to_dict(),
            "motion": round(self.motion, 3),
            "motion_cv": round(self.motion_cv, 3),
            "subject_ratio": round(self.subject_ratio, 3),
            "subject_area": round(self.subject_area, 4),
            "sharpness": round(self.sharpness, 2),
            "brightness": round(self.brightness, 2),
            "saturation": round(self.saturation, 2),
            "suitability": self.suitability,
        }


@dataclass
class ScanResult:
    """整支 PV 的分析结果."""

    path: Path
    duration: float
    width: int
    height: int
    fps: float
    shots: list[ShotMetrics] = field(default_factory=list)
    suggested: dict[str, dict] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def ranked(self) -> list[ShotMetrics]:
        return sorted(self.shots, key=lambda s: s.suitability, reverse=True)

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "duration": round(self.duration, 2),
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 2),
            "shot_count": len(self.shots),
            "shots": [s.to_dict() for s in self.shots],
            "suggested": self.suggested,
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------- 核心分析


def detect_shots(
    path: Path,
    *,
    threshold: float = 0.42,
    min_shot_s: float = 0.6,
    sample_fps: float = 8.0,
) -> list[Shot]:
    """用 HSV 直方图差分检测镜头切换.

    Args:
        threshold: 直方图相关性 1-corr 超过该值判为切换。0.42 是经验值,
            对动画片(色块大、切换干脆)比较合适。
        min_shot_s: 短于该值的镜头会被并入前一个 (避免检测噪声)。
        sample_fps: 分析帧率。8fps 对镜头检测足够, 且比逐帧快得多。

    为什么用直方图而不是像素差:
        像素差对运镜/亮度变化极敏感, 会把"镜头在摇"误判成"切镜头"。
        直方图对全局运动鲁棒, 但对内容突变(真的切镜头)敏感 —— 正是我们要的。
    """
    import cv2

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise InputValidationError(f"无法打开视频: {path}", stage="ingest")

    src_fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    step = max(1, int(round(src_fps / sample_fps)))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    boundaries: list[float] = [0.0]
    prev_hist = None
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            t = idx / src_fps
            small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv], [0, 1], None, [30, 32], [0, 180, 0, 256])
            cv2.normalize(hist, hist)
            if prev_hist is not None:
                corr = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL)
                if 1.0 - corr > threshold:
                    boundaries.append(t)
            prev_hist = hist
        idx += 1
    cap.release()

    duration = total / src_fps if total else (idx / src_fps if idx else 0.0)
    boundaries.append(duration)

    # 合并过短镜头
    shots: list[Shot] = []
    seg_start = boundaries[0]
    for i in range(1, len(boundaries)):
        seg_end = boundaries[i]
        if seg_end - seg_start < min_shot_s and shots:
            # 太短 -> 并入前一个镜头
            shots[-1].end = seg_end
            seg_start = seg_end
            continue
        shots.append(Shot(index=len(shots), start=seg_start, end=seg_end))
        seg_start = seg_end
    # 收尾: 最后一个不足 min_shot_s 就并掉
    if len(shots) >= 2 and shots[-1].duration < min_shot_s:
        shots[-2].end = shots[-1].end
        shots.pop()
    for i, s in enumerate(shots):
        s.index = i
    return shots


def _subject_score(frame: np.ndarray) -> tuple[bool, float]:
    """视觉显著性代理: 判断画面里有没有"明确主体", 返回 (有无, 主体占比).

    为什么不用人脸检测:
        OpenCV 5.0 已移除 `CascadeClassifier` 与 Haar 模型文件,
        而 dlib/insightface 是重依赖 (需编译/下载数百 MB 权重)。
        对于"选片段"这个粗判需求, 显著性代理已足够, 且零依赖。

    判据 (三个信号取交集, 都要求主体相对背景"突出"):
        1. 中心能量: 中央 50% 区域的边缘密度应显著高于四周
           —— 构图常识: 主体在中央, 且比背景有更多细节
        2. 饱和度集中: 中央区饱和度的标准差大 (说明有局部鲜艳色块,
           通常是角色面部/服装), 而非整幅均匀
        3. 对比度分离: 中央与边缘的平均亮度/色度有差异

    局限 (诚实说明):
        这是**代理指标**, 不能真正识别"这是脸"。
        对以下情况会误判:
          - 画面中央有大片高细节背景 (如密集文字、复杂花纹) -> 误判为有主体
          - 角色位于画面边缘 (非中心构图) -> 漏判
        因此 scan 结果**只作推荐**, 最终切分仍需人眼确认关键帧。
    """
    import cv2

    h, w = frame.shape[:2]
    if h < 16 or w < 16:
        return False, 0.0

    # --- 噪声/异常画面抑制 (三个判据, 任一命中直接判无主体) ---
    # 依据: 实测全屏随机噪点会骗过边缘密度判据。
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    local_std = float(cv2.Laplacian(gray, cv2.CV_64F).std())

    # 判据 1: 高频能量过高 -> 是噪点/极度杂乱的画面, 不是正常主体
    if local_std > 90.0:
        return False, 0.0

    # 判据 2: 帧内亮度方差极大 -> 画面本身就在"闪烁/跳变", 不可用作素材
    if float(gray.std()) > 95.0:
        return False, 0.0

    # 判据 3: 直方图过于平坦 -> 没有明确的明暗层次 (主体/背景分不开)
    hist = cv2.calcHist([gray], [0], None, [64], [0, 256]).ravel()
    hist = hist / (hist.sum() + 1e-9)
    flatness = float(np.exp(-np.sum(hist * np.log(hist + 1e-9))))  # 有效箱数
    if flatness > 46.0:   # 64 箱里有效箱数超过 46 -> 近似均匀分布
        return False, 0.0

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

    # 中央区 (50%) 与边缘区
    y0, y1 = h // 4, h * 3 // 4
    x0, x1 = w // 4, w * 3 // 4
    center = gray[y0:y1, x0:x1]
    sat_c = hsv[y0:y1, x0:x1, 1]

    edge_full = cv2.Canny(gray, 60, 160)
    e_center = float(edge_full[y0:y1, x0:x1].mean())
    e_all = float(edge_full.mean())
    if e_all < 1e-6:
        return False, 0.0
    center_energy = e_center / (e_all + 1e-6)

    sat_std = float(sat_c.std())
    contrast = float(abs(center.mean() - gray.mean()))

    # 三信号打分 (各自归一化到 0-1 后加权)
    s_energy = min(1.0, max(0.0, (center_energy - 1.0) / 0.8))   # >1 表示中心更密
    s_sat = min(1.0, sat_std / 70.0)
    s_contrast = min(1.0, contrast / 30.0)
    salience = 0.45 * s_energy + 0.35 * s_sat + 0.20 * s_contrast

    has_subject = salience >= 0.30
    # 主体占比用"中央高细节连通区"近似 (粗估, 用于排序足矣)
    area = 0.0
    if has_subject:
        _, bw = cv2.threshold(edge_full, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        area = float(bw[y0:y1, x0:x1].mean()) / 255.0
    return has_subject, area


def measure_shot(
    path: Path,
    shot: Shot,
    *,
    sample_fps: float = 6.0,
) -> ShotMetrics:
    """测量单个镜头的运动量/显著性/锐度."""
    import cv2

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return ShotMetrics(shot=shot)

    src_fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    step = max(1, int(round(src_fps / sample_fps)))

    cap.set(cv2.CAP_PROP_POS_MSEC, shot.start * 1000.0)
    frames: list[np.ndarray] = []
    grays: list[np.ndarray] = []
    idx = 0
    while True:
        pos = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if pos > shot.end + 0.05:
            break
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            frames.append(frame)
            grays.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        idx += 1
    cap.release()

    m = ShotMetrics(shot=shot)
    if not frames:
        return m

    # --- 运动量 (相邻帧光流幅值) ---
    mags = []
    for i in range(1, len(grays)):
        flow = cv2.calcOpticalFlowFarneback(
            grays[i - 1], grays[i], None, 0.5, 3, 15, 3, 5, 1.2, 0,
        )
        mag, _ = cv2.cartToPolar(flow[..., 0], flow[..., 1])
        mags.append(float(mag.mean()))
    if mags:
        arr = np.array(mags)
        m.motion = float(arr.mean())
        m.motion_cv = float(arr.std() / (arr.mean() + 1e-6))

    # --- 锐度 / 亮度 / 饱和度 ---
    m.sharpness = float(np.mean([
        cv2.Laplacian(g, cv2.CV_64F).var() for g in grays
    ]))
    m.brightness = float(np.mean([g.mean() for g in grays]))
    m.saturation = float(np.mean([
        cv2.cvtColor(f, cv2.COLOR_BGR2HSV)[..., 1].mean() for f in frames
    ]))

    # --- 显著性 (主体代理) ---
    hits = 0
    areas = []
    for f in frames:
        ok, area = _subject_score(f)
        if ok:
            hits += 1
            areas.append(area)
    m.subject_ratio = hits / len(frames) if frames else 0.0
    m.subject_area = float(np.mean(areas)) if areas else 0.0
    return m


def suggest_scheme(
    shots: list[ShotMetrics],
    *,
    target_seconds: float = 6.0,
    platform_max: float = 30.0,
) -> tuple[dict[str, dict], list[str]]:
    """从候选镜头里挑出 A/B/C 建议方案.

    选择逻辑 (对应题面"A/B 同规格, C 是压力测试"):
      - A: 适配度最高且运动量**低**的镜头  -> 建立 pipeline 上限基线
      - B: 与 A **规格相近但运镜不同**的镜头 -> 才能测出跨片段稳定性
      - C: 运动量**最高**的镜头 (故意为难)   -> 用于失败分析

    为什么 B 要"相近但不同":
        如果 B 和 A 一模一样, 测不出可复用性; 如果 B 和 A 差太远,
        分差可能来自内容难度而非 pipeline 不稳定。所以要"同规格不同运镜"。
    """
    warns: list[str] = []
    ok = [s for s in shots if s.shot.duration >= 2.5 and s.subject_ratio > 0]
    if len(ok) < 2:
        warns.append(
            f"仅找到 {len(ok)} 个可用镜头 (需检出主体且时长≥2.5s), "
            "建议更换 PV 或调整 threshold"
        )
        if not ok:
            return {}, warns

    by_suit = sorted(ok, key=lambda s: s.suitability, reverse=True)
    by_motion = sorted(ok, key=lambda s: s.motion)

    a = by_suit[0]
    # B: 在适配度前 60% 里, 找运动量与 A 差异最大的
    pool = by_suit[: max(2, int(math.ceil(len(by_suit) * 0.6)))]
    pool = [s for s in pool if s.shot.index != a.shot.index]
    b = max(pool, key=lambda s: abs(s.motion - a.motion)) if pool else None

    # C 是压力测试, 目的就是"预期失败" —— 所以从**全部**镜头里挑运动最猛的,
    # 不受"必须有主体"的限制。选到无主体/剧烈运镜的镜头反而更有分析价值。
    c = max(shots, key=lambda s: s.motion) if shots else None

    def clip_entry(m: ShotMetrics, role: str, complexity: str, why: str) -> dict:
        dur = min(target_seconds, m.shot.duration, platform_max)
        dur = max(2.5, round(dur, 1))     # 平台下限 2s, 留余量
        start = round(m.shot.start + max(0.0, (m.shot.duration - dur) / 2), 2)
        return {
            "start": start,
            "duration": dur,
            "role": role,
            "complexity": complexity,
            "rationale": why,
            "_shot_index": m.shot.index,
            "_suitability": m.suitability,
        }

    suggested: dict[str, dict] = {}
    suggested["A"] = clip_entry(
        a, "baseline", "low",
        f"适配度最高的镜头(#{a.shot.index}): 主体检出率 {a.subject_ratio:.0%}, "
        f"平均运动 {a.motion:.2f}, 用于建立 pipeline 上限基线",
    )
    if b is not None:
        lvl = "low" if b.motion < 2.0 else ("medium" if b.motion < 5.0 else "high")
        suggested["B"] = clip_entry(
            b, "baseline", lvl,
            f"与 A 规格相近但运镜不同(#{b.shot.index}): 平均运动 {b.motion:.2f} "
            f"vs A 的 {a.motion:.2f}, 用于测跨片段稳定性",
        )
    if c is not None and (b is None or c.shot.index not in
                          (a.shot.index, b.shot.index)):
        reason = (
            f"运动最剧烈的镜头(#{c.shot.index}): 平均运动 {c.motion:.2f}"
        )
        if c.subject_ratio <= 0:
            reason += ", 且未检出显著主体"
        reason += "。刻意选为压力测试样本 —— 预期换人质量显著下降, 用于失败案例分析"
        suggested["C"] = clip_entry(c, "stress", "high", reason)

    total = sum(v["duration"] for v in suggested.values())
    if total > 45:
        warns.append(
            f"建议片段总时长 {total:.1f}s 接近免费额度 50s, 建议缩短 "
            "duration 或减少片段数"
        )
    return suggested, warns


def scan(
    path: Path,
    *,
    threshold: float = 0.42,
    max_shots: int = 40,
) -> ScanResult:
    """完整扫描: 检测镜头 -> 测量特征 -> 推荐方案."""
    from .utils.video import probe

    info = probe(path)
    shots = detect_shots(path, threshold=threshold)
    if len(shots) > max_shots:
        # 镜头太多说明是快剪 PV, 只分析最长的若干个 (质量优先)
        shots = sorted(shots, key=lambda s: s.duration, reverse=True)[:max_shots]
        shots.sort(key=lambda s: s.start)
        for i, s in enumerate(shots):
            s.index = i

    detector = None  # 保留参数位以兼容旧调用; 当前实现用显著性代理, 无需检测器
    metrics = [measure_shot(path, s) for s in shots]
    suggested, warns = suggest_scheme(metrics)

    if not any(m.subject_ratio > 0 for m in metrics):
        warns.append(
            "所有镜头都未检出显著主体 —— 可能是纯风景 PV 或阈值偏严。"
            "建议人工检查关键帧后手动填写 clips.yaml"
        )

    return ScanResult(
        path=path,
        duration=info.duration,
        width=info.width,
        height=info.height,
        fps=info.fps,
        shots=metrics,
        suggested=suggested,
        warnings=warns,
    )


def render_scan(result: ScanResult, *, top: int = 12) -> str:
    """把扫描结果渲染成人类可读的文本报告."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("PV 扫描报告")
    lines.append("=" * 72)
    lines.append(f"文件      : {result.path}")
    lines.append(f"时长      : {result.duration:.2f}s")
    lines.append(f"规格      : {result.width}x{result.height} @ {result.fps:.2f}fps")
    lines.append(f"检测镜头数: {len(result.shots)}")
    lines.append("")

    if result.warnings:
        lines.append("【提示】")
        for w in result.warnings:
            lines.append(f"  ⚠ {w}")
        lines.append("")

    lines.append(f"【镜头排行 (按换人适配度, 前 {min(top, len(result.shots))} 名)】")
    lines.append(
        f"  {'#':>3} {'起始':>7} {'时长':>6} {'适配度':>7} "
        f"{'运动':>6} {'主体率':>7} {'主体占比':>8} {'锐度':>7}"
    )
    for m in result.ranked()[:top]:
        s = m.shot
        lines.append(
            f"  {s.index:>3} {s.start:>7.2f} {s.duration:>6.2f} "
            f"{m.suitability:>7.2f} {m.motion:>6.2f} "
            f"{m.subject_ratio:>6.0%} {m.subject_area:>7.1%} {m.sharpness:>7.1f}"
        )
    lines.append("")

    if result.suggested:
        lines.append("【推荐切片方案 (可直接粘贴到 configs/clips.yaml)】")
        lines.append("clips:")
        for cid in ("A", "B", "C"):
            v = result.suggested.get(cid)
            if not v:
                continue
            lines.append(f"  {cid}:")
            lines.append(f"    start: {v['start']}")
            lines.append(f"    duration: {v['duration']}")
            lines.append(f"    role: \"{v['role']}\"")
            lines.append(f"    complexity: \"{v['complexity']}\"")
            lines.append(f"    rationale: \"{v['rationale']}\"")
        lines.append("")
        total = sum(v["duration"] for v in result.suggested.values())
        lines.append(f"  片段总时长: {total:.1f}s (免费额度 50s)")
    else:
        lines.append("【推荐切片方案】")
        lines.append("  未能推荐 —— 有效镜头不足, 见上方提示")
    lines.append("=" * 72)
    return "\n".join(lines)
