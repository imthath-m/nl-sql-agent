"""LLM-judge: did the prediction answer the right question?

Phase-4 eval component. A frontier model (config.JUDGE_MODEL, e.g. gemini-2.5-pro)
reads the question, the gold SQL, the predicted SQL and its plain-language answer, and
returns a JSON verdict. Per plan.md the judge is an *offline* check (frontier is
offline-only), but it uses the same google-genai client as the rest of the agent.

It complements EX and the rule-based taxonomy: EX is exact result-set match, the judge
catches semantically-right answers EX marks wrong (and semantically-wrong ones EX passes).

Contract:
  judge(question, pred_sql, gold_sql, explanation=None, evidence="", model=None)
    -> {"correct": bool, "score": float 0..1, "reason": str}
"""
import json
import re

from . import agent, config

PROMPT = """You are grading a text-to-SQL system. Decide whether the PREDICTED query
answers the user's QUESTION as well as the reference (gold) query does — judge intent,
not character-for-character equality. Different but equivalent SQL is correct.

Question: {question}
External knowledge: {evidence}

Gold SQL (reference, correct):
{gold_sql}

Predicted SQL:
{pred_sql}

Predicted plain-language answer: {explanation}

Reply with ONLY a JSON object, no markdown:
{{"correct": true|false, "score": <0.0-1.0>, "reason": "<one short sentence>"}}
- correct: true if the predicted query answers the same question as the gold.
- score: your confidence that it answers the right question.
"""

_JSON = re.compile(r"\{.*\}", re.DOTALL)


def _parse(text: str) -> dict:
    """Pull the JSON verdict out of the model text and coerce the fields."""
    m = _JSON.search(text or "")
    if not m:
        return {"correct": False, "score": 0.0, "reason": "unparseable judge output"}
    obj = json.loads(m.group(0))
    return {
        "correct": bool(obj.get("correct", False)),
        "score": float(obj.get("score", 0.0)),
        "reason": str(obj.get("reason", "")).strip(),
    }


def judge(
    question: str,
    pred_sql: str,
    gold_sql: str,
    explanation: str | None = None,
    evidence: str = "",
    model: str | None = None,
) -> dict:
    """One frontier-model call → {correct, score, reason}. Needs the API (offline-only use)."""
    prompt = PROMPT.format(
        question=question,
        evidence=evidence or "(none)",
        gold_sql=gold_sql,
        pred_sql=pred_sql or "(no query)",
        explanation=explanation or "(none)",
    )
    return _parse(agent._complete(prompt, model or config.JUDGE_MODEL))


def _selfcheck():
    ok = _parse('{"correct": true, "score": 0.9, "reason": "same aggregation over same table"}')
    assert ok == {"correct": True, "score": 0.9, "reason": "same aggregation over same table"}, ok
    fenced = _parse('```json\n{"correct": false, "score": 0.1, "reason": "wrong table"}\n```')
    assert fenced["correct"] is False and fenced["score"] == 0.1, fenced
    bad = _parse("the model rambled with no json")
    assert bad["correct"] is False and bad["score"] == 0.0, bad
    print("judge selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
