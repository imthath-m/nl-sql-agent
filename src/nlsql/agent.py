"""NL->SQL generator: first-pass generation and error-driven repair.

Contracts:
  generate_sql(question, evidence, schema_ddl) -> one SQLite SQL string (phase-1 baseline).
  repair_sql(question, evidence, schema_ddl, prev_sql, error) -> a corrected SQL string.
Both are single Gemini Flash calls with no execution. The executor loop (loop.solve) drives
retries; these functions just turn a prompt into SQL. Keep both surfaces stable.
"""
import re
import threading
import time

from google import genai

from . import config

_client: genai.Client | None = None
_client_lock = threading.Lock()  # ponytail: serialize lazy init; concurrent workers else build+GC-close duplicate clients

# Per-thread token accounting so the runners can attribute cost/latency per example
# (each example is solved on one worker thread). Opt-in: reset before an example,
# read after. When unused it just accumulates harmlessly.
_usage = threading.local()


def reset_usage() -> None:
    _usage.prompt = 0
    _usage.output = 0
    _usage.calls = 0


def get_usage() -> dict:
    """Tokens + LLM calls recorded on this thread since the last reset_usage()."""
    return {
        "prompt_tokens": getattr(_usage, "prompt", 0),
        "output_tokens": getattr(_usage, "output", 0),
        "llm_calls": getattr(_usage, "calls", 0),
    }


def _record_usage(resp) -> None:
    um = getattr(resp, "usage_metadata", None)
    _usage.calls = getattr(_usage, "calls", 0) + 1
    if um is not None:
        _usage.prompt = getattr(_usage, "prompt", 0) + (um.prompt_token_count or 0)
        _usage.output = getattr(_usage, "output", 0) + (um.candidates_token_count or 0)

# Dialect adaptation (phase 7). `sqlite` is the default and keeps phases 1-6 byte-identical
# (empty rules block). `bigquery`/`snowflake` add concise dialect-specific guidance for the
# Spider 2.0-Lite port. `dialect_name` labels the analyst; `dialect_rules` is appended to Rules.
_DIALECT_NAME = {
    "sqlite": "SQLite",
    "bigquery": "BigQuery GoogleSQL",
    "snowflake": "Snowflake SQL",
}
_DIALECT_RULES = {
    "sqlite": "",
    "bigquery": (
        "- This is BigQuery GoogleSQL. Reference tables as `project.dataset.table` in backticks.\n"
        "- Use SAFE_CAST, EXTRACT(part FROM ts), DATE_SUB/DATE_DIFF, and UNNEST() for ARRAY/STRUCT fields.\n"
        "- Prefer standard SQL functions; there is no SQLite-style `strftime` or `||`-only concat (use CONCAT).\n"
    ),
    "snowflake": (
        "- This is Snowflake SQL. Reference tables as DATABASE.SCHEMA.TABLE; identifiers are case-insensitive.\n"
        "- Use DATEADD/DATEDIFF, TO_DATE/TO_TIMESTAMP, ILIKE, QUALIFY, and FLATTEN() for semi-structured fields.\n"
    ),
}


def _dialect_bits(dialect: str) -> tuple[str, str]:
    """(display name, extra rules block) for a dialect key; falls back to SQLite."""
    return _DIALECT_NAME.get(dialect, "SQLite"), _DIALECT_RULES.get(dialect, "")


PROMPT = """You are an expert {dialect} analyst. Write ONE {dialect} query that answers the question.

Database schema:
{schema}

External knowledge (use it): {evidence}

Question: {question}
{plan}
Rules:
- Output ONLY the SQL query. No prose, no markdown, no code fences.
- Use exact table/column names from the schema; quote names with spaces in backticks.
- Return only the columns the question asks for.
{dialect_rules}"""

REPAIR_PROMPT = """Your previous {dialect} query failed to run. Fix it.

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
{dialect_rules}"""


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:  # double-checked: only one Client ever built
                _client = genai.Client()  # reads GEMINI_API_KEY or Vertex env automatically
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
            _record_usage(resp)
            return _strip_fence(resp.text or "")
        except Exception as e:  # transient API/rate-limit errors -> backoff and retry
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"generation failed after {retries} tries: {last_err}")


def _plan_block(plan: list[str] | None) -> str:
    """Render an optional phase-3 planner plan into the prompt (empty when no plan)."""
    if not plan:
        return ""
    return "\nPlan (follow these steps):\n" + "\n".join(f"- {s}" for s in plan) + "\n"


def list_models() -> list[str]:
    import os
    region = os.environ.get("GOOGLE_CLOUD_LOCATION", "(unset)")
    print(f"Vertex AI region: {region} (catalog only — inference availability may differ)")
    return [m.name for m in _get_client().models.list()]

def probe_model(model_name: str) -> bool:
    try:
        _get_client().models.get(model=model_name)
        return True
    except Exception:
        return False


def generate_sql(
    question: str,
    evidence: str,
    schema_ddl: str,
    model: str | None = None,
    retries: int = 3,
    plan: list[str] | None = None,
    dialect: str = "sqlite",
) -> str:
    name, rules = _dialect_bits(dialect)
    prompt = PROMPT.format(
        dialect=name,
        schema=schema_ddl,
        evidence=evidence or "(none)",
        question=question,
        plan=_plan_block(plan),
        dialect_rules=rules,
    )
    return _complete(prompt, model, retries)


def repair_sql(
    question: str,
    evidence: str,
    schema_ddl: str,
    prev_sql: str,
    error: str,
    model: str | None = None,
    retries: int = 3,
    dialect: str = "sqlite",
) -> str:
    """Re-generate the SQL given the query that failed and the DB's error message."""
    name, rules = _dialect_bits(dialect)
    prompt = REPAIR_PROMPT.format(
        dialect=name,
        schema=schema_ddl,
        evidence=evidence or "(none)",
        question=question,
        prev_sql=prev_sql,
        error=error,
        dialect_rules=rules,
    )
    return _complete(prompt, model, retries)
