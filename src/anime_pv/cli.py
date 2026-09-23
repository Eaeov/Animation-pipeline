"""统一命令行入口.

用法示例:
    # 离线自检 (不需要密钥/素材, 秒级完成)
    python -m anime_pv.cli smoke

    # 环境体检
    python -m anime_pv.cli doctor

    # 扫描 PV, 推荐切片方案 (素材就绪后第一步)
    python -m anime_pv.cli scan --video assets/source/pv.mp4

    # 单个片段跑完整管线
    python -m anime_pv.cli run --clip A --adapter mock

    # 跑 A/B 并对比稳定性 (本题核心用法)
    python -m anime_pv.cli run --clips A,B --adapter dashscope --mode wan-std

    # 只跑某个阶段
    python -m anime_pv.cli stage --clip A --stage transfer

    # 出评估报告
    python -m anime_pv.cli eval --run-id 20260923-1400_baseline
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .adapters import build_adapter
from .config import PipelineConfig
from .context import RunContext
from .errors import PipelineError
from .stages import PIPELINE, get_stage

# --------------------------------------------------------------------- helpers


def make_run_id(tag: str | None = None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M")
    return f"{stamp}_{tag}" if tag else stamp


def build_ctx(args: argparse.Namespace, cfg: PipelineConfig) -> RunContext:
    adapter_kwargs = {}
    if args.adapter == "mock":
        adapter_kwargs["jitter"] = float(getattr(args, "jitter", 0.0) or 0.0)
    adapter = build_adapter(args.adapter, cfg.api, **adapter_kwargs)
    return RunContext(
        run_id=args.run_id or make_run_id(getattr(args, "tag", None) or args.adapter),
        cfg=cfg,
        adapter_name=args.adapter,
        mode=args.mode,
        adapter=adapter,
    )


def _stage_list(args: argparse.Namespace) -> list[str]:
    """确定要跑哪些阶段: 指定则只跑指定, 否则跑全流程."""
    if getattr(args, "stage", None):
        return [args.stage]
    if getattr(args, "until", None):
        idx = PIPELINE.index(args.until)
        return list(PIPELINE[: idx + 1])
    return list(PIPELINE)


# --------------------------------------------------------------------- 子命令


def cmd_doctor(args: argparse.Namespace) -> int:
    """环境体检: 逐项检查依赖/密钥/ffmpeg/素材, 给出可执行修复建议."""
    print("=" * 60)
    print("环境体检")
    print("=" * 60)
    ok = True

    # Python
    print(f"[{'OK ' if sys.version_info >= (3, 10) else '!! '}] Python {sys.version.split()[0]}"
          f"{'' if sys.version_info >= (3, 10) else '  (需要 >= 3.10)'}")
    ok &= sys.version_info >= (3, 10)

    # 依赖
    for mod, pipname in [
        ("requests", "requests"), ("yaml", "PyYAML"), ("cv2", "opencv-python"),
        ("PIL", "Pillow"), ("numpy", "numpy"), ("skimage", "scikit-image"),
    ]:
        try:
            __import__(mod)
            print(f"[OK ] 依赖 {pipname}")
        except ImportError:
            print(f"[-- ] 依赖 {pipname} 缺失  -> pip install {pipname}")
            ok = False

    # ffmpeg
    from .utils.video import ffmpeg_available, ffmpeg_source, _ff
    if ffmpeg_available():
        ver = "?"
        try:
            import subprocess as _sp
            r = _sp.run([_ff("ffmpeg"), "-version"], capture_output=True, text=True, check=False)
            first = (r.stdout or "").splitlines()[0] if r.stdout else ""
            ver = first.split(" Copyright")[0].replace("ffmpeg version ", "").strip() or "?"
        except Exception:
            pass
        print(f"[OK ] ffmpeg {ver}  ({ffmpeg_source()})")
        # ffprobe 单独自检 —— 9.0.2 就是装得上但 ffprobe 段错误
        try:
            import subprocess as _sp
            rp = _sp.run([_ff("ffprobe"), "-version"], capture_output=True, text=True, check=False)
            if rp.returncode != 0:
                print(f"[!! ] ffprobe 异常 (rc={rp.returncode}) -> 换 ffmpeg 版本 (建议 6.1.1)")
                ok = False
        except Exception as e:
            print(f"[!! ] ffprobe 自检失败: {e}")
            ok = False
    else:
        print("[-- ] ffmpeg 缺失  -> bash tools/setup_ffmpeg.sh  (推荐)")
        print("                     或 winget install Gyan.FFmpeg")
        ok = False

    # 密钥
    cfg = PipelineConfig.load(args.config)
    if cfg.api.has_key:
        k = cfg.api.api_key
        print(f"[OK ] DASHSCOPE_API_KEY 已设置 ({k[:6]}...{k[-4:]})")
    else:
        print("[-- ] DASHSCOPE_API_KEY 未设置  -> export DASHSCOPE_API_KEY=sk-xxxx")
        print("     (不影响 mock 适配器与离线自检)")

    # 素材
    src = cfg.assets_dir / "source"
    char = cfg.assets_dir / "character"
    vid_ext = {".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm"}
    img_ext = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    pv_files = [p for p in src.glob("*") if p.suffix.lower() in vid_ext] if src.is_dir() else []
    char_files = [p for p in char.glob("*") if p.suffix.lower() in img_ext] if char.is_dir() else []
    n_src, n_char = len(pv_files), len(char_files)

    if n_src:
        print(f"[OK ] 原始 PV 素材: {n_src} 个")
        for p in pv_files[:3]:
            print(f"       - {p.name} ({p.stat().st_size / 1048576:.1f} MB)")
    else:
        print("[-- ] 原始 PV 素材: 0 个  -> 把 PV 放到 assets/source/")
        ok = False

    if n_char:
        print(f"[OK ] 人设图素材: {n_char} 个")
        for p in char_files[:3]:
            print(f"       - {p.name} ({p.stat().st_size / 1024:.0f} KB)")
    else:
        print("[-- ] 人设图素材: 0 个  -> 把立绘放到 assets/character/ (命名 A.png / B.png)")
        ok = False

    # clips.yaml 是否还是模板
    from .config import CONFIG_DIR

    clips_yaml = CONFIG_DIR / "clips.yaml"
    placeholder = False
    if clips_yaml.exists():
        txt = clips_yaml.read_text(encoding="utf-8")
        placeholder = "TODO" in txt
    if placeholder:
        print("[-- ] clips.yaml 仍是模板 (含 TODO)  -> 跑 scan 生成推荐方案")
        ok = False
    elif clips_yaml.exists():
        print("[OK ] clips.yaml 已填写")

    print("-" * 60)
    if ok:
        print("结论: 环境就绪")
        print("下一步: python -m anime_pv.cli scan    # 扫描 PV 推荐切片")
    else:
        print("结论: 存在缺项, 见上方 -- 行")
        print("提示: 即使存在缺项, `smoke` 与 `--adapter mock` 仍可运行。")
    return 0 if ok else 1


def cmd_smoke(args: argparse.Namespace) -> int:
    """离线自检: 用合成素材走通全链路, 验证框架完整性 (不需要密钥/真实素材).

    设计要点: 自造素材 + 注入临时切分方案, 因此**不依赖用户尚未填写的
    configs/clips.yaml**。这样任何人在 clone 之后立刻能验证框架是否可用。
    """
    import json
    import shutil

    import numpy as np
    try:
        import cv2
    except ImportError:
        print("smoke 需要 opencv-python: pip install opencv-python")
        return 1

    from .utils.video import ffmpeg_available

    cfg = PipelineConfig.load(args.config)
    run_id = args.run_id or make_run_id("smoke")
    jitter = float(getattr(args, "jitter", 0.0) or 0.0)
    ctx = RunContext(
        run_id=run_id,
        cfg=cfg,
        adapter_name="mock",
        mode=args.mode,
        adapter=build_adapter("mock", jitter=jitter),
    )
    ctx.init_run({"purpose": "smoke test with synthetic assets", "jitter": jitter})
    if jitter > 0:
        print(f"[扰动自检模式] jitter={jitter} — 预期 L1 指标应显著变差")

    tmp = ctx.run_dir / "_synthetic"
    tmp.mkdir(parents=True, exist_ok=True)

    print(f"run_id: {ctx.run_id}")
    print("生成合成视频 (6s, 640x360, 24fps)...")
    vid = tmp / "synth.mp4"
    if ffmpeg_available():
        # 用 ffmpeg 生成 H.264 素材 —— OpenCV 的 mp4v 编码部分 ffprobe 解析不出流信息
        _make_video_ffmpeg(vid, seconds=6, w=640, h=360, fps=24)
    else:
        writer = cv2.VideoWriter(str(vid), cv2.VideoWriter_fourcc(*"mp4v"), 24.0, (640, 360))
        for i in range(144):
            frame = np.full((360, 640, 3), 40, dtype=np.uint8)
            cx = 80 + int(i * 3.3)
            cv2.circle(frame, (cx, 180), 45, (90, 160, 230), -1)
            cv2.putText(frame, f"f{i:03d}", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                        (240, 240, 240), 2)
            writer.write(frame)
        writer.release()

    print("生成合成人设图 (含人脸形状占位)...")
    img = np.full((512, 384, 3), 235, dtype=np.uint8)
    cv2.circle(img, (192, 210), 80, (200, 180, 165), -1)
    cv2.rectangle(img, (120, 300), (264, 512), (70, 90, 160), -1)
    char = tmp / "synth_char.png"
    cv2.imwrite(str(char), img)

    # 注入临时切分方案与素材, 使 ingest/character 阶段可跑
    from .config import CONFIG_DIR

    clips_yaml = CONFIG_DIR / "clips.yaml"
    clips_backup = clips_yaml.read_text(encoding="utf-8") if clips_yaml.exists() else None
    char_dir = cfg.assets_dir / "character"
    char_dir.mkdir(parents=True, exist_ok=True)
    injected_char = char_dir / "A.png"
    injected_char2 = char_dir / "B.png"
    shutil.copyfile(char, injected_char)
    shutil.copyfile(char, injected_char2)

    clips_yaml.parent.mkdir(parents=True, exist_ok=True)
    synth_rel = vid.relative_to(_REPO_REL(ctx)).as_posix()
    clips_yaml.write_text(
        "source:\n"
        f'  video: "{synth_rel}"\n'
        '  title: "smoke-synthetic"\n'
        '  season: "n/a"\n'
        'clips:\n'
        '  A: {start: 0.0, duration: 6.0, role: baseline, complexity: low}\n'
        '  B: {start: 0.0, duration: 6.0, role: baseline, complexity: low}\n',
        encoding="utf-8",
    )

    from .stages import PIPELINE, get_stage

    failures: list[str] = []
    notes: list[str] = []
    try:
        if not ffmpeg_available():
            print("  ! 未检测到 ffmpeg, 视频类阶段将被跳过 (见 doctor)")
        for stage_name in PIPELINE:
            stage = get_stage(stage_name)
            for clip in ("A", "B"):
                try:
                    print(f"  -> [{clip}] {stage_name} ...", end=" ", flush=True)
                    stage.run(ctx, clip, force=True)
                    print("OK")
                except PipelineError as exc:
                    if ffmpeg_available():
                        print(f"FAIL: {exc}")
                        failures.append(f"{clip}/{stage_name}")
                    else:
                        print(f"skip (缺 ffmpeg)")
                        notes.append(f"{clip}/{stage_name}: {exc}")
    finally:
        # 清理注入的素材与配置, 恢复原状
        if clips_backup is not None:
            clips_yaml.write_text(clips_backup, encoding="utf-8")
        else:
            clips_yaml.unlink(missing_ok=True)
        for p in (injected_char, injected_char2):
            p.unlink(missing_ok=True)

    print("-" * 60)
    if failures:
        print(f"框架自检未通过, 失败阶段: {failures}")
        return 1
    if notes:
        print(f"框架自检通过 (部分阶段因缺 ffmpeg 跳过: {len(notes)} 项)")
    else:
        print("框架自检通过 — 五阶段全部跑通")
    print("产物目录:", ctx.run_dir)
    return 0


def _make_video_ffmpeg(dest: Path, *, seconds: int, w: int, h: int, fps: int) -> None:
    """用 ffmpeg 合成测试视频 (H.264, 含移动圆形模拟主体).

    使用 lavfi 的 testsrc + drawbox 叠加, 输出标准 H.264/yuv420p,
    保证 ffprobe 能正确解析、后续阶段能正常处理。
    """
    import subprocess

    from .utils.video import _ff

    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"testsrc=size={w}x{h}:rate={fps}:duration={seconds},"
        f"drawbox=x='mod(t*120,{w - 80})':y={h // 2 - 40}:w=80:h=80:"
        f"color=0x5AA0E6@0.9:t=fill,"
        f"drawtext=text='%{{eif\\:t\\:d}}s':x=20:y=20:fontsize=28:fontcolor=white"
    )
    cmd = [
        _ff("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate={fps}:duration={seconds}",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-an", str(dest),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0 or not dest.exists():
        # drawtext 需要 libfreetype, 缺失时降级为更简单的滤镜链
        cmd_simple = [
            _ff("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate={fps}:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-an", str(dest),
        ]
        out2 = subprocess.run(cmd_simple, capture_output=True, text=True, check=False)
        if out2.returncode != 0 or not dest.exists():
            raise PipelineError(
                f"合成测试视频失败: {out2.stderr.strip()[:300]}", stage="smoke"
            )


def _REPO_REL(ctx: RunContext):
    from .config import REPO_ROOT

    return REPO_ROOT


def cmd_run(args: argparse.Namespace) -> int:
    cfg = PipelineConfig.load(args.config)
    clips = args.clips.split(",") if args.clips else list(cfg.clips)
    ctx = build_ctx(args, cfg)
    ctx.init_run()
    stages = _stage_list(args)

    print(f"run_id={ctx.run_id}  adapter={ctx.adapter_name}  mode={ctx.mode}")
    print(f"clips={clips}  stages={stages}")
    print("-" * 60)

    failed: list[tuple[str, str]] = []
    for clip in clips:
        print(f"\n[{clip}]")
        for sname in stages:
            stage = get_stage(sname)
            try:
                res = stage.run(ctx, clip, force=args.force)
                mark = "cached" if getattr(res, "reused", False) else "done"
                print(f"  {sname:<12} {mark}")
            except PipelineError as exc:
                print(f"  {sname:<12} FAILED: {exc}")
                failed.append((clip, sname))
                break

    print("-" * 60)
    if failed:
        print("失败:", ", ".join(f"{c}/{s}" for c, s in failed))
        return 1
    print("全部完成。产物:", ctx.run_dir)
    return 0


def cmd_stage(args: argparse.Namespace) -> int:
    cfg = PipelineConfig.load(args.config)
    ctx = build_ctx(args, cfg)
    stage = get_stage(args.stage)
    try:
        result = stage.run(ctx, args.clip, force=args.force)
    except PipelineError as exc:
        print(f"阶段 {args.stage} 失败: {exc}")
        if isinstance(exc.to_dict().get("cause"), str):
            print("原因:", exc.to_dict()["cause"])
        return 1
    print(json.dumps(
        {"stage": args.stage, "clip": args.clip, "reused": getattr(result, "reused", False),
         "outputs": [str(p) for p in getattr(result, "outputs", [])]},
        ensure_ascii=False, indent=2,
    ))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from .eval import run_evaluation

    cfg = PipelineConfig.load(args.config)
    run_dir = cfg.runs_dir / args.run_id
    if not run_dir.exists():
        print(f"运行目录不存在: {run_dir}")
        return 1
    clips = args.clips.split(",") if args.clips else None
    report = run_evaluation(run_dir, clips=clips, layers=args.layers)
    print(report.summary_text())
    print(f"\n报告已写入: {report.report_path}")
    if report.stability_note:
        print("\n" + report.stability_note)
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """扫描 PV, 推荐切片方案 (素材就绪后的第一步)."""
    from .analyze import render_scan, scan

    cfg = PipelineConfig.load(args.config)
    if args.video:
        video = Path(args.video)
    else:
        # 默认取 assets/source/ 下的第一个视频
        src_dir = cfg.assets_dir / "source"
        cands = sorted(
            p for p in src_dir.glob("*")
            if p.suffix.lower() in {".mp4", ".mov", ".avi", ".mkv", ".flv"}
        ) if src_dir.is_dir() else []
        if not cands:
            print(f"未找到 PV: 请指定 --video, 或把 PV 放到 {src_dir}/")
            return 1
        video = cands[0]
        if len(cands) > 1:
            print(f"发现 {len(cands)} 个视频, 使用第一个: {video.name}")
            print(f"(如需指定: --video <path>)")

    if not video.exists():
        print(f"文件不存在: {video}")
        return 1

    print(f"扫描中... (镜头检测 + 逐镜头测量, 约 1-2 秒/镜头)")
    result = scan(video, threshold=args.threshold)
    print(render_scan(result, top=args.top))

    if args.out:
        import json

        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n详细数据已写入: {out}")
    return 0


# --------------------------------------------------------------------- 解析器


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="anime_pv",
        description="动画 PV 人物替换 AI 管线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", default="default", help="configs/<name>.yaml")
    p.add_argument("--run-id", default=None, help="运行 ID (默认按时间生成)")

    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="环境体检")
    d.set_defaults(func=cmd_doctor)

    s = sub.add_parser("smoke", help="离线自检 (合成素材, 不需密钥)")
    s.add_argument("--mode", default="wan-std")
    s.add_argument("--jitter", type=float, default=0.0)
    s.set_defaults(func=cmd_smoke)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--clip", default="A", help="片段 ID")
    common.add_argument("--clips", default=None, help="批量: A,B,C")
    common.add_argument("--adapter", default="mock", help="mock | dashscope")
    common.add_argument("--mode", default="wan-std", help="wan-std | wan-pro")
    common.add_argument("--stage", default=None, help="只跑指定阶段")
    common.add_argument("--until", default=None, help="跑到指定阶段为止")
    common.add_argument("--force", action="store_true", help="忽略缓存重跑")
    common.add_argument("--tag", default=None, help="run_id 后缀标签")
    common.add_argument("--jitter", type=float, default=0.0)

    r = sub.add_parser("run", parents=[common], help="跑片段全流程")
    r.set_defaults(func=cmd_run)

    st = sub.add_parser("stage", parents=[common], help="只跑单个阶段")
    st.set_defaults(func=cmd_stage, clips=None)

    e = sub.add_parser("eval", help="生成评估报告")
    e.add_argument("--clip", default=None)
    e.add_argument("--clips", default=None)
    e.add_argument("--layers", default="L1,L2,L3", help="要跑的评估层, 如 L1,L3")
    e.set_defaults(func=cmd_eval)

    sc = sub.add_parser("scan", help="扫描 PV 并推荐切片方案 (素材就绪后第一步)")
    sc.add_argument("--video", default=None, help="PV 路径 (默认取 assets/source/ 第一个)")
    sc.add_argument("--threshold", type=float, default=0.42, help="镜头切换灵敏度")
    sc.add_argument("--top", type=int, default=12, help="显示前 N 个镜头")
    sc.add_argument("--out", default=None, help="把完整 JSON 写入该路径")
    sc.set_defaults(func=cmd_scan)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except PipelineError as exc:
        print(f"\n[管线错误] {exc}")
        d = exc.to_dict()
        for k in ("stage", "clip_id", "request_id", "cause"):
            if d.get(k):
                print(f"  {k}: {d[k]}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
