"""统一命令行入口.

用法示例:
    # 离线自检 (不需要密钥/素材, 秒级完成)
    python -m anime_pv.cli smoke

    # 环境体检
    python -m anime_pv.cli doctor

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
    from .utils.video import ffmpeg_available
    if ffmpeg_available():
        print("[OK ] ffmpeg / ffprobe")
    else:
        print("[-- ] ffmpeg 缺失  -> winget install Gyan.FFmpeg")
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
    n_src = len([p for p in src.glob("*") if p.suffix.lower() in {".mp4", ".mkv", ".mov", ".avi"}])
    n_char = len([p for p in char.glob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}])
    print(f"[{'OK ' if n_src else '-- '}] 原始 PV 素材: {n_src} 个 (放 assets/source/)")
    print(f"[{'OK ' if n_char else '-- '}] 人设图素材: {n_char} 个 (放 assets/character/)")

    print("-" * 60)
    print("结论:", "环境就绪" if ok else "存在缺项, 见上方 -- 行")
    print("提示: 即使存在缺项, `smoke` 与 `--adapter mock` 仍可运行。")
    return 0 if ok else 1


def cmd_smoke(args: argparse.Namespace) -> int:
    """离线自检: 用合成素材走通全链路, 验证框架完整性 (不需要密钥/真实素材)."""
    import numpy as np
    try:
        import cv2
    except ImportError:
        print("smoke 需要 opencv-python: pip install opencv-python")
        return 1

    cfg = PipelineConfig.load(args.config)
    ctx = RunContext(
        run_id=args.run_id or make_run_id("smoke"),
        cfg=cfg,
        adapter_name="mock",
        mode=args.mode,
        adapter=build_adapter("mock", jitter=0.0),
    )
    ctx.init_run({"purpose": "smoke test with synthetic assets"})

    tmp = ctx.run_dir / "_synthetic"
    tmp.mkdir(parents=True, exist_ok=True)

    print(f"run_id: {ctx.run_id}")
    print("生成合成视频 (6s, 640x360, 24fps)...")
    vid = tmp / "synth.mp4"
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

    from .adapters import TransferResult  # noqa: F401  仅确保导入可用
    from .stages import get_stage

    failures: list[str] = []
    for stage_name in PIPELINE:
        stage = get_stage(stage_name)
        try:
            print(f"  -> {stage_name} ...", end=" ", flush=True)
            stage.run(ctx, "A", force=True)
            print("OK")
        except PipelineError as exc:
            if stage_name in {"ingest", "assemble", "eval"}:
                print(f"FAIL: {exc}")
                failures.append(stage_name)
            else:
                # 依赖 ffmpeg 的阶段若缺依赖, 已由 doctor 报告, 不算框架缺陷
                print(f"skip ({exc})")

    print("-" * 60)
    if failures:
        print(f"框架自检未通过, 失败阶段: {failures}")
        return 1
    print("框架自检通过。产物目录:", ctx.run_dir)
    return 0


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
