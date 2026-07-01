"""Execution-accuracy scorer.

Mirrors the official BIRD EX metric (bird-sql/evaluation/evaluation_ex.py): run predicted
and gold SQL against the SQLite DB and compare result sets; any error or timeout scores 0.
We reimplement the ~15 lines rather than import evaluation_utils because that module imports
psycopg2/pymysql at top level (Postgres/MySQL paths we don't use) and would fail to load.
"""
import sqlite3
import threading


def _run(db_path: str, sql: str, timeout: float) -> list:
    con = sqlite3.connect(db_path)
    timer = threading.Timer(timeout, con.interrupt)  # matches official 30s func_timeout
    timer.start()
    try:
        return con.execute(sql).fetchall()
    finally:
        timer.cancel()
        con.close()


def ex_score(pred_sql: str, gold_sql: str, db_path: str, timeout: float = 30.0) -> int:
    """1 if predicted result set equals gold's, else 0. Error/timeout -> 0 (official behavior)."""
    try:
        pred = _run(db_path, pred_sql, timeout)
        gold = _run(db_path, gold_sql, timeout)
    except Exception:
        return 0
    return int(set(pred) == set(gold))


def _selfcheck():
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t(x INTEGER)")
    con.executemany("INSERT INTO t VALUES (?)", [(1,), (2,), (3,)])
    con.commit()
    con.close()
    gold = "SELECT x FROM t WHERE x > 1"
    assert ex_score("SELECT x FROM t WHERE x > 1", gold, path) == 1  # same set
    assert ex_score("SELECT x FROM t ORDER BY x DESC", gold, path) == 0  # extra row
    assert ex_score("SELECT nope FROM t", gold, path) == 0  # error -> 0
    os.remove(path)
    print("score selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
