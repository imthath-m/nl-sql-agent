"""NL->SQL generator: first-pass generation and error-driven repair.

Contracts:
  generate_sql(question, evidence, schema_ddl) -> one SQLite SQL string (phase-1 baseline).
  repair_sql(question, evidence, schema_ddl, prev_sql, error) -> a corrected SQL string.
Both are single Gemini Flash calls with no execution. The executor loop (loop.solve) drives
retries; these functions just turn a prompt into SQL. Keep both surfaces stable.
"""
import re
import time

from google import genai

from . import config

_client: genai.Client | None = None

PROMPT = """You are an expert SQLite analyst. Write ONE SQLite query that answers the question.

Database schema:
{schema}

External knowledge (use it): {evidence}

Question: {question}

Rules:
- Output ONLY the SQL query. No prose, no markdown, no code fences.
- Use exact table/column names from the schema; quote names with spaces in backticks.
- Return only the columns the question asks for.
"""

REPAIR_PROMPT = """Your previous SQLite query failed to run. Fix it.

Database schema:
{schema}

External knowledge (use it): {evidence}

Question: {question}

Your previous query:
{prev_sql}

The database returned this error:
{error}

Rules:
- Output ONLY the corrected SQL query. No prose, no markdown, no code fences.
- Use exact table/column names from the schema; quote names with spaces in backticks.
- Return only the columns the question asks for.
"""


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client()  # reads GEMINI_API_KEY / Vertex env automatically
    return _client


def _strip_fence(text: str) -> str:
    """Defensive: drop ```sql ... ``` fences if the model adds them anyway."""
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    sql = (m.group(1) if m else text).strip()
    return sql.rstrip(";").strip()


def _complete(prompt: str, model: str | None, retries: int = 3) -> str:
    """One Gemini call, backing off on transient API/rate-limit errors. Returns clean SQL."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = _get_client().models.generate_content(
                model=model or config.MODEL, contents=prompt
            )
            return _strip_fence(resp.text or "")
        except Exception as e:  # transient API/rate-limit errors -> backoff and retry
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"generation failed after {retries} tries: {last_err}")


def generate_sql(
    question: str,
    evidence: str,
    schema_ddl: str,
    model: str | None = None,
    retries: int = 3,
) -> str:
    prompt = PROMPT.format(schema=schema_ddl, evidence=evidence or "(none)", question=question)
    return _complete(prompt, model, retries)


def repair_sql(
    question: str,
    evidence: str,
    schema_ddl: str,
    prev_sql: str,
    error: str,
    model: str | None = None,
    retries: int = 3,
) -> str:
    """Re-generate the SQL given the query that failed and the DB's error message."""
    prompt = REPAIR_PROMPT.format(
        schema=schema_ddl,
        evidence=evidence or "(none)",
        question=question,
        prev_sql=prev_sql,
        error=error,
    )
    return _complete(prompt, model, retries)
