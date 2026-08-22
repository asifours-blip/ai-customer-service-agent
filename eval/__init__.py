"""Evaluation 包：数据集加载 → 运行 → 指标 → Judge/校准 → 成本 → 报告。"""

from eval.loader import load_dataset

__all__ = ["load_dataset"]
