"""Warehouse executor adapter: run a query against the example's backend.

Phase-7 component. Spider 2.0-Lite examples live on three backends. This adapter routes by
`example["platform"]`:

  sqlite     — run locally through the existing executor.py (executor.ExecResult), same as BIRD.
  bigquery   — run via google-cloud-bigquery (optional dependency).
  snowflake  — run via snowflake-connector-python (optional dependency).

Live BigQuery/Snowflake execution is NOT required to score Spider2-Lite (we compare against
the released gold-result CSVs via spider2_score), so the warehouse clients are optional and
deliberately kept out of requirements.txt. When a warehouse example is run without the client
library or its credentials, `execute` raises `WarehouseUnavailable` with an actionable message
rather than failing obscurely; the runner records that as the example's error.

Credentials are read from env, following config.py conventions:
  BigQuery  — GOOGLE_CLOUD_PROJECT (+ standard ADC / GOOGLE_APPLICATION_CREDENTIALS).
  Snowflake — SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD (+ optional SNOWFLAKE_ROLE).
"""
import os

from . import executor, spider2


class WarehouseUnavailable(RuntimeError):
    """A BigQuery/Snowflake example was run without its client library or credentials."""


def _need(cond, msg: str) -> None:
    if not cond:
        raise WarehouseUnavailable(msg)


def _run_bigquery(sql: str) -> executor.ExecResult:
    try:
        from google.cloud import bigquery
    except ImportError:
        raise WarehouseUnavailable(
            "BigQuery execution needs the optional 'google-cloud-bigquery' package "
            "(pip install google-cloud-bigquery). Scoring uses the gold-result CSVs instead."
        )
    _need(os.environ.get("GOOGLE_CLOUD_PROJECT"),
          "BigQuery execution needs GOOGLE_CLOUD_PROJECT set (+ ADC credentials).")
    try:
        client = bigquery.Client(project=os.environ["GOOGLE_CLOUD_PROJECT"])
        rows = [tuple(r.values()) for r in client.query(sql).result()]
        return executor.ExecResult(ok=True, rows=rows, error=None)
    except Exception as e:
        return executor.ExecResult(ok=False, rows=None, error=f"{type(e).__name__}: {e}")


def _run_snowflake(sql: str) -> executor.ExecResult:
    try:
        import snowflake.connector
    except ImportError:
        raise WarehouseUnavailable(
            "Snowflake execution needs the optional 'snowflake-connector-python' package "
            "(pip install snowflake-connector-python). Scoring uses the gold-result CSVs instead."
        )
    for var in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"):
        _need(os.environ.get(var), f"Snowflake execution needs {var} set.")
    try:
        con = snowflake.connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ["SNOWFLAKE_USER"],
            password=os.environ["SNOWFLAKE_PASSWORD"],
            role=os.environ.get("SNOWFLAKE_ROLE"),
        )
        try:
            rows = [tuple(r) for r in con.cursor().execute(sql).fetchall()]
        finally:
            con.close()
        return executor.ExecResult(ok=True, rows=rows, error=None)
    except Exception as e:
        return executor.ExecResult(ok=False, rows=None, error=f"{type(e).__name__}: {e}")


def execute(sql: str, example: dict) -> executor.ExecResult:
    """Run `sql` against the example's backend. sqlite runs locally; bigquery/snowflake need
    their optional client + credentials, else raise WarehouseUnavailable."""
    platform = example.get("platform") or spider2.platform_of(example["instance_id"])
    if platform == "sqlite":
        return executor.execute(sql, str(spider2.sqlite_path(example["db"])))
    if platform == "bigquery":
        return _run_bigquery(sql)
    if platform == "snowflake":
        return _run_snowflake(sql)
    raise WarehouseUnavailable(f"unknown platform {platform!r} for {example.get('instance_id')}")


def _selfcheck():
    import sqlite3
    import tempfile
    from pathlib import Path

    from . import config

    # sqlite path routes through the local executor and returns rows.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "spider2-lite"
        sq = root / "resource" / "databases" / "sqlite"
        sq.mkdir(parents=True)
        con = sqlite3.connect(str(sq / "shop.sqlite"))
        con.execute("CREATE TABLE t(x INTEGER)")
        con.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])
        con.commit()
        con.close()
        old = config.SPIDER2_DIR
        config.SPIDER2_DIR = root
        try:
            res = execute("SELECT x FROM t ORDER BY x", {"instance_id": "local1", "db": "shop",
                                                          "platform": "sqlite"})
            assert res.ok and res.rows == [(1,), (2,)], res
        finally:
            config.SPIDER2_DIR = old

    # bigquery/snowflake without the client lib -> a clear, actionable error (not a crash).
    for platform in ("bigquery", "snowflake"):
        try:
            execute("SELECT 1", {"instance_id": "x", "db": "d", "platform": platform})
            assert False, "expected WarehouseUnavailable"
        except WarehouseUnavailable as e:
            assert "optional" in str(e) or "needs" in str(e), e
    print("warehouse selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
