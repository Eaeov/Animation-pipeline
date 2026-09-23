"""配置加载.

所有可调参数集中在此, 代码中禁止硬编码魔法数字 (见 AGENTS.md 第 4 节铁律 3).
密钥只从环境变量读取, 绝不写入 yaml 或代码.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "configs"


@dataclass
class PlatformLimits:
    """百炼 wan2.2-animate-mix 的平台硬约束.

    这些值来自官方文档, 改动前请先核对:
    https://help.aliyun.com/zh/model-studio/wan-animate-mix-api
    """

    video_min_seconds: float = 2.0
    video_max_seconds: float = 30.0
    video_min_side: int = 200
    video_max_side: int = 2048
    video_max_mb: float = 200.0
    video_formats: tuple[str, ...] = (".mp4", ".avi", ".mov")

    image_min_side: int = 200
    image_max_side: int = 4096
    image_max_mb: float = 5.0
    image_formats: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".bmp", ".webp")

    aspect_min: float = 1 / 3
    aspect_max: float = 3.0

    def check_video(self, seconds: float, w: int, h: int, size_mb: float, ext: str) -> list[str]:
        """返回违规项列表, 空列表表示合规 (不抛异常, 便于批量预检)."""
        problems: list[str] = []
        if not (self.video_min_seconds < seconds <= self.video_max_seconds):
            problems.append(
                f"时长 {seconds:.2f}s 不在 ({self.video_min_seconds}, {self.video_max_seconds}] 区间"
            )
        if not (self.video_min_side <= w <= self.video_max_side):
            problems.append(f"宽 {w} 不在 [{self.video_min_side}, {self.video_max_side}]")
        if not (self.video_min_side <= h <= self.video_max_side):
            problems.append(f"高 {h} 不在 [{self.video_min_side}, {self.video_max_side}]")
        if size_mb > self.video_max_mb:
            problems.append(f"大小 {size_mb:.1f}MB 超过 {self.video_max_mb}MB")
        if ext.lower() not in self.video_formats:
            problems.append(f"格式 {ext} 不在 {self.video_formats}")
        aspect = w / h if h else 0
        if not (self.aspect_min <= aspect <= self.aspect_max):
            problems.append(f"宽高比 {aspect:.2f} 不在 [{self.aspect_min:.2f}, {self.aspect_max:.2f}]")
        return problems

    def check_image(self, w: int, h: int, size_mb: float, ext: str) -> list[str]:
        problems: list[str] = []
        if not (self.image_min_side <= w <= self.image_max_side):
            problems.append(f"宽 {w} 不在 [{self.image_min_side}, {self.image_max_side}]")
        if not (self.image_min_side <= h <= self.image_max_side):
            problems.append(f"高 {h} 不在 [{self.image_min_side}, {self.image_max_side}]")
        if size_mb > self.image_max_mb:
            problems.append(f"大小 {size_mb:.1f}MB 超过 {self.image_max_mb}MB")
        if ext.lower() not in self.image_formats:
            problems.append(f"格式 {ext} 不在 {self.image_formats}")
        aspect = w / h if h else 0
        if not (self.aspect_min <= aspect <= self.aspect_max):
            problems.append(f"宽高比 {aspect:.2f} 不在 [{self.aspect_min:.2f}, {self.aspect_max:.2f}]")
        return problems


@dataclass
class ApiConfig:
    """平台接入配置. api_key 仅从环境变量注入."""

    base_url: str = "https://dashscope.aliyuncs.com"
    model: str = "wan2.2-animate-mix"
    submit_path: str = "/api/v1/services/aigc/image2video/video-synthesis"
    query_path: str = "/api/v1/tasks/{task_id}"
    poll_interval_s: int = 15
    poll_timeout_s: int = 900
    max_retries: int = 3
    check_image: bool = True

    _env_key_name: str = "DASHSCOPE_API_KEY"

    @property
    def api_key(self) -> str:
        key = os.environ.get(self._env_key_name, "").strip()
        if not key:
            raise ConfigError(
                f"环境变量 {self._env_key_name} 未设置. "
                f"请执行: export {self._env_key_name}=sk-xxxx",
                stage="config",
            )
        return key

    @property
    def has_key(self) -> bool:
        return bool(os.environ.get(self._env_key_name, "").strip())


@dataclass
class PipelineConfig:
    """管线主配置."""

    limits: PlatformLimits = field(default_factory=PlatformLimits)
    api: ApiConfig = field(default_factory=ApiConfig)

    clips: list[str] = field(default_factory=lambda: ["A", "B"])
    default_mode: str = "wan-std"
    output_resolution: str = "720P"

    runs_dir: Path = REPO_ROOT / "runs"
    assets_dir: Path = REPO_ROOT / "assets"

    @classmethod
    def load(cls, name: str = "default") -> "PipelineConfig":
        """从 configs/<name>.yaml 加载, 缺失则用默认值."""
        cfg = cls()
        path = CONFIG_DIR / f"{name}.yaml"
        if not path.exists():
            return cfg
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PipelineConfig":
        cfg = cls()
        if "api" in raw:
            allowed = {f for f in ApiConfig.__dataclass_fields__ if not f.startswith("_")}
            for k, v in raw["api"].items():
                if k in allowed:
                    setattr(cfg.api, k, v)
        if "limits" in raw:
            allowed = set(PlatformLimits.__dataclass_fields__)
            for k, v in raw["limits"].items():
                if k in allowed:
                    setattr(cfg.limits, k, v)
        for k in ("clips", "default_mode", "output_resolution"):
            if k in raw:
                setattr(cfg, k, raw[k])
        return cfg
