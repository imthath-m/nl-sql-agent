"""Executor: run generated SQL against the SQLite DB and capture rows or the error.

Phase-2 component. Unlike score.ex_score (which only cares if the result set matches gold),
the executor returns the actual error string so the loop can feed it back to the generator
for repair. Same interrupt-based timeout as the official BIRD scorer.
"""
import sqlite3
import threading
from dataclasses import dataclass


@dataclass
class ExecResult:
    ok: bool
    rows: list | None  # result rows on success, else None
    error: str | None  # "ErrorType: message" on failure, else None


def execute(sql: str, db_path: str, timeout: float = 30.0) -> ExecResult:
    """Run one query. Never raises — a failure (syntax, bad column, timeout) becomes ok=False."""
    con = sqlite3.connect(db_path)
    timer = threading.Timer(timeout, con.interrupt)  # matches official 30s func_timeout
    timer.start()
    try:
        rows = con.execute(sql).fetchall()
        return ExecResult(ok=True, rows=rows, error=None)
    except Exception as e:
        return ExecResult(ok=False, rows=None, error=f"{type(e).__name__}: {e}")
    finally:
        timer.cancel()
        con.close()


def _selfcheck():
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t(x INTEGER)")
    con.executemany("INSERT INTO t VALUES (?)", [(1,), (2,), (3,)])
    con.commit()
    con.close()

    ok = execute("SELECT x FROM t WHERE x > 1", path)
    assert ok.ok and ok.rows == [(2,), (3,)], ok

    bad = execute("SELECT nope FROM t", path)
    assert not bad.ok and bad.rows is None and "nope" in bad.error, bad

    os.remove(path)
    print("executor selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
