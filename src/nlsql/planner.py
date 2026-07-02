"""Planner: decompose the question into ordered natural-language steps.

Phase-3 component. One Gemini call turns the question (+ evidence + pruned schema) into a
short numbered plan the generator follows. It does not choose tools or run anything — the
pipeline order (retrieve -> plan -> generate -> execute -> repair) is fixed — so it stays a
plain call, like the rest of the loop.
"""
import re

from . import agent

PROMPT = """You are planning how to answer a question with a single SQLite query.
Break the question into a short ordered list of steps: which tables to touch, the joins,
the filters, any aggregation, and the ordering/limit. Do NOT write SQL.

Database schema:
{schema}

External knowledge (use it): {evidence}

Question: {question}

Rules:
- 2 to 6 numbered steps.
- Each step is one short line.
- No SQL, and no prose before or after the list.
"""

_STEP = re.compile(r"^\s*\d+[.)]\s*(.+)$")


def _parse(text: str) -> list[str]:
    steps = [m.group(1).strip() for line in text.splitlines() if (m := _STEP.match(line))]
    return steps or ([text.strip()] if text.strip() else [])


def plan(question: str, evidence: str, schema_ddl: str, model: str | None = None) -> list[str]:
    """Ordered plain-language steps for answering the question. Never empty on real output."""
    prompt = PROMPT.format(schema=schema_ddl, evidence=evidence or "(none)", question=question)
    return _parse(agent._complete(prompt, model))


def _selfcheck():
    steps = _parse("1. Join store and orders on id\n2) Filter total > 100\n3. Order by total desc")
    assert steps == [
        "Join store and orders on id",
        "Filter total > 100",
        "Order by total desc",
    ], steps
    assert _parse("just one line") == ["just one line"]
    assert _parse("") == []
    print("planner selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
