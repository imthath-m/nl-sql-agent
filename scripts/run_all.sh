#!/usr/bin/env bash
# One-command runner: baseline vs loop vs phase3 on a seeded subset. Args pass through.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
exec "$PY" -m nlsql.run_all "$@"
