"""Mock 适配器 —— 离线跑通全链路, 不花钱、不需素材.

存在意义 (对应 AGENTS.md 第 7 节):
- 素材/密钥未就绪时先验证 pipeline 结构正确
- CI 里做回归测试
- 评估器调试时提供确定性输入

它刻意引入可控扰动 (亮度偏移/帧数变化/轻微模糊), 用于验证评估器
真的能检出质量差异 —— 如果评估器对 mock 的扰动毫无反应, 说明评估器是坏的.
"""

from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path
from typing import Any

from ..errors import AdapterError
from .base import TaskStatus, TransferResult


class MockAdapter:
    """确定性 mock: 输出 = 输入视频的副本 + 可控扰动.

    Args:
        jitter: 扰动强度 0.0-1.0. 0 = 完美复刻 (评估应给满分);
            0.5 = 中等退化; 1.0 = 严重退化 (评估应给低分).
        fail_rate: 模拟失败概率, 用于测试重试与失败路径.
        latency_s: 模拟 API 延迟, 用于测试轮询逻辑.
    """

    name = "mock"

    def __init__(self, jitter: float = 0.0, fail_rate: float = 0.0, latency_s: float = 0.0) -> None:
        self.jitter = max(0.0, min(1.0, jitter))
        self.fail_rate = fail_rate
        self.latency_s = latency_s

    def submit(
        self,
        image_path: Path,
        video_path: Path,
        *,
        mode: str = "wan-std",
        **kwargs: Any,
    ) -> str:
        if not image_path.exists():
            raise AdapterError(f"人设图不存在: {image_path}", stage="transfer")
        if not video_path.exists():
            raise AdapterError(f"参考视频不存在: {video_path}", stage="transfer")
        if self.latency_s:
            time.sleep(self.latency_s)
        seed = f"{image_path.name}|{video_path.name}|{mode}|{self.jitter}"
        return "mock-" + hashlib.sha256(seed.encode()).hexdigest()[:16]

    def poll(self, task_id: str) -> TaskStatus:
        # 用 task_id 的 hash 决定是否模拟失败, 保证同 id 结果稳定
        digits = int(task_id.split("-")[-1][:4], 16) / 0xFFFF
        if digits < self.fail_rate:
            return TaskStatus(
                task_id=task_id,
                status="FAILED",
                code="MockFailure",
                message="模拟的生成失败 (用于测试失败路径)",
            )
        return TaskStatus(task_id=task_id, status="SUCCEEDED", billable_seconds=5.0)

    def download(self, url: str, dest: Path) -> Path:
        raise AdapterError("Mock 适配器不支持 URL 下载, 请使用 transfer()", stage="transfer")

    def wait(self, task_id: str) -> TaskStatus:
        return self.poll(task_id)

    def transfer(
        self,
        image_path: Path,
        video_path: Path,
        *,
        mode: str = "wan-std",
        dest: Path,
        **kwargs: Any,
    ) -> TransferResult:
        task_id = self.submit(image_path, video_path, mode=mode, **kwargs)
        status = self.wait(task_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(video_path, dest)

        degraded = self.jitter > 0
        if degraded:
            _apply_jitter(dest, self.jitter)

        return TransferResult(
            video_path=dest,
            task_id=task_id,
            billable_seconds=5.0,
            mode=mode,
            raw_response={
                "mock": True,
                "jitter": self.jitter,
                "status": status.status,
                "note": "离线产物, 非真实模型输出",
            },
        )


def _apply_jitter(path: Path, intensity: float) -> None:
    """对输出视频叠加可控退化, 供评估器自检.

    优先用 OpenCV 处理; 不可用时静默跳过 (mock 仍可用于流程测试).
    """
    try:
        import cv2  # noqa: PLC0415
    except ImportError:
        return

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()
    if not frames:
        return

    k = max(1, int(1 + intensity * 10))
    blur = (k if k % 2 == 1 else k + 1)
    out_frames = []
    for i, frame in enumerate(frames):
        f = cv2.GaussianBlur(frame, (blur, blur), 0) if blur > 1 else frame
        # 亮度随帧序漂移 -> 制造闪烁, 检验时序稳定性指标
        shift = int(intensity * 40 * ((i % 2) * 2 - 1))
        f = cv2.convertScaleAbs(f, alpha=1.0, beta=shift)
        out_frames.append(f)

    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h)
    )
    for f in out_frames:
        writer.write(f)
    writer.release()
