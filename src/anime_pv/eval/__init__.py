"""评估体系.

三层设计 (对应 docs/03-评估方案.md):
  L1 客观技术质量  —— 可复现、无主观性, 用于回归与自检
  L2 语义质量      —— 对齐"换人"任务本质 (身份/动作/场景/伪影)
  L3 稳定性        —— 跨片段方差, 直接回答题目 "A/B 同 pipeline 都到 75 分"

设计原则:
  1. **可复现**: 同一输入必须产出同一分数 (L1 完全确定; L2 固定 seed + 固定 prompt)
  2. **可解释**: 每个分数带 detail 与 evidence (对比图/抽帧), 不做黑箱
  3. **可自检**: MockAdapter 的 jitter 扰动能被 L1 检出, 否则说明评估器失效
  4. **诚实**: 无法测量的维度标记为 not_measured, 不用估算值填充
"""

from .report import EvalReport, run_evaluation
from .l1_technical import L1Evaluators
from .l2_semantic import L2Evaluators
from .l3_stability import StabilityEvaluator

__all__ = [
    "EvalReport",
    "run_evaluation",
    "L1Evaluators",
    "L2Evaluators",
    "StabilityEvaluator",
]
