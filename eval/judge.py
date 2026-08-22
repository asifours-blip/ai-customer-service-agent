"""LLM Judge（仅 live）：deepseek-v4-pro，rubric 0/1/2。

- Judge 不得作为唯一真值；与 Deterministic/Human 指标严格分列
- 离线模式不运行 Judge（报告中标注 LIVE_ONLY）
"""

from __future__ import annotations

from typing import Any, Protocol

from app.llm.client import LLMClient

JUDGE_SYSTEM = (
    "你是严格的客服回答质量评审。给定用户问题、系统回答和参考要点，"
    '只输出 JSON：{"score": 0|1|2, "reason": "简短理由"}。'
    "2=正确且完整覆盖参考要点；1=部分正确或遗漏关键点；0=错误/答非所问/编造。"
)

RUBRIC = {"0": "错误/答非所问/编造", "1": "部分正确或遗漏关键点", "2": "正确且完整"}


class JudgeClient(Protocol):
    def score(self, question: str, answer: str, reference: str) -> dict[str, Any]: ...


class LLMJudge:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def score(self, question: str, answer: str, reference: str) -> dict[str, Any]:
        import json

        user = f"用户问题：{question}\n系统回答：{answer}\n参考要点：{reference}\n请评分。"
        resp = self.llm.complete(JUDGE_SYSTEM, user, max_tokens=300)
        try:
            data = json.loads(resp.content.strip().removeprefix("```json").removesuffix("```").strip())
            score = int(data["score"])
            assert score in (0, 1, 2)
            return {"score": score, "reason": str(data.get("reason", ""))}
        except Exception:  # noqa: BLE001
            return {"score": -1, "reason": "judge 输出解析失败", "raw": resp.content[:200]}
