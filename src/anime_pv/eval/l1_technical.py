"""L1 客观技术质量评估.

指标选择理由 (为什么这几个最能反映"换人"任务的质量):

| 指标 | 测什么 | 为什么对本题重要 |
|---|---|---|
| PSNR | 像素级保真 | 换人应尽量少动画面其余区域, PSNR 过低说明背景被重绘 |
| SSIM | 结构相似 | 比 PSNR 更贴合人眼, 抓结构性破坏 |
| 时序闪烁度 (TF) | 帧间亮度跳变 | 换人模型最典型的失败是"人脸闪" |
| 光流平滑度 | 运动连续性 | 抓跳帧/抖动/动作断裂 |
| 边缘锐度比 | 画质退化 | 抓模糊/糊脸 |
| 色彩偏移 | 光照融合 | 抓"换上去的人脸色不对" |

重要: 这些指标是**有参照的** (ref vs generated), 不是无参照打分 ——
因为我们有原始 PV 片段作为 ground truth, 这正是本 pipeline 的优势。

自检要求: 对 MockAdapter(jitter>0) 的产物, TF/锐度指标必须显著变差,
否则说明指标失效 (见 tests/test_eval_sanity.py)。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .base import ClipPair, Metric


def _read_frames(video: Path, max_frames: int = 60) -> list[np.ndarray]:
    import cv2

    cap = cv2.VideoCapture(str(video))
    frames: list[np.ndarray] = []
    while len(frames) < max_frames:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    cap.release()
    return frames


def _resize_pair(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    import cv2

    h = min(a.shape[0], b.shape[0])
    w = min(a.shape[1], b.shape[1])
    if (a.shape[0], a.shape[1]) != (h, w):
        a = cv2.resize(a, (w, h), interpolation=cv2.INTER_AREA)
    if (b.shape[0], b.shape[1]) != (h, w):
        b = cv2.resize(b, (w, h), interpolation=cv2.INTER_AREA)
    return a, b


class PSNREvaluator:
    dim = "psnr"
    layer = "L1"

    def score(self, pair: ClipPair, *, out_dir: Path) -> Metric:
        if not pair.ready:
            return Metric(self.dim, self.layer, None, note="缺少参考或生成视频")
        ref = _read_frames(pair.reference)  # type: ignore[arg-type]
        gen = _read_frames(pair.generated)  # type: ignore[arg-type]
        if not ref or not gen:
            return Metric(self.dim, self.layer, None, note="抽帧失败")
        n = min(len(ref), len(gen))
        vals = []
        for i in range(n):
            a, b = _resize_pair(ref[i], gen[i])
            vals.append(_psnr(a, b))
        m = float(np.mean(vals))
        # 归一: PSNR 20dB -> 0 分, 40dB -> 100 分 (线性截断)
        score = float(np.clip((m - 20) / 20 * 100, 0, 100))
        return Metric(self.dim, self.layer, score, raw_value=round(m, 3),
                      detail={"frames": n, "per_frame_std": round(float(np.std(vals)), 3),
                              "normalization": "PSNR 20-40dB -> 0-100"})


class SSIMEvaluator:
    dim = "ssim"
    layer = "L1"

    def score(self, pair: ClipPair, *, out_dir: Path) -> Metric:
        if not pair.ready:
            return Metric(self.dim, self.layer, None, note="缺少参考或生成视频")
        try:
            from skimage.metrics import structural_similarity as ssim
        except ImportError:
            return Metric(self.dim, self.layer, None, note="缺 scikit-image")
        ref = _read_frames(pair.reference)  # type: ignore[arg-type]
        gen = _read_frames(pair.generated)  # type: ignore[arg-type]
        n = min(len(ref), len(gen))
        if not n:
            return Metric(self.dim, self.layer, None, note="抽帧失败")
        vals = []
        for i in range(n):
            a, b = _resize_pair(ref[i], gen[i])
            vals.append(ssim(a, b, channel_axis=2, data_range=255))
        m = float(np.mean(vals))
        return Metric(self.dim, self.layer, float(np.clip(m * 100, 0, 100)),
                      raw_value=round(m, 4),
                      detail={"frames": n, "per_frame_std": round(float(np.std(vals)), 4)})


class TemporalFlickerEvaluator:
    """时序闪烁度 —— 换人模型最典型的失败模式.

    做法: 计算相邻帧平均亮度的二阶差分均值。
    稳定视频的亮度应平滑变化; "人脸闪"会造成尖峰。
    分数 = 100 - 归一化后的闪烁量。
    """

    dim = "temporal_stability"
    layer = "L1"

    # 经验阈值: 平均亮度跳变 0.5/255 -> 视为开始明显闪烁
    FLICKER_REF = 6.0

    def score(self, pair: ClipPair, *, out_dir: Path) -> Metric:
        if not pair.generated or not pair.generated.exists():
            return Metric(self.dim, self.layer, None, note="缺少生成视频")
        frames = _read_frames(pair.generated)
        if len(frames) < 4:
            return Metric(self.dim, self.layer, None, note="帧数不足")

        means = np.array([float(f.mean()) for f in frames])
        d2 = np.abs(np.diff(means, n=2))
        flicker = float(d2.mean())

        # 同时看高频能量占比 (频域辅助证据)
        spec = np.abs(np.fft.rfft(means - means.mean()))
        hi = float(spec[len(spec) // 2:].sum() / (spec.sum() + 1e-9))

        score = float(np.clip(100 * (1 - flicker / self.FLICKER_REF), 0, 100))
        return Metric(
            self.dim, self.layer, score, raw_value=round(flicker, 4),
            detail={"frame_count": len(frames), "high_freq_energy_ratio": round(hi, 4),
                    "ref_threshold": self.FLICKER_REF,
                    "note": "分数越低说明帧间亮度跳变越剧烈(闪脸/闪烁)"},
        )


class FlowSmoothnessEvaluator:
    """光流平滑度 —— 抓动作断裂与抖动.

    做法: 计算相邻帧光流的平均幅值序列, 取其变异系数 (CV)。
    平滑动作的 CV 小; 跳帧/抖动会让 CV 变大。

    归一化说明 (为什么不用线性截断):
        原实现 `100*(1 - cv/0.8)` 在 CV=0.8 就归零, 过于陡峭 —— 真实动画
        片段的运镜本身就有变速 (推/拉/摇), CV 常在 0.6~1.5, 线性截断会让
        指标扎堆在 0 分, 失去区分度 (冒烟测试里 passthrough 也是 0 分)。
        改为指数饱和: score = 100 * exp(-cv / tau), tau=1.2。
        性质: CV=0 -> 100; CV=0.6 -> 61; CV=1.2 -> 37; CV=2.4 -> 13。
        单调、无硬截断、在常见区间保持分辨率, 但仍能惩罚剧烈抖动。
    """

    dim = "motion_smoothness"
    layer = "L1"

    # 饱和常数: 越大越宽容。1.2 对应"CV 每增加 1.2 分数衰减到 37%"
    TAU = 1.2

    def score(self, pair: ClipPair, *, out_dir: Path) -> Metric:
        if not pair.generated or not pair.generated.exists():
            return Metric(self.dim, self.layer, None, note="缺少生成视频")
        import cv2

        frames = _read_frames(pair.generated, max_frames=40)
        if len(frames) < 4:
            return Metric(self.dim, self.layer, None, note="帧数不足")

        grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
        mags = []
        for i in range(1, len(grays)):
            flow = cv2.calcOpticalFlowFarneback(
                grays[i - 1], grays[i], None,
                0.5, 3, 15, 3, 5, 1.2, 0,
            )
            mag, _ = cv2.cartToPolar(flow[..., 0], flow[..., 1])
            mags.append(float(mag.mean()))
        arr = np.array(mags)
        mean_motion = float(arr.mean())
        cv = float(arr.std() / (mean_motion + 1e-6))

        # 静止画面 (光流接近 0) 无平滑度可言 -> 判为 not_measured, 不用 0 冒充
        if mean_motion < 0.05:
            return Metric(self.dim, self.layer, None, raw_value=round(cv, 4),
                          detail={"mean_motion": round(mean_motion, 4),
                                  "note": "画面近似静止, 光流平滑度不适用"})

        score = float(np.clip(100.0 * float(np.exp(-cv / self.TAU)), 0, 100))
        return Metric(self.dim, self.layer, score, raw_value=round(cv, 4),
                      detail={"mean_motion": round(mean_motion, 3),
                              "std_motion": round(float(arr.std()), 3),
                              "cv": round(cv, 4), "frames": len(grays),
                              "tau": self.TAU,
                              "normalization": "score = 100*exp(-cv/tau)"})


class SharpnessEvaluator:
    """边缘锐度保持度 —— 抓模糊/糊脸."""

    dim = "sharpness"
    layer = "L1"

    def score(self, pair: ClipPair, *, out_dir: Path) -> Metric:
        import cv2

        if not pair.generated or not pair.generated.exists():
            return Metric(self.dim, self.layer, None, note="缺少生成视频")

        def lap_var(path: Path) -> float:
            frames = _read_frames(path, max_frames=30)
            vals = [cv2.Laplacian(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
                    for f in frames]
            return float(np.mean(vals)) if vals else 0.0

        gen_v = lap_var(pair.generated)
        if pair.reference and pair.reference.exists():
            ref_v = lap_var(pair.reference)
            if ref_v > 1e-6:
                ratio = gen_v / ref_v
                # 保留 80% 以上算满分, 低于 20% 算 0 分
                score = float(np.clip((ratio - 0.2) / 0.6 * 100, 0, 100))
                return Metric(self.dim, self.layer, score, raw_value=round(ratio, 4),
                              detail={"gen_lapvar": round(gen_v, 2),
                                      "ref_lapvar": round(ref_v, 2),
                                      "note": "比值<1 表示生成比原片更糊"})
        # 无参考时给绝对分 (经验区间 0-1000)
        score = float(np.clip(gen_v / 10, 0, 100))
        return Metric(self.dim, self.layer, score, raw_value=round(gen_v, 2),
                      detail={"note": "无参考, 使用绝对锐度"})


class ColorFidelityEvaluator:
    """色彩/光照保真度 —— 抓"换上去的人脸色不对".

    Mix 模式会用 Relighting LoRA 适配原片光照, 该指标检验其是否生效。
    做法: 比较整体色调 (H/S 通道均值) 与色温偏移。
    """

    dim = "color_fidelity"
    layer = "L1"

    def score(self, pair: ClipPair, *, out_dir: Path) -> Metric:
        import cv2

        if not pair.ready:
            return Metric(self.dim, self.layer, None, note="缺少参考或生成视频")
        ref = _read_frames(pair.reference)   # type: ignore[arg-type]
        gen = _read_frames(pair.generated)   # type: ignore[arg-type]
        n = min(len(ref), len(gen))
        if not n:
            return Metric(self.dim, self.layer, None, note="抽帧失败")

        d_h, d_s, d_v = [], [], []
        for i in range(n):
            a, b = _resize_pair(ref[i], gen[i])
            ha = cv2.cvtColor(a, cv2.COLOR_BGR2HSV).astype(np.float32)
            hb = cv2.cvtColor(b, cv2.COLOR_BGR2HSV).astype(np.float32)
            d_h.append(abs(float(ha[..., 0].mean()) - float(hb[..., 0].mean())) / 180 * 255)
            d_s.append(abs(float(ha[..., 1].mean()) - float(hb[..., 1].mean())))
            d_v.append(abs(float(ha[..., 2].mean()) - float(hb[..., 2].mean())))

        dist = float(np.mean(d_h) * 0.5 + np.mean(d_s) * 0.25 + np.mean(d_v) * 0.25)
        score = float(np.clip(100 * (1 - dist / 40), 0, 100))
        return Metric(self.dim, self.layer, score, raw_value=round(dist, 3),
                      detail={"mean_delta_h": round(float(np.mean(d_h)), 2),
                              "mean_delta_s": round(float(np.mean(d_s)), 2),
                              "mean_delta_v": round(float(np.mean(d_v)), 2),
                              "note": "H/S/V 加权距离, 越小说明色调越接近原片"})


def _psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    if mse <= 1e-10:
        return 99.0
    return float(10 * np.log10(255.0 ** 2 / mse))


class L1Evaluators:
    """L1 评估器集合."""

    @staticmethod
    def all() -> list:
        return [
            PSNREvaluator(), SSIMEvaluator(), TemporalFlickerEvaluator(),
            FlowSmoothnessEvaluator(), SharpnessEvaluator(), ColorFidelityEvaluator(),
        ]
