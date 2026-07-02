"""Result-Explainer: restate the query's answer in one plain sentence.

Phase-2 component. Takes the question and the executed result rows and returns a short,
jargon-free answer — no SQL, no table talk. One Gemini Flash call.
"""
from . import agent

PROMPT = """Answer the user's question in one plain sentence, using the SQL result rows.

Question: {question}

Result rows (Python repr): {rows}

Rules:
- Answer directly from the rows. No SQL, no column/table names, no jargon.
- If there are many rows, summarize (e.g. "12 customers, top three are ...").
- If there are no rows, say the query returned no results.
- Exactly one sentence.
"""


def explain(question: str, rows: list, model: str | None = None, max_rows: int = 50) -> str:
    """One-sentence natural-language answer. Rows are truncated so long results stay cheap."""
    shown = rows[:max_rows]
    suffix = f" (+{len(rows) - max_rows} more rows)" if len(rows) > max_rows else ""
    prompt = PROMPT.format(question=question, rows=repr(shown) + suffix)
    return agent._complete(prompt, model)
