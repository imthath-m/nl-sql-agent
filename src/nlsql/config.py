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


def db_path(db_id: str) -> Path:
    return DB_ROOT / db_id / f"{db_id}.sqlite"
