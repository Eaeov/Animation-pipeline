"""L2 语义质量评估 (VLM 评分).

为什么需要 L2: L1 全是"像素/结构/时序"指标, 但它们**无法回答"人换成功了吗"**。
一个把主角换成别人的视频, PSNR 可能很低(因为变化大)但其实换得很成功;
反过来, 一个几乎没改动的视频 PSNR 很高但根本没换人。
所以必须有语义层评估。

做法: 用多模态大模型 (默认 qwen-vl-max) 对"人设图 + 原始帧 + 生成帧"做三元对比评分。

关键设计 —— 保证评分可信:
  1. **固定 rubric**: prompt 里给出 0-100 的明确打分锚点, 不让模型自由发挥
  2. **固定 seed / temperature=0**: 同一输入尽量给同一分数 (可复现)
  3. **强制 JSON 输出**: 便于程序化解析, 避免自然语言歧义
  4. **留证**: 保存发给模型的原始 prompt / 图片路径 / 原始响应
  5. **调用失败标记 not_measured**, 绝不默认给中间分 (否则污染统计)

维度设计 (对"人物替换"任务最本质的四问):
  - identity_consistency  生成的脸像不像人设图里的人?
  - motion_preservation   动作/姿态是否保留了原片?
  - scene_preservation    背景/构图/镜头是否保留?
  - artifact_level        有无伪影 (糊脸/多手/穿模/闪烁)?
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import requests

from .base import ClipPair, Metric

VLM_MODEL = "qwen-vl-max"
VLM_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

RUBRIC = """你是动画视频质量评审专家。下面给你三张图:
- 图1: 目标角色的人设图 (我们希望把视频里的人物换成这个角色)
- 图2: 原始动画视频的一帧
- 图3: AI 换人后生成的对应帧

请严格按以下维度打分 (0-100 整数), 打分锚点:
- 90-100: 几乎无可挑剔, 可直接用于成片
- 75-89 : 质量良好, 有轻微瑕疵但可接受
- 60-74 : 及格, 明显瑕疵但主体可辨
- 40-59 : 较差, 关键要素损坏 (脸崩/动作丢失/背景严重改变)
- 0-39  : 失败, 不可用

维度定义:
1. identity_consistency: 图3中人物的外貌是否与图1的人设一致 (发型/发色/服装/五官风格)
2. motion_preservation : 图3中人物的姿态动作是否与图2保持一致
3. scene_preservation  : 图3的背景/构图/镜头角度/色调是否与图2保持一致
4. artifact_level      : 图3的画面质量, 有无模糊/伪影/结构错误 (100=无伪影, 0=严重伪影)

只输出 JSON, 不要任何解释文字:
{"identity_consistency": <int>, "motion_preservation": <int>, "scene_preservation": <int>,
 "artifact_level": <int>, "reason": "<30字内的中文理由>"}"""

DIM_MAP = {
    "identity_consistency": "identity_consistency",
    "motion_preservation": "motion_preservation",
    "scene_preservation": "scene_preservation",
    "artifact_level": "artifact_level",
}


class VLMSemanticEvaluator:
    """基于 VLM 的语义评分器.

    Args:
        model: VLM 模型名 (百炼 OpenAI 兼容接口).
        timeout: 单次请求超时.
    """

    layer = "L2"

    def __init__(self, model: str = VLM_MODEL, timeout: int = 120) -> None:
        self.model = model
        self.timeout = timeout

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _data_url(path: Path) -> str:
        ext = path.suffix.lower().lstrip(".")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(ext, "jpeg")
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:image/{mime};base64,{b64}"

    def _call(self, images: list[Path]) -> dict | None:
        key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if not key:
            return None
        content: list[dict] = [{"type": "text", "text": RUBRIC}]
        for i, p in enumerate(images, 1):
            content.append({"type": "text", "text": f"图{i}:"})
            content.append({"type": "image_url", "image_url": {"url": self._data_url(p)}})

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.0,          # 可复现性
            "seed": 42,
            "response_format": {"type": "json_object"},
        }
        try:
            r = requests.post(
                VLM_BASE,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
                timeout=self.timeout,
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            return json.loads(text)
        except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError):
            return None

    # ------------------------------------------------------------------ 协议

    def score(self, pair: ClipPair, *, out_dir: Path) -> list[Metric]:
        """返回四个维度的 Metric.

        需要: 人设图 + 参考帧 + 生成帧。缺任一项则全部标记 not_measured。
        """
        dims = list(DIM_MAP)
        if not (pair.character and pair.reference and pair.generated):
            return [Metric(d, self.layer, None, note="素材不足, 无法做语义评估") for d in dims]

        from ..utils.video import extract_frame  # 局部导入避免循环

        ev_dir = out_dir / "evidence"
        ev_dir.mkdir(parents=True, exist_ok=True)
        mid = 0.5
        ref_f = ev_dir / f"{pair.clip_id}_ref_mid.jpg"
        gen_f = ev_dir / f"{pair.clip_id}_gen_mid.jpg"
        try:
            extract_frame(pair.reference, ref_f, at=None)
            extract_frame(pair.generated, gen_f, at=None)
        except Exception:
            return [Metric(d, self.layer, None, note="抽帧失败") for d in dims]

        imgs = [pair.character, ref_f, gen_f]
        result = self._call(imgs)

        # 留存 prompt 与证据, 保证可复核
        (out_dir / f"vlm_request_{pair.clip_id}.json").write_text(
            json.dumps({"model": self.model, "images": [str(p) for p in imgs],
                        "rubric": RUBRIC}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if result is None:
            return [Metric(d, self.layer, None, note="VLM 调用失败或未配置密钥") for d in dims]

        (out_dir / f"vlm_response_{pair.clip_id}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        metrics: list[Metric] = []
        reason = str(result.get("reason", ""))[:60]
        for d in dims:
            raw = result.get(d)
            try:
                val = float(raw)
            except (TypeError, ValueError):
                metrics.append(Metric(d, self.layer, None, note="响应缺该维度"))
                continue
            metrics.append(Metric(
                d, self.layer, max(0.0, min(100.0, val)),
                detail={"vlm_reason": reason, "model": self.model, "evidence": [ref_f.name, gen_f.name]},
                evidence=[ref_f, gen_f],
            ))
        return metrics


class FaceIdentityEvaluator:
    """人脸身份相似度 (可选, 需 insightface + onnxruntime).

    比 VLM 更客观地量化"脸像不像", 但需要额外依赖与模型权重。
    依赖缺失时返回 not_measured —— 不阻塞整体评估。
    """

    dim = "face_identity_sim"
    layer = "L2"

    def score(self, pair: ClipPair, *, out_dir: Path) -> Metric:
        if not (pair.character and pair.generated):
            return Metric(self.dim, self.layer, None, note="素材不足")
        try:
            import cv2  # noqa: F401
            from insightface.app import FaceAnalysis
        except ImportError:
            return Metric(
                self.dim, self.layer, None,
                note="未安装 insightface, 跳过人脸相似度 (pip install insightface onnxruntime)",
            )

        try:
            app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
            app.prepare(ctx_id=-1, det_size=(640, 640))

            from ..utils.video import extract_frame
            ev = out_dir / "evidence"
            ev.mkdir(parents=True, exist_ok=True)
            gen_f = ev / f"{pair.clip_id}_gen_mid_face.jpg"
            extract_frame(pair.generated, gen_f, at=None)

            ref_faces = app.get(str(pair.character))
            gen_faces = app.get(str(gen_f))
            if not ref_faces or not gen_faces:
                return Metric(self.dim, self.layer, None, note="未检出人脸")
            import numpy as np

            a = ref_faces[0].normed_embedding
            b = gen_faces[0].normed_embedding
            cos = float(np.dot(a, b))
            score = float(max(0.0, min(100.0, cos * 100)))
            return Metric(self.dim, self.layer, score, raw_value=round(cos, 4),
                          detail={"note": "余弦相似度*100"})
        except Exception as exc:  # noqa: BLE001  - 可选组件, 任何异常都不应中断评估
            return Metric(self.dim, self.layer, None, note=f"人脸评估失败: {exc}")


class L2Evaluators:
    @staticmethod
    def all() -> list:
        return [VLMSemanticEvaluator(), FaceIdentityEvaluator()]
