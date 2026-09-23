"""anime_pv — 动画 PV 人物替换 AI 管线.

五阶段流水线: ingest -> character -> style_align -> transfer -> assemble
所有外部模型调用通过 adapters 层抽象, 详见 AGENTS.md / ARCHITECTURE.md.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
