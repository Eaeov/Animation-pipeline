# Pipeline 设计文档

> 版本：v1.0　|　日期：2026-09-23
> 配套：`ARCHITECTURE.md`（代码契约）、`AGENTS.md`（协作入口）

---

## 0. 设计目标与取舍

题面把优先级写得很清楚：**可复用 + 稳定 > 峰值质量**。
所以本 pipeline 的所有设计决策都服从一条准则：

> **消除片段间的自由度差异，让 A/B/C 走过结构上完全相同的路径。**

具体化为四条设计原则：

| 原则 | 实现手段 |
|---|---|
| **结构同构** | A/B/C 的 `runs/` 目录结构、经过的 stage 序列完全一致 |
| **参数外置** | 所有可调参数在 `configs/`，代码里零硬编码 |
| **无分支** | stage 内部不得出现 `if clip_id == ...` 的特判 |
| **可验证** | 每步落 manifest（含 input_hash），可 diff 证明 A/B 同配置 |

---

## 1. 总体架构

```
                    ┌─────────────────────────────────────────┐
                    │           configs/  (参数唯一来源)         │
                    │  default.yaml (模型/轮询/限制)             │
                    │  clips.yaml   (片段切分方案)               │
                    └───────────────┬─────────────────────────┘
                                    │ 只读
                    ┌───────────────▼─────────────────────────┐
   原始 PV ────────▶│  [1] ingest      切片 + 归一化 + 约束预检   │
                    └───────────────┬─────────────────────────┘
                                    │ clip.mp4 (24fps, ≤1280px, 无声)
   人设图 ────────▶┌───────────────▼─────────────────────────┐
                    │  [2] character  尺寸/宽高比/大小 规整      │
                    └───────────────┬─────────────────────────┘
                                    │ character.jpg
                    ┌───────────────▼─────────────────────────┐
                    │  [3] style_align  按片段画幅补边对齐 ★     │
                    └───────────────┬─────────────────────────┘
                                    │ character_aligned.png
                    ┌───────────────▼─────────────────────────┐
                    │  [4] transfer   wan2.2-animate-mix       │
                    │      submit → poll(15s) → download       │
                    └───────────────┬─────────────────────────┘
                                    │ transferred.mp4 (无声)
                    ┌───────────────▼─────────────────────────┐
                    │  [5] assemble  音频回接 + 对照视频         │
                    └───────────────┬─────────────────────────┘
                                    │ final.mp4
                    ┌───────────────▼─────────────────────────┐
                    │  [eval]  L1 客观 / L2 VLM / L3 跨片段方差  │
                    └─────────────────────────────────────────┘

      ★ = 本题设计取舍的核心：用显式阶段保证 A/B 条件公平
```

**数据流向**：单向（forward-only）。**阶段间通信**：只通过 `RunContext` + 文件系统，
不通过内存传递大对象 —— 这保证每个阶段都能**独立运行**（NFR）。

---

## 2. 阶段详解

### [1] `ingest` — 切片与归一化

**输入**：`assets/source/pv.mp4` + `configs/clips.yaml` 的切分方案
**输出**：`runs/<run>/<clip>/01_ingest/clip.mp4` + `frames/`

**做的事**：

1. **重编码切片**（`-ss` / `-t` + libx264）
   > 为什么不用 `-c copy`：流拷贝会吸附到最近关键帧，导致实际时长偏差，
   > 可能跌破平台的 2s 下限而被拒。宁可慢一点也要精确。

2. **归一化规格**：最长边 ≤1280、帧率 24fps、yuv420p、去音轨
   > 去音轨是必要的：平台不处理音频，且减小体积。
   > 音频在 `assemble` 阶段从源视频重新取回。

3. **约束预检**：时长 / 分辨率 / 宽高比 / 大小 / 格式
   > **本地预检而不是靠 API 报错** —— 省一次往返、省一次可能的计费、
   > 且错误信息更明确（能同时报出所有违规项，而非第一个）。

4. **抽帧**：每 0.5s 一帧，供评估与人眼检视

**关键实现**：`input_hash = sha256(切分参数 + 源视频元信息)` → 幂等基础。

---

### [2] `character` — 人设图规整

**输入**：`assets/character/<clip_id>.png`（或通用 `char.png`）
**输出**：`02_character/character.jpg`

**做的事**（按顺序）：

1. 转 RGB（去 alpha / 调色板模式 —— 平台不收 PNG 索引色）
2. 超长边等比缩到 ≤2048；过小则放大到 ≥256
3. **宽高比越界时用补边（pad）而非裁剪**
   > 为什么 pad 不 crop：裁剪会切掉人物的头发/手臂，破坏身份特征。
   > 补白边虽然丑，但保留了完整人物，且 `style_align` 阶段会再处理。
4. 质量递减压缩（95→60）至 ≤5MB

**命名约定**（支持 A/B 各用不同人设）：
```
assets/character/A.png      ← 推荐，片段 A 专用
assets/character/char.png   ← 通用人设（所有片段共用）
```

> 设计说明：**允许 A/B 用不同人设，但必须用同一套处理逻辑。**
> 人设差异是"输入差异"，不是"pipeline 差异"，不违反可复用性。

---

### [3] `style_align` — 画幅对齐 ★核心设计

**输入**：`02_character/character.jpg` + `01_ingest/clip.mp4`
**输出**：`03_style_align/character_aligned.png`

**做的事**：按参考视频的宽高比，把人设图等比缩放后**居中补边**到同比例画幅。

**为什么单独设一个阶段（本项目的设计取舍核心）：**

官方 FAQ 明确列出：
> *"确保输入图片与参考视频中人物画幅占比相似"*
> *"画幅不匹配是最常见的弱结果原因"*

这意味着，**如果不做对齐**：
- 片段 A 的人设画幅可能恰好接近 → 高分
- 片段 B 的人设画幅不匹配 → 崩掉
- 结果：**A 90 分 / B 40 分 → 极差 50 分 → 完全违反题面 P1**

如果把对齐"顺便"写在 `character` 阶段里，会有两个问题：
1. 它需要读 `clip.mp4`（跨阶段依赖），破坏阶段独立性
2. 它变成一个隐式副作用，出问题时难以定位

**所以把它提升为独立阶段**：
- 显式出现在 pipeline 图上，评审一眼能看到"他们考虑了公平性"
- 对 A/B/C **一视同仁**地执行
- 可单独运行、单独验证（`cli stage --stage style_align`）
- manifest 里记录了 `clip_canvas`，可审计

> 一句话：**把"公平性"从"注意事项"变成"架构组件"。**

---

### [4] `transfer` — 换人生成

**输入**：`03_style_align/character_aligned.png` + `01_ingest/clip.mp4`
**输出**：`04_transfer/transferred.mp4` + `raw_response.json`

**调用流程**（两步异步）：

```
POST /api/v1/services/aigc/image2video/video-synthesis
  Header: X-DashScope-Async: enable
  Body: {model: wan2.2-animate-mix,
         input: {image_url, video_url, watermark: false},
         parameters: {mode: wan-std, check_image: true}}
  → {task_id}

GET /api/v1/tasks/{task_id}   (每 15s 轮询, 超时 900s)
  → PENDING → RUNNING → SUCCEEDED {results.video_url}
  → 立即下载（URL 24h 失效）
```

**工程处理**：

| 问题 | 处理 |
|---|---|
| 素材需公网 URL | `Publisher` 抽象（data-URI 兜底 / OSS 扩展） |
| 4xx 不重试 | 鉴权/参数错误重试无意义 |
| 5xx 重试 | 指数退避，最多 3 次 |
| 失败不扣费 | 鼓励重试 |
| 重复扣费 | `input_hash` 命中缓存则跳过（用 `--force` 强制） |
| 计费统计 | 记录 `billable_seconds`，按模式算单价 |
| 原始响应 | 存 `raw_response.json`，失败可溯源 |

**为什么 `mode` 是参数而非写死**：
std 用于迭代验证（省钱），pro 用于终稿。**同一份代码，两个模式** —— 又一处"参数化而非分支"。

---

### [5] `assemble` — 回拼与交付

**输入**：`04_transfer/transferred.mp4` + 源视频原片段（带音轨）
**输出**：`05_assemble/final.mp4` + `compare.mp4`

**做的事**：

1. **音频回接**：`-map 0:v -map 1:a -shortest`
   > 换人模型输出**无声**。不接回音轨，PV 就失去节奏感，
   > 评估"是否可用"时音画同步是隐含要求。音轨取自源视频对应时间段。

2. **对照视频**：原片与生成片纵向堆叠
   > 这是**最直观的评估证据** —— 评审一眼就能看出换人效果，
   > 比任何指标数字都有说服力。

**设计取舍**：对照视频生成失败**不阻断**主流程（静默降级），
因为它是证据辅助物，不是交付必需物。

---

## 3. 数据契约

### 3.1 目录结构（A/B/C 完全同构）

```
runs/20260923-1400_baseline/
├── run_manifest.json            # 全局: 配置快照 / git commit / adapter / mode
├── A/
│   ├── 01_ingest/   manifest.json, clip.mp4, frames/
│   ├── 02_character/manifest.json, character.jpg
│   ├── 03_style_align/manifest.json, character_aligned.png
│   ├── 04_transfer/ manifest.json, transferred.mp4, raw_response.json
│   ├── 05_assemble/ manifest.json, final.mp4, compare.mp4, clip_with_audio.mp4
│   └── ...
├── B/   ← 与 A 完全同构
├── C/   ← 与 A 完全同构
└── _eval/
    ├── report.json              # 机器可读
    ├── report.md                # 人可读
    ├── A/ report.json, vlm_request_A.json, vlm_response_A.json, evidence/
    └── B/ ...
```

**"完全同构"是刻意的**：评审可以直接 `diff -r runs/X/A runs/X/B` 验证
A/B 走过同一条路径 —— 这是可复用性的**物理证据**，比任何文字声明都有力。

### 3.2 manifest 最小字段

```json
{
  "stage": "transfer", "clip_id": "A", "run_id": "20260923-1400_baseline",
  "input_hash": "sha256:...",                  // 幂等与复现的核心
  "params": {"adapter":"dashscope", "mode":"wan-std", "model":"wan2.2-animate-mix"},
  "started_at": "...", "finished_at": "...", "elapsed_s": 143.2,
  "status": "succeeded",
  "outputs": ["04_transfer/transferred.mp4"],
  "task_id": "0385dc79-...", "request_id": "a67f8716-...",   // 失败可溯源
  "billable_seconds": 5.2, "estimated_cny": 3.12,            // 成本核算
  "error": null
}
```

---

## 4. 可复用性保障机制（NFR-1 的实现）

| 机制 | 实现 | 如何验证 |
|---|---|---|
| 无分支 | stage 代码不含 clip 特判 | `grep -rn "clip_id ==" src/stages/` 无结果 |
| 参数外置 | 切分/模式/分辨率全在 yaml | 评审可改 yaml 复跑 |
| 结构同构 | A/B/C 目录完全一致 | `diff -r runs/X/A runs/X/B` |
| 配置单一 | 一次 run 只读一份 config | `run_manifest.json` 只有一个 config 块 |
| 显式对齐 | `style_align` 阶段 | pipeline 图上可见 |

> **可复用性不是靠自律，是靠架构强制的。**
> 由于 `animate-mix` 没有 prompt 参数，甚至**在物理上不存在**
> "为片段 A 特调 prompt"这种可能。

---

## 5. 稳定性保障机制（NFR-2 的实现）

| 机制 | 作用 |
|---|---|
| 输入归一化 | 消除"因输入规格不同导致的质量差异" |
| 画幅对齐 | 消除"人设画幅与片段不匹配"这一最大变量 |
| 固定无 prompt | 消除"prompt 敏感性"导致的片段依赖 |
| 固定 seed（VLM） | 评估分数可复现 |
| L3 方差指标 | **把稳定性变成可测量的数字** |

---

## 6. 扩展点

| 扩展 | 位置 | 说明 |
|---|---|---|
| 新模型 | `adapters/` + 注册表 | 不改 stages |
| 新指标 | `eval/` | 实现 Evaluator 协议 |
| 超分修复 | `stages/06_upscale` | Real-ESRGAN |
| OSS 上传 | `adapters/cdn.py` | 替换 DataUriPublisher |
| 人物检测裁剪 | `style_align` 增强 | 自动匹配人物占比（比 pad 更精确） |
| 批量并行 | `tools/batch_runner.py` | 注意百炼并发限流 |

---

## 7. 已知局限（诚实声明）

写到交付文档里，不隐藏：

1. **不做人物检测**：`style_align` 只对齐画幅，不检测人物在画面中的占比。
   更精确的做法是先检测人物框再缩放对齐 —— 时间预算外，列为扩展点。
2. **单角色限制**：平台模型针对单一主角替换，多人场景会失败（列入失败案例）。
3. **音频不重生成**：只回接原音轨。若人设角色与原角色性别/声线差异大，
   会有"音画不符感" —— 题面未要求声音替换，明确不做。
4. **无超分**：输出固定 720P，不做后处理提升。
5. **VLM 评分有波动**：已用 temperature=0 + seed 固定，但同一输入仍可能小幅波动，
   因此用 L1 客观指标交叉验证。
6. **缓存基于 input_hash**：若平台模型版本静默更新，hash 不变但输出会变。
   manifest 里记录 `model.version="unknown"` 就是为了标记这个不确定性。
