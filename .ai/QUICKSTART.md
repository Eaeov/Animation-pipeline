# .ai/ — AI Agent 快速上手卡片

> 给未来的 AI Agent（或未来的自己）：**先读 `AGENTS.md`，再读本文件。**
> 本文件是"最短路径"——如果你只有 60 秒，读这里。

---

## 30 秒理解任务

**做什么**：取今年新番 PV → 切片 → 用日式人设图替换片中主角 → 输出成片 + 评估报告

**核心模型**：阿里云百炼 `wan2.2-animate-mix`（视频换人）
- 输入：人设图 URL + 参考视频 URL
- 约束：视频 `2s < d ≤ 30s`、200–2048px、≤200MB；图片 200–4096px、≤5MB
- 免费额度：50 秒（std + pro 共享）
- 计费：std 0.6 元/秒，pro 0.9 元/秒

**判分口径**（最重要，读三遍）：
> 对片段 A 和 B 用**同一套 pipeline** 都到 75 分
> **优于** 为 A/B 分别特调到 90 分

---

## 绝对不要做的事

| 禁止 | 原因 |
|---|---|
| ❌ 为某个片段加 `if clip_id == "A"` 分支 | 直接违反判分口径，会 fail 测试 |
| ❌ 硬编码 prompt / 分辨率 / 时长 | 破坏可复用性，应放 `configs/` |
| ❌ 把整支 PV 全量跑 | 题面点名"没有意义" |
| ❌ 评估时把未测到的指标当 0 分 | 污染统计，必须标 `not_measured` |
| ❌ 在 stages 里直接调 HTTP | 必须走 `adapters/` |
| ❌ 提交密钥或大视频 | 安全 + 仓库体积 |

---

## 常见改动怎么做

### 我要换一个生成模型

```bash
# 1. 新建 src/anime_pv/adapters/yourmodel.py
#    实现 VideoTransferAdapter 协议: submit() + poll() + download()
# 2. 在 adapters/__init__.py 的 ADAPTERS 注册
# 3. 完事 —— stages/ 一行都不用改
```

### 我要加一个评估指标

```bash
# 1. 新建 src/anime_pv/eval/your_metric.py
#    实现 Evaluator 协议: score(pair, out_dir) -> Metric
# 2. 在 l1_technical.L1Evaluators.all() 或 l2_semantic.L2Evaluators.all() 注册
# 3. 完事
```

### 我要加一个管线阶段

```bash
# 1. 新建 src/anime_pv/stages/your_stage.py，实现 Stage 协议
# 2. 在 stages/__init__.py 的 PIPELINE 和 _REGISTRY 登记（注意顺序）
# 3. 在 eval/report.py 的 STAGE_DIRS 加目录映射
```

### 我要调试某个阶段

```bash
# 只跑一个阶段，不跑全流程
python -m anime_pv.cli stage --clip A --stage transfer --adapter mock

# 强制重跑（忽略缓存）
python -m anime_pv.cli stage --clip A --stage transfer --force
```

---

## 关键文件速查

| 我想改... | 改这里 |
|---|---|
| 模型/接口地址/轮询参数 | `configs/default.yaml` |
| 片段切分方案 | `configs/clips.yaml` |
| 平台约束常量 | `src/anime_pv/config.py::PlatformLimits` |
| 生成模型调用逻辑 | `src/anime_pv/adapters/dashscope.py` |
| 阶段编排 | `src/anime_pv/stages/<stage>.py` |
| 评估指标 | `src/anime_pv/eval/l1_technical.py` / `l2_semantic.py` |
| 稳定性公式 | `src/anime_pv/eval/l3_stability.py::StabilityEvaluator` |
| 报告格式 | `src/anime_pv/eval/report.py::render_markdown` |

---

## 验证清单（改完代码后必须跑）

```bash
# 1. 语法
python -m compileall -q src/

# 2. 导入
PYTHONPATH=src python -c "from anime_pv.stages import PIPELINE; print(PIPELINE)"

# 3. 架构守卫 + 单元测试（必须全绿）
PYTHONPATH=src pytest tests/ -q

# 4. 离线跑通（不需要密钥）
PYTHONPATH=src python -m anime_pv.cli smoke
```

**特别注意**：`tests/test_pipeline_structure.py::TestNoClipSpecificBranch`
会扫描 `stages/` 里的片段特判分支。**如果你加了分支，测试会失败 —— 这是故意的。**

---

## 架构约束速记

```
依赖方向（严格单向）：
  cli → stages → adapters
  cli → eval   → adapters
  utils ← 谁都能用，但它不 import 上层

禁止：
  stages  import eval        （反向依赖）
  adapters import stages     （越层）
  stages  出现 clip_id == "A"（违反可复用性）
```

---

## 遇到问题怎么办

| 现象 | 排查 |
|---|---|
| `未检测到 ffmpeg` | `bash tools/setup_ffmpeg.sh` 或 `winget install Gyan.FFmpeg` |
| `DASHSCOPE_API_KEY 未设置` | `export DASHSCOPE_API_KEY=sk-xxxx`（mock 不需要） |
| `pip: No matching distribution found` | **换源**：`-i https://mirrors.aliyun.com/pypi/simple/` |
| `时长不在 (2, 30] 区间` | 改 `configs/clips.yaml` 的 `duration` |
| 换人效果差（脸不像） | 检查输入图与视频的人物画幅占比是否接近（官方首要建议） |
| VLM 评分 N/A | 未配置密钥或调用失败；L1+L3 仍有效 |

先跑 `python -m anime_pv.cli doctor` —— 它会一次性告诉你缺什么。
