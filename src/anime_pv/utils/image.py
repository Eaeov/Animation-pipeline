"""图片处理工具 (人设图合规化).

人设图往往是从网页/推特存下来的, 规格参差 (超 5MB / 宽高比超 1:3 / 格式不对),
平台会直接拒绝. 这里统一做规整, 让 stage 层无脑调用.
"""

from __future__ import annotations

import io
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..errors import InputValidationError, StageError


@dataclass
class ImageInfo:
    path: Path
    width: int
    height: int
    size_mb: float
    fmt: str

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "width": self.width,
            "height": self.height,
            "size_mb": round(self.size_mb, 2),
            "format": self.fmt,
            "aspect": round(self.aspect, 4),
        }


def _pil():
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise StageError(
            "需要 Pillow: pip install Pillow", stage="character", cause=exc
        )
    return Image


def info(path: Path) -> ImageInfo:
    Image = _pil()
    if not path.exists():
        raise InputValidationError(f"图片不存在: {path}", stage="character")
    with Image.open(path) as im:
        w, h = im.size
        fmt = im.format or path.suffix.lstrip(".").upper()
    return ImageInfo(path, w, h, path.stat().st_size / (1024 * 1024), fmt)


def normalize(
    src: Path,
    dest: Path,
    *,
    max_side: int = 2048,
    min_side: int = 256,
    max_mb: float = 5.0,
    aspect_range: tuple[float, float] = (1 / 3, 3.0),
    background: tuple[int, int, int] = (255, 255, 255),
) -> Path:
    """把人设图规整到平台可接受的规格.

    处理步骤:
      1. 转 RGB (去除 alpha / 调色板模式)
      2. 超长边等比缩放; 过小则放大到 min_side
      3. 宽高比越界时用补边 (pad) 而非裁剪 —— 保留人物完整性
      4. 质量递减压缩至 <= max_mb
    """
    Image = _pil()
    dest.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(src) as im:
        im = im.convert("RGB")
        w, h = im.size

        scale = 1.0
        if max(w, h) > max_side:
            scale = max_side / max(w, h)
        elif min(w, h) < min_side:
            scale = min_side / min(w, h)
        if scale != 1.0:
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            w, h = im.size

        lo, hi = aspect_range
        aspect = w / h
        if aspect < lo or aspect > hi:
            target = lo if aspect < lo else hi
            if target == lo:  # 太窄 -> 加宽
                new_w, new_h = int(h * lo), h
            else:  # 太宽 -> 加高
                new_w, new_h = w, int(w / hi)
            canvas = Image.new("RGB", (new_w, new_h), background)
            canvas.paste(im, ((new_w - w) // 2, (new_h - h) // 2))
            im = canvas

        out = dest.with_suffix(".jpg")
        for quality in (95, 90, 85, 80, 70, 60):
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() / (1024 * 1024) <= max_mb:
                out.write_bytes(buf.getvalue())
                return out
        out.write_bytes(buf.getvalue())
        if out.stat().st_size / (1024 * 1024) > max_mb:
            raise InputValidationError(
                f"压缩后仍超过 {max_mb}MB: {out}", stage="character"
            )
    return out


def align_to_clip(
    char_path: Path,
    clip_path: Path,
    dest: Path,
    *,
    canvas: tuple[int, int] | None = None,
) -> Path:
    """把人设图对齐到目标片段的画幅比例.

    为什么需要: 官方 FAQ 明确指出 —— 输入图片与参考视频中人物画幅占比
    越接近, 换人质量越好; 画幅不匹配是弱结果的首要原因.
    因此我们按参考视频的宽高比把人设图补边到同比例画幅.

    这里只做画幅对齐 (外部约束), 不做人物检测裁剪 —— 后者留作扩展点.
    """
    from .video import probe  # 局部导入避免循环引用

    Image = _pil()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if canvas is None:
        v = probe(clip_path)
        canvas = (v.width, v.height)

    cw, ch = canvas
    with Image.open(char_path) as im:
        im = im.convert("RGB")
        w, h = im.size
        scale = min(cw / w, ch / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        im = im.resize((nw, nh), Image.LANCZOS)
        bg = Image.new("RGB", (cw, ch), (255, 255, 255))
        bg.paste(im, ((cw - nw) // 2, (ch - nh) // 2))
        bg.save(dest, format="PNG")
    return dest


def side_by_side(paths: list[Path], dest: Path, *, label: bool = True) -> Path:
    """横向拼接多张图, 用于生成评估证据图."""
    Image = _pil()
    ims = []
    for p in paths:
        with Image.open(p) as im:
            ims.append(im.convert("RGB").copy())
    if not ims:
        raise StageError("没有可拼接的图片", stage="eval")
    h = max(i.height for i in ims)
    scaled = []
    for i in ims:
        if i.height != h:
            i = i.resize((int(i.width * h / i.height), h), Image.LANCZOS)
        scaled.append(i)
    total_w = sum(i.width for i in scaled)
    canvas = Image.new("RGB", (total_w, h), (255, 255, 255))
    x = 0
    for i in scaled:
        canvas.paste(i, (x, 0))
        x += i.width
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.suffix.lower() in {".jpg", ".jpeg"}:
        canvas.save(dest, format="JPEG", quality=92)
    else:
        canvas.save(dest, format="PNG")
    return dest


def copy_asset(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    shutil.copyfile(src, dest)
    return dest
