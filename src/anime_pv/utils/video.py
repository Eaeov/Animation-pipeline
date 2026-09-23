"""视频处理工具.

依赖 ffmpeg (命令行) + OpenCV (帧级分析).
ffmpeg 缺失时给出明确安装指引, 而不是抛晦涩的 FileNotFoundError.

查找顺序 (find_ffmpeg):
  1. 环境变量 ANIME_PV_FFMPEG_DIR 指定的 bin 目录  (CI / 自定义安装)
  2. 项目自带 tools/ffmpeg/bin                     (setup_ffmpeg.sh 装的)
  3. 系统 PATH
这样"按脚本装好了却说找不到"的情况不会发生.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..errors import InputValidationError, StageError

# <repo>/src/anime_pv/utils/video.py -> parents[3] = <repo>
_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUNDLED_BIN = _REPO_ROOT / "tools" / "ffmpeg" / "bin"

_EXE = ".exe" if os.name == "nt" else ""


def _looks_like_bin(d: Path, name: str) -> Path | None:
    """目录下是否存在可用的 ffmpeg/ffprobe 可执行文件."""
    cand = d / f"{name}{_EXE}"
    if cand.is_file():
        return cand
    # 兼容没有扩展名的 POSIX 风格安装
    cand2 = d / name
    if cand2.is_file():
        return cand2
    return None


def find_ffmpeg(name: str) -> str | None:
    """定位 ffmpeg 或 ffprobe, 返回可执行文件路径; 找不到返回 None."""
    env_dir = os.environ.get("ANIME_PV_FFMPEG_DIR")
    if env_dir:
        hit = _looks_like_bin(Path(env_dir), name)
        if hit:
            return str(hit)

    if _BUNDLED_BIN.is_dir():
        hit = _looks_like_bin(_BUNDLED_BIN, name)
        if hit:
            return str(hit)

    return shutil.which(name)


def ffmpeg_available() -> bool:
    return find_ffmpeg("ffmpeg") is not None and find_ffmpeg("ffprobe") is not None


def _ff(name: str) -> str:
    """取可执行文件路径; 缺了就抛带指引的错 (而不是裸 FileNotFoundError)."""
    exe = find_ffmpeg(name)
    if exe is None:
        require_ffmpeg()
        raise StageError(f"找不到 {name}", stage="ingest")  # 理论上不可达
    return exe


def ffmpeg_source() -> str:
    """返回 ffmpeg 来源描述, 供 doctor 显示 (便于排查"到底用的是哪个")."""
    exe = find_ffmpeg("ffmpeg")
    if not exe:
        return "缺失"
    p = Path(exe).resolve()
    try:
        if _BUNDLED_BIN.resolve() in p.parents:
            return f"项目自带 ({p.parent})"
    except OSError:
        pass
    if os.environ.get("ANIME_PV_FFMPEG_DIR"):
        return f"环境变量 ANIME_PV_FFMPEG_DIR ({p.parent})"
    return f"系统 PATH ({p.parent})"


def require_ffmpeg() -> None:
    if not ffmpeg_available():
        raise StageError(
            "未检测到 ffmpeg/ffprobe, 视频处理无法进行.\n"
            "推荐 (项目自带, 不污染系统 PATH):\n"
            "  bash tools/setup_ffmpeg.sh\n"
            "其他方式:\n"
            "  winget install Gyan.FFmpeg\n"
            "  choco install ffmpeg\n"
            "或下载解压后设置 ANIME_PV_FFMPEG_DIR 指向其 bin 目录.",
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
    """读取视频元信息.

    优先 ffprobe; 若 ffprobe 异常 (崩溃/无输出/解析失败) 则回退到 OpenCV。
    加这层兜底的原因: 部分 ffmpeg 构建存在 ffprobe 段错误问题 (见 docs/04 失败案例),
    不能因为探测工具的缺陷让整个管线不可用。
    """
    if not path.exists():
        raise InputValidationError(f"文件不存在: {path}", stage="ingest")

    if ffmpeg_available():
        try:
            return _probe_ffprobe(path)
        except (InputValidationError, ValueError, json.JSONDecodeError):
            pass  # 落到 OpenCV 兜底
    return _probe_opencv(path)


def _probe_ffprobe(path: Path) -> VideoInfo:
    cmd = [
        _ff("ffprobe"), "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,nb_frames,codec_name",
        "-show_entries", "format=duration,size",
        "-of", "json", str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    # 段错误时 returncode 会是异常大值 (如 3221225477 / 139), stdout 为空
    if out.returncode != 0 or not (out.stdout or "").strip():
        raise InputValidationError(
            f"ffprobe 解析失败 (rc={out.returncode}): {out.stderr.strip()[:200]}",
            stage="ingest",
        )
    data = json.loads(out.stdout)
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


def _probe_opencv(path: Path) -> VideoInfo:
    """OpenCV 兜底探测. 无需 ffprobe, 但精度略低 (FPS 可能不准)."""
    try:
        import cv2
    except ImportError as exc:
        raise InputValidationError(
            f"ffprobe 不可用且未安装 OpenCV, 无法探测视频: {path}",
            stage="ingest", cause=exc,
        )
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise InputValidationError(f"无法打开视频: {path}", stage="ingest")
    try:
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        nb = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    finally:
        cap.release()

    # 部分编码下 CAP_PROP_FPS 返回 0/异常, 用帧数反推
    duration = nb / fps if fps > 1e-6 else 0.0
    if fps <= 1e-6 or fps > 240:
        fps = 24.0
        duration = nb / fps if nb else 0.0
    return VideoInfo(
        path=path, duration=duration, width=w, height=h, fps=fps, nb_frames=nb,
        codec="unknown(cv2)", size_mb=path.stat().st_size / (1024 * 1024),
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
    cmd = [ _ff("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(src)]
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
    cmd = [ _ff("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
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
    cmd = [ _ff("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1", str(dest),
    ]
    _run(cmd, "抽帧失败", "ingest")
    return dest


def extract_frames(video: Path, dest_dir: Path, *, every_s: float = 0.5, max_frames: int = 200) -> list[Path]:
    """按时间间隔抽帧, 用于时序分析与 VLM 评估."""
    require_ffmpeg()
    dest_dir.mkdir(parents=True, exist_ok=True)
    cmd = [ _ff("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
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
            [ _ff("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
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
        [ _ff("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", *inputs,
         "-filter_complex", ";".join(filter_parts), "-map", prev.strip("[]"),
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(dest)],
        "交叉淡化拼接失败", "assemble",
    )
    return dest


def _run(cmd: list[str], err_msg: str, stage: str) -> None:
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise StageError(f"{err_msg}: {out.stderr.strip()[:500]}", stage=stage)
