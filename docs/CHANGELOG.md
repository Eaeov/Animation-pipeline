# CHANGELOG

> 格式：`[模块] 做了什么 — 为什么`
> 约定：每次改动后追加，不覆盖历史（见 AGENTS.md §7）

---

## v0.1.0 — 2026-09-23（框架搭建）

### 框架 / 文档
- `[docs]` 建立 vibecoding 框架三件套：`AGENTS.md`（AI 协作入口）、
  `ARCHITECTURE.md`（架构与代码契约）、`TASK.md`（题面原文与硬约束）
  — 目的：让任何 AI/人接手后 5 分钟内理解任务边界，避免偏离题目优先级
- `[docs]` 编写 `docs/00-需求分析.md` — 把题面三句隐藏的考察点显式化
  （可复用 > 峰值质量），并据此导出设计与验收标准
- `[docs]` 编写 `docs/01-技术选型.md` — 6 种方案对比，
  论证为何"无 prompt 的换人模型"是本题优势而非劣势
- `[docs]` 编写 `docs/02-Pipeline设计.md` — 五阶段详解 + 数据契约
- `[docs]` 编写 `docs/03-评估方案.md` — 三层评估体系与量化口径
- `[docs]` 编写 `docs/04-交付说明.md` — 结题报告（含 6 个题面问题）
- `[docs]` 编写 `docs/素材来源与授权.md` — 版权合规声明

### 核心代码
- `[config]` `PlatformLimits` 把百炼平台约束（2s<d≤30s、200-2048px、
  1:3-3:1、≤200MB/≤5MB）编码为可测试的常量 — 目的：本地预检替代 API 报错
- `[errors]` 建立 `PipelineError` 体系，异常携带 `stage`/`clip_id`/`request_id`
  — 目的：失败可诊断，能直接去百炼控制台溯源
- `[context]` `RunContext` + `StageTimer` 实现 manifest 落盘与 `input_hash` 幂等
  — 目的：不重复扣费 + 实验可复现
- `[adapters]` 定义 `VideoTransferAdapter` 协议 + 注册表
  — 目的：换模型只加文件，不改 stages（NFR-5）
- `[adapters]` `DashScopeAnimateMixAdapter` 实现 wan2.2-animate-mix 调用：
  两步异步、4xx 不重试 / 5xx 指数退避、结果即时下载
- `[adapters]` `MockAdapter` 提供可控扰动（jitter/blur/flicker）
  — 目的：离线跑通 + **评估器自检**（这是评估可信的关键）
- `[stages]` `ingest`：重编码切片（非流拷贝，保证时长精确）+ 归一化 + 约束预检
  — 为什么重编码：`-c copy` 会吸附关键帧导致时长偏差，可能跌破 2s 下限
- `[stages]` `character`：人设图规整，宽高比越界时**补边而非裁剪**
  — 为什么 pad 不 crop：裁剪会切掉头发/手臂，破坏身份特征
- `[stages]` `style_align`：按片段画幅对齐 ★核心设计
  — 为什么独立成阶段：把"公平性"从注意事项变成架构组件（详见 docs/02 §2）
- `[stages]` `transfer`：调用适配器 + 记录计费秒数与原始响应
- `[stages]` `assemble`：音频回接（模型输出无声）+ 对照视频生成
- `[utils]` `video`：ffmpeg 封装（probe/cut/normalize/extract_frames/concat）
  — 缺 ffmpeg 时给出可执行的安装指引，而非晦涩的 FileNotFoundError
- `[utils]` `image`：人设图规整与画幅对齐

### 评估体系
- `[eval]` `l1_technical`：6 个客观指标（PSNR/SSIM/时序闪烁/光流平滑/
  锐度/色彩），全部**有参照**（对比原片），而非无参照打分
  — 关键：`temporal_stability` 用二阶差分，专抓"闪脸"这一头号失败模式
- `[eval]` `l2_semantic`：VLM 三元对比评分（人设图+原片帧+生成帧）
  — 为什么三元：单独看生成图无法判断"像不像"和"对不对"
  — 为什么必须加 L2：L1 有语义盲区（换得干净 vs 没换，PSNR 无法区分）
- `[eval]` `l3_stability`：跨片段方差 + 一致性指数 `CI = mean/(mean+2·std)` ★
  — 本题胜负手：把"可复用性"从口号变成可测量的数
- `[eval]` `report`：Markdown + JSON 双输出

### 测试
- `[test]` `test_pipeline_structure.py`（21 项）：阶段注册、平台约束边界、
  适配器注册、**无片段特判分支守卫**、L3 数学正确性
- `[test]` `test_eval_sanity.py`（3 项）：用 MockAdapter 制造已知缺陷，
  验证评估器能检出 — **这是"评估方案本身可信"的证据**

### 工具
- `[tools]` `setup_env.sh`：一键建 venv + 装依赖，默认阿里源
  — 为什么不用清华源：pip 26 与其索引格式不兼容，会报 No matching distribution
- `[tools]` `setup_ffmpeg.sh`：项目内安装 ffmpeg，多镜像轮询
  — 为什么自备：winget 在受限环境创建 symlink 会失败；GitHub 直连不可靠

### 修复的缺陷
- `[fix]` `config.py` 相对导入越界（`..errors` → `.errors`）
  — 顶层包文件不能使用两级相对导入，会导致 `ImportError: attempted relative
  import beyond top-level package`

---

## v0.1.1 — 2026-09-23（端到端验证 + 评估器自检修复）

> 触发原因：装好稳定版 ffmpeg 后跑首次**真实五阶段 + 评估**，暴露出一批
> "离线测试查不出、只有真跑才现形"的缺陷。

### 🔴 高危修复（评估可信性）

- `[fix]` **`cmd_smoke` 静默丢弃 `--jitter`** — 硬编码 `jitter=0.0`，
  导致"扰动自检"机制完全失效且不报错。`smoke --jitter 0.6` 与 `jitter=0`
  结果完全一致（都是 89.74）。
  — 危害：评估器的自检形同虚设，可能给出虚假的乐观结论。
  — 修复：透传 `args.jitter`，并在 jitter>0 时打印 `[扰动自检模式]`。
- `[fix]` **`FlowSmoothnessEvaluator` 归一化硬截断** —
  `100*(1-cv/0.8)` 在 CV>0.8 处直接归零，正常视频也拿 0 分，指标失去分辨率。
  — 修复：改指数饱和 `100*exp(-cv/tau)`，`tau=1.2`；画面近似静止时
  诚实标 `not_measured`（不用 0 冒充）。

### 🟡 可用性修复

- `[fix]` **ffmpeg 自动发现**：`ffmpeg_available()` 原只查 PATH，
  导致"按 `setup_ffmpeg.sh` 装进项目里了，`doctor` 却说没有"。
  — 修复：新增 `find_ffmpeg()`，查找顺序
  `ANIME_PV_FFMPEG_DIR` → 项目自带 `tools/ffmpeg/bin` → 系统 PATH。
- `[fix]` **消除 8 处硬编码 `"ffmpeg"`/`"ffprobe"`**：全部改走 `_ff()`，
  避免"探测得到但调用时 FileNotFoundError"的自相矛盾状态。
- `[fix]` `ClipPair` 的 `reference`/`generated` 改为可选默认 None —
  `build_pairs` 先构造再逐个 setattr，原签名会让 `eval` 直接 TypeError。
- `[fix]` `tools/setup_ffmpeg.sh` 版本 9.0.2 → **6.1.1**
  — 9.0.2 的 ffprobe 在本机段错误（详见 §8 失败案例 4）。
  另在安装末尾加 `ffprobe -version` 健康自检，把"装完就崩"提前暴露。

### 🟢 增强

- `[enhance]` `doctor` 显示 ffmpeg **版本 + 来源路径**，并单独自检 ffprobe
  — 便于一眼看出用的是哪个 ffmpeg（排查段错误类问题时关键）。
- `[test]` 测试 24 → **28 项**，新增：
  - `TestFlowSmoothnessNormalization`：归一化单调性 + 无硬截断（防 Bug 2 回归）
  - `TestCliParamsActuallyUsed`：断言 `--jitter` 真的透传到 adapter（防 Bug 1 回归）
- `[docs]` `04-交付说明.md` 新增**失败案例 5：评估器自检机制形同虚设**
  — 记录"功能看起来在工作实际没生效"这类最难查的 bug，含修复前后指标对照表。

### ✅ 本次验证结果

| 项目 | 结果 |
|---|---|
| `doctor` | **环境就绪**（ffmpeg 6.1.1 项目自带，自动发现） |
| `smoke`（jitter=0） | **五阶段全通**，综合 **89.74**，CI **1.0** |
| `smoke --jitter 0.6` | 综合 **28.01**，psnr 1.5 / temporal 0.0 / sharpness 0.0 |
| 区分度 | **61.7 分** — 评估器确认能检出缺陷 |
| `pytest` | **28 passed** |

---

## 待办（素材就绪后）

- `[ ]` 填写 `configs/clips.yaml` 的真实切分方案
- `[ ]` 放入 PV 与人设图素材
- `[ ]` 跑 std 模式生成 A/B/C
- `[ ]` 执行评估并写入实测报告
- `[ ]` 补充真实失败案例（含截图/视频证据）
- `[ ]` 发布 GitHub 公开仓库
