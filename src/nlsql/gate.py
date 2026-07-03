"""Abstention gate: refuse low-confidence answers and route them to a human.

Phase-5 component, and our own addition — BIRD does not reward abstention (see README).
Rule-based and offline: a cheap confidence score built from signals the pipeline already
produces, with no extra LLM call. Three signals, each in [0, 1]:

  retrieval — mean cosine similarity of the tables the retriever kept (how confidently
              the schema was linked at the embedding stage).
  linking   — fraction of the generated SQL's tables that were among the retrieved ones
              (agreement between what was retrieved and what the query actually used).
  execution — 1.0 if the SQL ran clean on the first try, decaying per repair attempt,
              0.0 if it never ran.

When retrieval is off, the retrieval/linking signals default to 1.0 (there is no
retrieval signal to penalize). Overall confidence is the mean of the three; the gate
refuses (abstains) when it falls below `config.ABSTAIN_THRESHOLD`. `decide` returns a
row-shaped verdict so the same logic drives the live loop and the offline gate eval.
"""
from . import config, schema_link


def _clamp(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def retrieval_signal(scores: list[float] | None) -> float:
    """Mean cosine of the kept tables. 1.0 when retrieval was off / nothing pruned."""
    if not scores:
        return 1.0
    return round(_clamp(sum(scores) / len(scores)), 4)


def linking_signal(pred_sql: str, retrieved: list[str] | None) -> float:
    """Fraction of the SQL's tables that were retrieved. 1.0 when retrieval was off."""
    if not retrieved:
        return 1.0
    used = schema_link.tables_in_sql(pred_sql or "")
    if not used:
        return 1.0
    kept = {t.lower() for t in retrieved}
    return round(len(used & kept) / len(used), 4)


def execution_signal(attempts: int | None, exec_ok, max_repairs: int = 3) -> float:
    """1.0 clean, minus 1/(max_repairs+1) per extra attempt; 0.0 if it never ran."""
    if exec_ok is False:
        return 0.0
    if not attempts or attempts <= 1:
        return 1.0
    return round(_clamp(1.0 - (attempts - 1) / (max_repairs + 1)), 4)


def confidence(row: dict, max_repairs: int = 3) -> dict:
    """Overall confidence + its three component signals for a run-file result row.

    Reads `retrieval_scores`, `retrieved_tables`, `pred`, `attempts`, `exec_ok`.
    """
    signals = {
        "retrieval": retrieval_signal(row.get("retrieval_scores")),
        "linking": linking_signal(row.get("pred") or "", row.get("retrieved_tables")),
        "execution": execution_signal(row.get("attempts"), row.get("exec_ok"), max_repairs),
    }
    overall = round(sum(signals.values()) / len(signals), 4)
    return {"confidence": overall, "signals": signals}


def decide(row: dict, threshold: float | None = None, max_repairs: int = 3) -> dict:
    """Gate verdict for a row: confidence, abstain flag, human-readable reason, signals.

    Abstains (refuses, routes to a human) when confidence < threshold
    (default `config.ABSTAIN_THRESHOLD`). The reason names the weakest signal.
    """
    threshold = config.ABSTAIN_THRESHOLD if threshold is None else threshold
    c = confidence(row, max_repairs)
    conf, signals = c["confidence"], c["signals"]
    abstain = conf < threshold
    reason = ""
    if abstain:
        name, val = min(signals.items(), key=lambda kv: kv[1])
        reason = f"confidence {conf:.2f} < {threshold:.2f} (weakest signal: {name}={val:.2f})"
    return {
        "confidence": conf,
        "abstain": abstain,
        "reason": reason,
        "signals": signals,
        "threshold": threshold,
    }


def _selfcheck():
    # High confidence: retrieval sure, SQL uses a retrieved table, ran clean -> served.
    high = decide(
        {"retrieval_scores": [0.9, 0.85], "retrieved_tables": ["a", "b"],
         "pred": "SELECT x FROM a", "attempts": 1, "exec_ok": True},
        threshold=0.55,
    )
    assert high["signals"] == {"retrieval": 0.875, "linking": 1.0, "execution": 1.0}, high
    assert abs(high["confidence"] - 0.9583) < 1e-3 and high["abstain"] is False, high

    # Low retrieval + SQL touches an un-retrieved table -> abstain, weakest = linking.
    low = decide(
        {"retrieval_scores": [0.2, 0.15], "retrieved_tables": ["a", "b"],
         "pred": "SELECT x FROM c", "attempts": 1, "exec_ok": True},
        threshold=0.55,
    )
    assert low["signals"]["linking"] == 0.0 and low["signals"]["retrieval"] == 0.175, low
    assert low["abstain"] is True and "linking" in low["reason"], low

    # Execution signal decays with repairs and floors at 0 when it never ran.
    assert execution_signal(1, True) == 1.0
    assert execution_signal(3, True, max_repairs=3) == 0.5  # 1 - 2/4
    assert execution_signal(2, False) == 0.0
    assert execution_signal(None, None) == 1.0  # unknown exec status -> no penalty

    # Retrieval off (baseline/loop rows): retrieval + linking default to 1.0.
    off = confidence({"pred": "SELECT x FROM a", "attempts": 1, "exec_ok": True})
    assert off["signals"]["retrieval"] == 1.0 and off["signals"]["linking"] == 1.0, off
    assert off["confidence"] == 1.0, off

    # Threshold controls the decision boundary for the same confidence.
    mid_row = {"retrieval_scores": [0.5, 0.5], "retrieved_tables": ["a"],
               "pred": "SELECT x FROM a", "attempts": 1, "exec_ok": True}
    assert decide(mid_row, threshold=0.9)["abstain"] is True  # conf ~0.833
    assert decide(mid_row, threshold=0.6)["abstain"] is False
    print("gate selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
