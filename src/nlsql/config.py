"""Paths and model config. Everything overridable by env; sane defaults for local dev."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Repo root = nl-sql-agent/ ; BIRD data lives one level up by default.
REPO_ROOT = Path(__file__).resolve().parents[2]
BIRD_DIR = Path(os.environ.get("BIRD_DIR", REPO_ROOT.parent / "bird-data" / "dev_20240627"))

DEV_JSON = BIRD_DIR / "dev.json"
DB_ROOT = BIRD_DIR / "dev_databases"

RUNS_DIR = REPO_ROOT / "runs"

# LLM provider: "gemini" (google-genai; GEMINI_API_KEY or Vertex env) or "openai".
PROVIDER = os.environ.get("NLSQL_PROVIDER", "gemini").strip().lower()
_OPENAI = PROVIDER == "openai"

MODEL = os.environ.get("NLSQL_MODEL", "gpt-4.1-mini" if _OPENAI else "gemini-2.5-flash")

# Phase-3 schema retriever.
EMBED_MODEL = os.environ.get("NLSQL_EMBED_MODEL", "text-embedding-3-small" if _OPENAI else "text-embedding-004"
)
RETRIEVE_TOP_K = int(os.environ.get("NLSQL_RETRIEVE_TOP_K", "5"))

# Phase-4 LLM judge: frontier model, run offline only (per plan.md model strategy).
JUDGE_MODEL = os.environ.get("NLSQL_JUDGE_MODEL", "gpt-4.1" if _OPENAI else "gemini-2.5-pro")

# Phase-5 abstention gate: refuse when overall confidence falls below this threshold.
# Our addition — BIRD does not reward abstention (see README).
ABSTAIN_THRESHOLD = float(os.environ.get("NLSQL_ABSTAIN_THRESHOLD", "0.55"))


def db_path(db_id: str) -> Path:
    return DB_ROOT / db_id / f"{db_id}.sqlite"
