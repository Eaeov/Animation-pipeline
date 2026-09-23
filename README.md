# anime-pv-pipeline

> **动画 PV 人物替换 AI 管线** — 取新番宣传 PV，切片后用日式人设图替换片中主角，
> 产出一套**可复用、稳定、可量化评估**的管线。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)]()
[![Model](https://img.shields.io/badge/model-wan2.2--animate--mix-orange)]()
[![License](https://img.shields.io/badge/license-MIT--noncommercial-lightgrey)]()

---

## 这是什么

一个针对「视频人物替换」任务设计的五阶段管线 + 三层评估体系。

**核心命题**：题目要求 *"对片段 A 和片段 B 用同样的 pipeline 都跑出 75 分，
优于对 A/B 特调出 90 分"* —— 即 **可复用性 > 峰值质量**。

因此本项目的设计重心不在"把某一帧做到极致"，而在：

- 用**架构强制**消除片段间的自由度差异（无 prompt、显式对齐、无分支）
- 用**跨片段方差**把"稳定性"变成一个可测量的数字

---

## 快速开始

### 1. 环境准备

```bash
# 安装依赖
pip install -r requirements.txt

# 安装 ffmpeg (Windows)
winget install Gyan.FFmpeg
# 或使用本项目自带的 (下载后解压到 tools/ffmpeg/)
# bash tools/setup_ffmpeg.sh

# 配置密钥 (仅 dashscope 适配器需要)
export DASHSCOPE_API_KEY=sk-xxxx        # Linux/macOS
set DASHSCOPE_API_KEY=sk-xxxx           # Windows CMD
$env:DASHSCOPE_API_KEY="sk-xxxx"        # PowerShell
```

### 2. 环境体检

```bash
python -m anime_pv.cli doctor
```

逐项检查 Python / 依赖 / ffmpeg / 密钥 / 素材，并给出可执行的修复命令。

### 3. 离线自检（不需要密钥和素材，秒级完成）

```bash
python -m anime_pv.cli smoke
```

用合成素材走通全链路，验证框架完整性。

### 4. 准备素材

```
assets/
├── source/pv.mp4           # 原始 PV（自行下载）
└── character/
    ├── A.png               # 片段 A 的人设图
    └── B.png               # 片段 B 的人设图（也可共用一个 char.png）
```

编辑 `configs/clips.yaml` 填写切分方案（片段起止时间）。

### 5. 运行管线

```bash
# 用 mock 适配器离线跑通（不花钱）
python -m anime_pv.cli run --clips A,B --adapter mock

# 真实生成（先用 std 模式验证）
python -m anime_pv.cli run --clips A,B --adapter dashscope --mode wan-std

# 只跑单个阶段（调试用）
python -m anime_pv.cli stage --clip A --stage transfer
```

### 6. 生成评估报告

```bash
python -m anime_pv.cli eval --run-id <run_id>
```

输出 `runs/<run_id>/_eval/report.md` 与 `report.json`。

---

## 管线结构

```
原始 PV ──▶ [1] ingest       切片 + 归一化 + 约束预检
人设图 ──▶ [2] character     尺寸/比例/大小 规整
        ──▶ [3] style_align  按片段画幅对齐 ★
        ──▶ [4] transfer     wan2.2-animate-mix 换人
        ──▶ [5] assemble     音频回接 + 对照视频
                ↓
        [eval] L1 客观 / L2 语义(VLM) / L3 跨片段稳定性
```

★ `style_align` 是本题设计取舍的核心：官方文档指出"画幅不匹配是最常见的弱结果原因"，
把对齐提升为独立阶段，是为了让 A/B/C **在架构上被一视同仁**。

---

## 三层评估体系

| 层 | 指标 | 手段 | 回答什么问题 |
|---|---|---|---|
| **L1** 客观技术 | PSNR / SSIM / 时序闪烁 / 光流平滑 / 锐度 / 色彩 | OpenCV + scikit-image | 技术合格吗？（确定性、可 CI） |
| **L2** 语义质量 | 身份一致性 / 动作保留 / 场景保留 / 伪影 | VLM (`qwen-vl-max`) | 人换成功了吗？ |
| **L3** 稳定性 | 跨片段标准差 / 一致性指数 / 片段极差 | 自研 | **pipeline 挑片段吗？** |

**L3 是核心**：一致性指数 `CI = mean / (mean + 2·std)` 同时惩罚低分与高方差，
精确对应题目"稳定 75 分 > 特调 90 分"的口径。

---

## 项目结构

```
anime-pv-pipeline/
├── AGENTS.md              # AI 协作入口（先读这个）
├── ARCHITECTURE.md        # 架构与代码契约
├── TASK.md                # 题面原文与硬约束
├── docs/                  # 设计文档
│   ├── 00-需求分析.md
│   ├── 01-技术选型.md
│   ├── 02-Pipeline设计.md
│   ├── 03-评估方案.md
│   └── 04-交付说明.md
├── src/anime_pv/
│   ├── cli.py             # 命令行入口
│   ├── config.py          # 配置（平台约束常量）
│   ├── context.py         # 运行上下文 + manifest 幂等
│   ├── errors.py          # 可诊断异常体系
│   ├── stages/            # 五阶段
│   ├── adapters/          # 模型适配器（换模型不改 stages）
│   ├── eval/              # 三层评估
│   └── utils/             # 视频/图片工具
├── configs/               # 参数唯一来源
├── tests/                 # 结构测试 + 评估器自检
└── runs/                  # 运行产物（不入 git）
```

---

## 设计原则

| 原则 | 落地 |
|---|---|
| 可复用 > 峰值 | 无 prompt 自由度、无片段特判、显式对齐阶段 |
| 评估要能自证 | `tests/test_eval_sanity.py` 用已知缺陷验证评估器有效性 |
| 诚实优先 | 未测到的指标标 `not_measured`，绝不用 0 分冒充 |
| 架构强制 | `tests/test_pipeline_structure.py` 自动拦截片段特判分支 |
| 不做过度抽象 | 5 个阶段不值得引入 DAG 调度器 |

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [需求分析](docs/00-需求分析.md) | 任务拆解、设计决策、时间规划、验收标准 |
| [技术选型](docs/01-技术选型.md) | 模型对比与选型依据、平台约束、技术栈 |
| [Pipeline 设计](docs/02-Pipeline设计.md) | 五阶段详解、数据契约、可复用性机制 |
| [评估方案](docs/03-评估方案.md) | 三层评估设计与量化口径 |
| [交付说明](docs/04-交付说明.md) | 交付清单 + 结题报告 + 失败案例 |

---

## 许可与声明

- 代码：MIT（仅限学习/研究用途）
- PV 与人设图素材版权归原权利人所有，本项目仅用于技术演示
- 详见 `docs/素材来源与授权.md`
