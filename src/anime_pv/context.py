"""运行上下文与 manifest 管理.

设计核心 (对应 ARCHITECTURE.md 第 3、4 节):
- Stage 之间只通过 RunContext + 文件系统交换数据
- 每个 stage 落盘 manifest.json, 支持幂等跳过与实验复现
- input_hash 把"输入内容 + 参数"绑定, 输入变了自动失效缓存
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import PipelineConfig
from .errors import PipelineError


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def sha256_obj(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@dataclass
class RunContext:
    """一次运行的上下文. 所有 stage 都接收它."""

    run_id: str
    cfg: PipelineConfig
    adapter_name: str = "mock"
    mode: str = "wan-std"
    run_dir: Path = field(default_factory=Path)
    adapter: Any = None
    notes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_dir or str(self.run_dir) == ".":
            self.run_dir = self.cfg.runs_dir / self.run_id

    # -------------------------------------------------------------- 路径管理

    def clip_dir(self, clip_id: str) -> Path:
        return self.run_dir / clip_id

    def stage_dir(self, clip_id: str, stage: str) -> Path:
        d = self.clip_dir(clip_id) / f"{_stage_index(stage)}_{stage}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def manifest_path(self, clip_id: str, stage: str) -> Path:
        return self.stage_dir(clip_id, stage) / "manifest.json"

    # -------------------------------------------------------------- manifest

    def load_manifest(self, clip_id: str, stage: str) -> dict[str, Any] | None:
        p = self.manifest_path(clip_id, stage)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def cached_ok(
        self, clip_id: str, stage: str, input_hash: str, force: bool = False
    ) -> dict[str, Any] | None:
        """命中缓存则返回 manifest, 否则 None. force=True 视为永远未命中."""
        if force:
            return None
        m = self.load_manifest(clip_id, stage)
        if m and m.get("status") == "succeeded" and m.get("input_hash") == input_hash:
            return m
        return None

    def write_manifest(self, clip_id: str, stage: str, payload: dict[str, Any]) -> Path:
        p = self.manifest_path(clip_id, stage)
        p.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return p

    # -------------------------------------------------------------- 运行记录

    def init_run(self, extra: dict[str, Any] | None = None) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "run_id": self.run_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "git_commit": git_commit(),
            "adapter": self.adapter_name,
            "mode": self.mode,
            "clips": self.cfg.clips,
            "config": {
                "model": self.cfg.api.model,
                "base_url": self.cfg.api.base_url,
                "poll_interval_s": self.cfg.api.poll_interval_s,
                "output_resolution": self.cfg.output_resolution,
            },
            "notes": self.notes,
            "extra": extra or {},
        }
        (self.run_dir / "run_manifest.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )


_STAGE_ORDER = ["ingest", "character", "style_align", "transfer", "assemble", "eval"]


def _stage_index(stage: str) -> str:
    try:
        return f"{_STAGE_ORDER.index(stage) + 1:02d}"
    except ValueError:
        return "99"


class StageTimer:
    """上下文管理器, 记录阶段耗时并写入 manifest."""

    def __init__(self, ctx: RunContext, clip_id: str, stage: str, input_hash: str,
                 params: dict[str, Any] | None = None) -> None:
        self.ctx = ctx
        self.clip_id = clip_id
        self.stage = stage
        self.input_hash = input_hash
        self.params = params or {}
        self.started: float = 0.0
        self.outputs: list[str] = []
        self.extra: dict[str, Any] = {}

    def __enter__(self) -> "StageTimer":
        self.started = time.time()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        elapsed = round(time.time() - self.started, 2)
        payload: dict[str, Any] = {
            "stage": self.stage,
            "clip_id": self.clip_id,
            "run_id": self.ctx.run_id,
            "input_hash": self.input_hash,
            "params": self.params,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started)),
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_s": elapsed,
            "status": "succeeded" if exc is None else "failed",
            "outputs": self.outputs,
            "error": None if exc is None else _as_pipeline_error(exc, self.stage, self.clip_id),
        }
        payload.update(self.extra)
        try:
            self.ctx.write_manifest(self.clip_id, self.stage, payload)
        except OSError:
            pass  # manifest 写入失败不应掩盖真实异常
        return False  # 不吞异常


def _as_pipeline_error(exc: BaseException, stage: str, clip_id: str) -> dict[str, Any]:
    if isinstance(exc, PipelineError):
        exc.stage = exc.stage or stage
        exc.clip_id = exc.clip_id or clip_id
        return exc.to_dict()
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "stage": stage,
        "clip_id": clip_id,
        "request_id": None,
        "cause": None,
    }
