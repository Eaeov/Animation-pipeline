"""视频处理工具.

依赖 ffmpeg (命令行) + OpenCV (帧级分析).
ffmpeg 缺失时给出明确安装指引, 而不是抛晦涩的 FileNotFoundError.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..errors import InputValidationError, StageError


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def require_ffmpeg() -> None:
    if not ffmpeg_available():
        raise StageError(
            "未检测到 ffmpeg/ffprobe, 视频处理无法进行.\n"
            "Windows 安装 (任选其一):\n"
            "  winget install Gyan.FFmpeg\n"
            "  choco install ffmpeg\n"
            "或下载解压后把 bin 目录加入 PATH.",
            stage="ingest",
        )


@dataclass
class VideoInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    nb_frames: int
    codec: str
    size_mb: float

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "duration": round(self.duration, 3),
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 3),
            "nb_frames": self.nb_frames,
            "codec": self.codec,
            "size_mb": round(self.size_mb, 2),
            "aspect": round(self.aspect, 4),
        }


def probe(path: Path) -> VideoInfo:
    """用 ffprobe 读取视频元信息."""
    require_ffmpeg()
    if not path.exists():
        raise InputValidationError(f"文件不存在: {path}", stage="ingest")
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,nb_frames,codec_name",
        "-show_entries", "format=duration,size",
        "-of", "json", str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise InputValidationError(
            f"ffprobe 解析失败: {out.stderr.strip()}", stage="ingest"
        )
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams") or [{}]
    fmt = data.get("format") or {}
    st = streams[0]

    def _fps(expr: str | None) -> float:
        if not expr:
            return 0.0
        if "/" in expr:
            num, den = expr.split("/", 1)
            try:
                return float(num) / float(den) if float(den) else 0.0
            except ValueError:
                return 0.0
        try:
            return float(expr)
        except ValueError:
            return 0.0

    duration = float(fmt.get("duration", 0.0) or 0.0)
    fps = _fps(st.get("r_frame_rate"))
    nb = int(st.get("nb_frames") or 0)
    if not nb and duration and fps:
        nb = int(duration * fps)
    return VideoInfo(
        path=path,
        duration=duration,
        width=int(st.get("width") or 0),
        height=int(st.get("height") or 0),
        fps=fps,
        nb_frames=nb,
        codec=str(st.get("codec_name") or "unknown"),
        size_mb=float(fmt.get("size", 0) or 0) / (1024 * 1024),
    )


def cut(
    src: Path,
    dest: Path,
    *,
    start: float,
    duration: float,
    reencode: bool = True,
) -> Path:
    """截取片段.

    默认重编码以保证起止点精确 (流拷贝会吸附到关键帧, 导致时长不准,
    可能触碰平台 2s 下限).
    """
    require_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(src)]
    cmd += ["-t", f"{duration:.3f}"]
    if reencode:
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-an"]
    else:
        cmd += ["-c", "copy", "-an"]
    cmd += ["-movflags", "+faststart", str(dest)]
    _run(cmd, "切片失败", "ingest")
    return dest


def normalize(
    src: Path,
    dest: Path,
    *,
    max_side: int = 1280,
    fps: float | None = None,
    crf: int = 18,
) -> Path:
    """归一化: 限制最长边 + 可选重采样帧率 + 统一像素格式与编码.

    目的: 让 A/B 片段的基础规格一致, 消除"因输入规格不同导致质量差异"
    这一混杂因素 —— 这是保证可复用性的前提.
    """
    require_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    vf = [f"scale='if(gt(iw,ih),min({max_side},iw),-2)':'if(gt(iw,ih),-2,min({max_side},ih))'"]
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    cmd += ["-vf", ",".join(vf)]
    if fps:
        cmd += ["-r", str(fps)]
    cmd += [
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", str(dest),
    ]
    _run(cmd, "归一化失败", "ingest")
    return dest


def extract_frame(video: Path, dest: Path, *, at: float | None = None) -> Path:
    """抽取单帧 (默认取中间帧). 用于关键帧比对与证据留存."""
    require_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if at is None:
        at = probe(video).duration / 2
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1", str(dest),
    ]
    _run(cmd, "抽帧失败", "ingest")
    return dest


def extract_frames(video: Path, dest_dir: Path, *, every_s: float = 0.5, max_frames: int = 200) -> list[Path]:
    """按时间间隔抽帧, 用于时序分析与 VLM 评估."""
    require_ffmpeg()
    dest_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-vf", f"fps=1/{every_s}", "-frames:v", str(max_frames),
        str(dest_dir / "frame_%04d.jpg"),
    ]
    _run(cmd, "批量抽帧失败", "ingest")
    return sorted(dest_dir.glob("frame_*.jpg"))


def concat(parts: list[Path], dest: Path, *, crossfade_s: float = 0.0) -> Path:
    """拼接多个片段.

    crossfade_s > 0 时用 xfade 做交叉淡化转场; 为 0 则直接拼接.
    片段需规格一致 (同一 pipeline 产出的天然满足).
    """
    require_ffmpeg()
    if not parts:
        raise StageError("没有可拼接的片段", stage="assemble")
    dest.parent.mkdir(parents=True, exist_ok=True)

    if len(parts) == 1:
        shutil.copyfile(parts[0], dest)
        return dest

    if crossfade_s <= 0:
        lst = dest.parent / "_concat_list.txt"
        lst.write_text(
            "\n".join(f"file '{p.resolve().as_posix()}'" for p in parts),
            encoding="utf-8",
        )
        _run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(dest)],
            "拼接失败", "assemble",
        )
        lst.unlink(missing_ok=True)
        return dest

    # xfade 链式转场
    inputs: list[str] = []
    for p in parts:
        inputs += ["-i", str(p)]
    durations = [probe(p).duration for p in parts]
    filter_parts = []
    prev = "[0:v]"
    offset = durations[0] - crossfade_s
    for i in range(1, len(parts)):
        label = f"[v{i}]"
        filter_parts.append(
            f"{prev}[{i}:v]xfade=transition=fade:duration={crossfade_s}:offset={offset:.3f}{label}"
        )
        prev = label
        offset += durations[i] - crossfade_s
    _run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
         "-filter_complex", ";".join(filter_parts), "-map", prev.strip("[]"),
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(dest)],
        "交叉淡化拼接失败", "assemble",
    )
    return dest


def _run(cmd: list[str], err_msg: str, stage: str) -> None:
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise StageError(f"{err_msg}: {out.stderr.strip()[:500]}", stage=stage)
