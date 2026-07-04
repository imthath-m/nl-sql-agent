"""Phase-6 review view: a thin, static HTML audit page over a run/trace file.

The auditability deliverable. Reads a run JSON (any runner; richest with `run_phase5`, which
records per-example gate confidence + step traces) fully offline and writes one self-contained
HTML file — no web framework, no new dependencies, inline CSS, no JS beyond the native
`<details>` toggle. Examples are listed **low-confidence-first** so the gate's refusals and the
shakiest answers surface at the top; each row expands to show:

  - the ordered step trace (retrieve -> plan -> generate -> execute -> repair -> gate -> explain),
  - the final SQL and the retrieved tables + pruned schema it was written against (the audit link),
  - the confidence signals and the abstention decision,
  - the plain-language answer.

    python -m nlsql.review runs/phase5_<ts>.json
"""
import argparse
import html
import json
from datetime import datetime, timezone

from . import config

_CSS = """
* { box-sizing: border-box; }
body { font: 14px/1.5 -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 24px;
       color: #1a1a1a; background: #f6f7f9; }
h1 { font-size: 20px; margin: 0 0 4px; }
.sub { color: #666; margin: 0 0 20px; }
.stats { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
.stat { background: #fff; border: 1px solid #e2e5ea; border-radius: 8px; padding: 10px 14px; }
.stat b { display: block; font-size: 18px; }
.stat span { color: #666; font-size: 12px; }
details { background: #fff; border: 1px solid #e2e5ea; border-radius: 8px; margin-bottom: 8px; }
summary { cursor: pointer; padding: 12px 14px; display: flex; align-items: center; gap: 12px;
          list-style: none; }
summary::-webkit-details-marker { display: none; }
.conf { font-variant-numeric: tabular-nums; font-weight: 700; width: 52px; }
.bar { flex: 0 0 90px; height: 8px; border-radius: 4px; background: #e6e8ec; overflow: hidden; }
.bar i { display: block; height: 100%; }
.q { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tag { font-size: 11px; padding: 2px 8px; border-radius: 10px; white-space: nowrap; }
.abstain { background: #fde2e1; color: #a4271f; }
.served { background: #e2f3e6; color: #216e39; }
.db { color: #888; font-size: 12px; }
.body { padding: 4px 16px 16px; border-top: 1px solid #eef0f3; }
.body h4 { margin: 16px 0 6px; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #888; }
pre { background: #f4f5f7; border: 1px solid #e6e8ec; border-radius: 6px; padding: 10px;
      overflow-x: auto; margin: 0; font: 12px/1.45 ui-monospace, Menlo, monospace; }
table.trace { width: 100%; border-collapse: collapse; font-size: 12px; }
table.trace th, table.trace td { text-align: left; padding: 5px 8px; border-bottom: 1px solid #eef0f3;
      vertical-align: top; }
table.trace th { color: #888; font-weight: 600; }
.io { color: #444; font: 11px/1.4 ui-monospace, Menlo, monospace; word-break: break-word; }
.chip { display: inline-block; background: #eef1f5; border-radius: 6px; padding: 2px 8px; margin: 0 4px 4px 0;
        font-size: 12px; }
.sig { display: inline-block; margin-right: 14px; }
.sig b { font-variant-numeric: tabular-nums; }
.reason { color: #a4271f; }
"""


def _conf_of(row: dict):
    """A row's gate confidence, or None when the gate did not run on it."""
    c = row.get("confidence")
    return c if isinstance(c, (int, float)) else None


def sort_rows(rows: list[dict]) -> list[dict]:
    """Ascending by confidence — lowest first — with un-scored rows last (order-stable)."""
    def key(row):
        c = _conf_of(row)
        return (c is None, c if c is not None else 0.0)
    return sorted(rows, key=key)


def _e(x) -> str:
    return html.escape(str(x))


def _bar(conf) -> str:
    if conf is None:
        return '<span class="bar"><i style="width:0"></i></span>'
    pct = int(round(conf * 100))
    hue = int(round(conf * 120))  # 0=red .. 120=green
    return f'<span class="bar"><i style="width:{pct}%;background:hsl({hue},60%,45%)"></i></span>'


def _io(v) -> str:
    return _e(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)


def _trace_table(trace: dict) -> str:
    steps = trace.get("steps", [])
    if not steps:
        return "<p class='db'>no trace recorded</p>"
    body = "".join(
        f"<tr><td><b>{_e(s.get('name',''))}</b></td><td>{s.get('latency_s','—')}s</td>"
        f"<td class='io'>{_io(s.get('inputs',{}))}</td><td class='io'>{_io(s.get('output',{}))}</td></tr>"
        for s in steps
    )
    return ("<table class='trace'><tr><th>step</th><th>latency</th><th>inputs</th>"
            f"<th>output</th></tr>{body}</table>")


def _signals(row: dict) -> str:
    sig = row.get("gate_signals") or {}
    if not sig:
        return ""
    chips = "".join(f"<span class='sig'>{_e(k)} <b>{v}</b></span>" for k, v in sig.items())
    reason = row.get("gate_reason")
    r = f"<div class='reason'>{_e(reason)}</div>" if reason else ""
    return f"<h4>Confidence signals</h4>{chips}{r}"


def _schema_link(row: dict) -> str:
    """The answer -> SQL -> retrieved schema chain, from the trace link or the row fields."""
    link = (row.get("trace") or {}).get("links") or {}
    schema = link.get("schema") or {}
    tables = schema.get("tables")
    if tables is None:
        tables = row.get("retrieved_tables")
    ddl = schema.get("pruned_ddl")
    chips = ("".join(f"<span class='chip'>{_e(t)}</span>" for t in tables)
             if tables else "<span class='db'>retrieval off — full schema used</span>")
    out = f"<h4>Retrieved tables (SQL was written against these)</h4>{chips}"
    if ddl:
        out += f"<pre>{_e(ddl)}</pre>"
    return out


def _row_html(row: dict) -> str:
    conf = _conf_of(row)
    conf_s = f"{conf:.2f}" if conf is not None else "—"
    abstained = bool(row.get("abstain"))
    tag = ("<span class='tag abstain'>ABSTAINED</span>" if abstained
           else "<span class='tag served'>served</span>")
    qid = row.get("question_id", "?")
    question = row.get("question", "")
    sql = row.get("pred") or ""
    explanation = row.get("explanation")
    answer = (f"<h4>Answer</h4><p>{_e(explanation)}</p>" if explanation
              else "<h4>Answer</h4><p class='db'>no answer — routed to a human</p>"
              if abstained else "")

    return (
        "<details><summary>"
        f"<span class='conf'>{conf_s}</span>{_bar(conf)}"
        f"<span class='q'>{_e(question)}</span>"
        f"<span class='db'>{_e(row.get('db_id',''))} · #{_e(qid)}</span>{tag}"
        "</summary><div class='body'>"
        f"{_signals(row)}"
        f"<h4>Final SQL</h4><pre>{_e(sql)}</pre>"
        f"{_schema_link(row)}"
        f"{answer}"
        f"<h4>Step trace</h4>{_trace_table(row.get('trace') or {})}"
        "</div></details>"
    )


def render(run: dict) -> str:
    rows = run.get("results", [])
    ordered = sort_rows(rows)
    confs = [c for c in (_conf_of(r) for r in rows) if c is not None]
    n_abstain = sum(bool(r.get("abstain")) for r in rows)
    mean_c = f"{sum(confs) / len(confs):.3f}" if confs else "—"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    stats = (
        f"<div class='stat'><b>{len(rows)}</b><span>examples</span></div>"
        f"<div class='stat'><b>{n_abstain}</b><span>abstained</span></div>"
        f"<div class='stat'><b>{mean_c}</b><span>mean confidence</span></div>"
    )
    body = "".join(_row_html(r) for r in ordered)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>nl-sql review</title><style>{_CSS}</style></head><body>"
        "<h1>nl-sql-agent — review</h1>"
        f"<p class='sub'>lowest confidence first · generated {stamp}</p>"
        f"<div class='stats'>{stats}</div>{body}</body></html>"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Phase-6 static HTML review view over a run file")
    p.add_argument("run", help="run JSON (best from run_phase5: per-row confidence + trace)")
    p.add_argument("-o", "--out", help="output HTML path (default runs/review_<ts>.html)")
    args = p.parse_args()

    run = json.loads(open(args.run).read())
    doc = render(run)

    config.RUNS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or (config.RUNS_DIR / f"review_{stamp}.html")
    with open(out, "w") as f:
        f.write(doc)
    print(f"wrote {out}")


def _selfcheck():
    run = {
        "summary": {"abstain_threshold": 0.55},
        "results": [
            {"question_id": 1, "db_id": "shop", "question": "How many stores?",
             "confidence": 0.92, "abstain": False, "pred": "SELECT COUNT(*) FROM store",
             "gate_signals": {"retrieval": 0.9, "linking": 1.0, "execution": 1.0},
             "explanation": "There are 12 stores.", "retrieved_tables": ["store"],
             "trace": {"steps": [
                 {"name": "retrieve", "inputs": {"top_k": 5}, "output": {"tables": ["store"]},
                  "latency_s": 0.1, "ts": "t"},
                 {"name": "generate", "inputs": {}, "output": {"sql": "SELECT COUNT(*) FROM store"},
                  "latency_s": 0.2, "ts": "t"}],
                 "links": {"answer": "There are 12 stores.", "sql": "SELECT COUNT(*) FROM store",
                           "schema": {"tables": ["store"], "pruned_ddl": "CREATE TABLE store(id INT)"}}}},
            {"question_id": 2, "db_id": "shop", "question": "Weirdly phrased ambiguous thing?",
             "confidence": 0.31, "abstain": True, "pred": "SELECT x FROM mystery",
             "gate_signals": {"retrieval": 0.2, "linking": 0.0, "execution": 1.0},
             "gate_reason": "confidence 0.31 < 0.55 (weakest signal: linking=0.00)",
             "explanation": None, "retrieved_tables": ["a", "b"], "trace": {"steps": [], "links": {}}},
            {"question_id": 3, "db_id": "shop", "question": "no gate row",
             "pred": "SELECT 1", "explanation": "One."},  # no confidence -> sorts last
        ],
    }
    ordered = sort_rows(run["results"])
    assert [r["question_id"] for r in ordered] == [2, 1, 3], [r["question_id"] for r in ordered]

    doc = render(run)
    # low-confidence example renders before the high-confidence one.
    assert doc.index("ambiguous") < doc.index("How many stores"), "not low-confidence-first"
    # trace, SQL, retrieved schema and the answer all render.
    assert "Step trace" in doc and "SELECT COUNT(*) FROM store" in doc, "sql/trace missing"
    assert "CREATE TABLE store" in doc and "Retrieved tables" in doc, "schema link missing"
    assert "There are 12 stores." in doc and "ABSTAINED" in doc, "answer/decision missing"
    assert "retrieval" in doc and "0.2" in doc, "confidence signals missing"
    assert doc.startswith("<!doctype html>") and doc.rstrip().endswith("</html>")
    print("review selfcheck ok")


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 2 and sys.argv[1] == "_selfcheck":
        _selfcheck()
    else:
        main()
