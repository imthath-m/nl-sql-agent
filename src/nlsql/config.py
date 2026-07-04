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
MODEL = os.environ.get("NLSQL_MODEL", "gemini-2.5-flash")

# Phase-3 schema retriever.
EMBED_MODEL = os.environ.get("NLSQL_EMBED_MODEL", "text-embedding-004")
RETRIEVE_TOP_K = int(os.environ.get("NLSQL_RETRIEVE_TOP_K", "5"))

# Phase-4 LLM judge: frontier model, run offline only (per plan.md model strategy).
JUDGE_MODEL = os.environ.get("NLSQL_JUDGE_MODEL", "gemini-2.5-pro")

# Phase-5 abstention gate: refuse when overall confidence falls below this threshold.
# Our addition — BIRD does not reward abstention (see README).
ABSTAIN_THRESHOLD = float(os.environ.get("NLSQL_ABSTAIN_THRESHOLD", "0.55"))

# Phase-7 Spider 2.0-Lite. The cloned xlang-ai/Spider2 repo's spider2-lite/ dir lives one
# level up by default (alongside bird-data/), overridable by env. COL_BUDGET caps columns
# per table for the wide-schema pruner (Spider2 tables can exceed 1,000 columns).
SPIDER2_DIR = Path(os.environ.get("SPIDER2_DIR", REPO_ROOT.parent / "Spider2" / "spider2-lite"))
SPIDER2_COL_BUDGET = int(os.environ.get("NLSQL_COL_BUDGET", "40"))


def db_path(db_id: str) -> Path:
    return DB_ROOT / db_id / f"{db_id}.sqlite"
