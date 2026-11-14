"""NL->SQL generator: first-pass generation and error-driven repair.

Contracts:
  generate_sql(question, evidence, schema_ddl) -> one SQLite SQL string (phase-1 baseline).
  repair_sql(question, evidence, schema_ddl, prev_sql, error) -> a corrected SQL string.
Both are single Gemini Flash calls with no execution. The executor loop (loop.solve) drives
retries; these functions just turn a prompt into SQL. Keep both surfaces stable.
"""
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from . import config

_client = None  # google.genai.Client, built lazily (gemini provider only)
_oai_client = None  # openai.OpenAI, built lazily (openai provider only)
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
        # Gemini 2.5 thinking tokens are billed as output; count them or cost is undercounted.
        _usage.output = getattr(_usage, "output", 0) + (um.candidates_token_count or 0) + (
            getattr(um, "thoughts_token_count", 0) or 0
        )

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt(name: str) -> str:
    """Versioned prompt template from src/nlsql/prompts/<name>.txt (".txt" optional)."""
    fname = name if name.endswith(".txt") else name + ".txt"
    return (_PROMPTS_DIR / fname).read_text()


PROMPT = load_prompt("generator_v1")

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


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:  # double-checked: only one Client ever built
                from google import genai  # lazy: the openai path never needs it

                _client = genai.Client()  # reads GEMINI_API_KEY or Vertex env automatically
    return _client


def _get_openai():
    """OpenAI client. The key may be injected by a network proxy, so send a placeholder."""
    global _oai_client
    if _oai_client is None:
        with _client_lock:
            if _oai_client is None:
                from openai import OpenAI

                _oai_client = OpenAI(
                    api_key=os.environ.get("OPENAI_API_KEY", "proxy-injected"), max_retries=0
                )
    return _oai_client


def _call_llm(prompt: str, model: str) -> tuple[str, dict]:
    """One provider call -> (text, usage dict for the cache). Records thread usage."""
    if config.PROVIDER == "openai":
        resp = _get_openai().chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}], temperature=0
        )
        u = resp.usage
        _usage.calls = getattr(_usage, "calls", 0) + 1
        pt, ct = (u.prompt_tokens or 0, u.completion_tokens or 0) if u else (0, 0)
        _usage.prompt = getattr(_usage, "prompt", 0) + pt
        _usage.output = getattr(_usage, "output", 0) + ct  # includes reasoning tokens
        return resp.choices[0].message.content or "", {"prompt_tokens": pt, "completion_tokens": ct}
    resp = _get_client().models.generate_content(model=model, contents=prompt)
    _record_usage(resp)
    um = getattr(resp, "usage_metadata", None)
    usage = {k: getattr(um, k, None) for k in
             ("prompt_token_count", "candidates_token_count", "thoughts_token_count")}
    return resp.text or "", usage


def _strip_fence(text: str) -> str:
    """Defensive: drop ```sql ... ``` fences if the model adds them anyway."""
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    sql = (m.group(1) if m else text).strip()
    return sql.rstrip(";").strip()


def _cache_path(prompt: str, model: str) -> Path | None:
    """Opt-in response cache: only when NLSQL_CACHE_DIR is set (off by default)."""
    d = os.environ.get("NLSQL_CACHE_DIR")
    if not d:
        return None
    key = hashlib.sha256((model + "\n" + prompt).encode()).hexdigest()  # model names are provider-distinct
    return Path(d) / f"{key}.json"


def _complete(prompt: str, model: str | None, retries: int = 3) -> str:
    """One LLM call (config.PROVIDER), backing off on transient API/rate-limit errors. Returns clean SQL."""
    model = model or config.MODEL
    cache = _cache_path(prompt, model)
    if cache is not None and cache.exists():
        # cache hit: no API call, so record nothing (zero tokens / zero llm_calls)
        return _strip_fence(json.loads(cache.read_text())["text"])
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            text, usage = _call_llm(prompt, model)
            if cache is not None:
                cache.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache.with_suffix(f".{threading.get_ident()}.tmp")
                tmp.write_text(json.dumps({"model": model, "text": text, "usage": usage}))
                tmp.replace(cache)  # atomic: concurrent workers never read a half-written file
            return _strip_fence(text)
        except Exception as e:  # transient API/rate-limit errors -> backoff and retry
            last_err = e
            if attempt < retries - 1:
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
    prompt_template: str | None = None,
) -> str:
    """prompt_template: optional generator template (same placeholders as PROMPT); default v1."""
    prompt = (prompt_template or PROMPT).format(
        schema=schema_ddl,
        evidence=evidence or "(none)",
        question=question,
        plan=_plan_block(plan),
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
