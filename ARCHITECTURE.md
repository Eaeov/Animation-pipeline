# ARCHITECTURE.md — 架构与目录契约

> 配套 `AGENTS.md` 使用。本文件定义**代码往哪放、层与层怎么通信**。改代码前必读。

---

## 1. 分层图

```
                        ┌──────────────┐
                        │   cli.py     │  唯一命令行入口
                        └──────┬───────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
   ┌────▼─────┐          ┌─────▼──────┐        ┌──────▼─────┐
   │ stages/  │          │  eval/     │        │  utils/    │
   │ 业务编排  │          │  评估器     │        │  无状态工具 │
   └────┬─────┘          └─────┬──────┘        └────────────┘
        │                      │
        │   ┌──────────────────┘
        │   │
   ┌────▼───▼─────────────────────┐
   │        adapters/             │  所有外部模型调用的唯一出口
   │  ┌────────┬────────┬──────┐  │
   │  │DashScope│ Mock  │ ...  │  │
   │  └────────┴────────┴──────┘  │
   └──────────────────────────────┘
```

**依赖方向严格单向**：`cli → stages → adapters`，`cli → eval → adapters`。
`stages/` **禁止** import `eval/`；`adapters/` **禁止** import `stages/`。
`utils/` 谁都能用，但它自己不 import 任何上层。

---

## 2. 目录职责表

| 路径 | 职责 | 允许新增 | 禁止 |
|---|---|---|---|
| `src/anime_pv/cli.py` | 子命令解析、run_id 管理、调度 | 新增子命令 | 写业务逻辑 |
| `src/anime_pv/stages/` | 五阶段编排逻辑 | 新增 stage（需在 `PIPELINE` 注册） | 直接调 HTTP/百炼 SDK |
| `src/anime_pv/adapters/` | 外部模型适配、重试、轮询、计费统计 | 新增模型适配器 | 碰文件系统业务路径 |
| `src/anime_pv/eval/` | 指标计算、报告生成 | 新增 Evaluator | 修改素材 |
| `src/anime_pv/utils/` | 视频/图片/IO/日志 | 新增纯函数工具 | 持有全局可变状态 |
| `configs/` | 全部可调参数 | 新增实验配置 | 存密钥 |
| `assets/source/` | 原始 PV 及切片 | — | 提交大文件（用 Git LFS 或外链） |
| `assets/character/` | 人设图 | — | — |
| `runs/<run_id>/` | 运行产物（gitignore） | — | 提交到 git |
| `tools/` | 环境检查、辅助脚本 | — | — |
| `docs/` | 设计文档 | — | — |

---

## 3. 核心接口契约

### 3.1 Stage 协议

```python
class Stage(Protocol):
    name: str                      # 阶段名，等于 runs/ 下的子目录名
    def run(self, ctx: RunContext, clip_id: str, force: bool = False) -> StageResult: ...
```

- 输入输出**只通过 `RunContext` 和文件系统**，不通过 return 传大数据。
- 必须幂等：若 `manifest.json` 已存在且输入 hash 未变 → 直接返回缓存结果（`force=True` 才重跑）。
- 抛错统一 `PipelineError(stage, clip_id, request_id, cause)`。

### 3.2 Adapter 协议

```python
class VideoTransferAdapter(Protocol):
    name: str
    def submit(self, image_path: Path, video_path: Path, *, mode: str, **kw) -> str:  # 返回 task_id
        ...
    def poll(self, task_id: str, *, timeout: int, interval: int) -> TaskResult: ...
    def transfer(self, image_path: Path, video_path: Path, *, mode: str, **kw) -> TransferResult: ...
```

`transfer()` 是模板方法，默认 = `submit` + `poll` + 下载。子类通常只需实现 `submit`/`poll`。

**新增模型只需：**
1. 在 `adapters/` 加一个文件，实现上述协议；
2. 在 `adapters/__init__.py` 的 `ADAPTERS` 注册表里登记名字。
**不要动 `stages/`。**

### 3.3 Evaluator 协议

```python
class Evaluator(Protocol):
    dim: str        # 维度名，如 "temporal_stability"
    layer: str      # L1 | L2 | L3
    def score(self, ref: ClipArtifact, out: ClipArtifact) -> Metric: ...
```

`Metric = {dim, layer, value: float(0-100), detail: dict, evidence: list[Path]}`

---

## 4. 产物目录布局

```
runs/<run_id>/
├── run_manifest.json          # 本次运行的全局信息（配置快照、时间、git commit）
├── A/                          # clip_id
│   ├── 01_ingest/manifest.json + clip.mp4 + frames/
│   ├── 02_character/manifest.json
│   ├── 03_style_align/manifest.json + aligned.png
│   ├── 04_transfer/manifest.json + out.mp4 + raw_response.json
│   ├── 05_assemble/manifest.json + final.mp4
│   └── eval/report.json + report.md + evidence/
└── B/
    └── ...（与 A 结构完全一致）
```

**A 和 B 目录结构必须完全同构** —— 这是"同一 pipeline 跑 A/B"的物理证据。

---

## 5. manifest.json 最小字段

```json
{
  "stage": "transfer",
  "clip_id": "A",
  "run_id": "20260923-1400_baseline",
  "input_hash": "sha256:...",
  "params": {"mode": "wan-std", "adapter": "dashscope"},
  "model": {"name": "wan2.2-animate-mix", "version": "unknown"},
  "started_at": "...", "finished_at": "...", "elapsed_s": 143.2,
  "status": "succeeded",
  "outputs": ["out.mp4"],
  "cost": {"billable_seconds": 5.0, "estimated_cny": 3.0},
  "error": null
}
```

> `input_hash` = 所有输入文件内容 hash + params hash。用于幂等判断与实验可复现。

---

## 6. 扩展点（后续可加，但不要现在做）

- `adapters/KlingAdapter`、`adapters/WanI2VAdapter`（对照实验）
- `eval/IdentityFaceEvaluator`（InsightFace 人脸相似度）
- `stages/06_upscale`（Topaz / Real-ESRGAN 提升终稿画质）
- `tools/batch_runner.py`（多片段并行，注意百炼并发限流）

---

## 7. 反模式清单（Code Review 时重点看）

| 反模式 | 为什么错 | 正确做法 |
|---|---|---|
| stage 里硬编码 prompt | 破坏可复用性（P1） | 进 `configs/` |
| 为片段 A 加 `if clip_id == "A"` 分支 | **直接违反 P1，本题自杀** | 参数化，A/B 走同一路径 |
| adapter 里写业务目录拼接 | 越层 | 返回结果，由 stage 落盘 |
| eval 里改素材 | 副作用 | 只读，产出 evidence 副本 |
| 大视频提交进 git | 仓库爆炸 | gitignore + 外链/LFS |
| 跳过 manifest 直接下载 | 无法复现、无法幂等计费 | 一律走 run 流程 |
