"""Cost and latency accounting for a run.

Phase-4 eval component. Turns the per-example token counts / wall-clock the runners
record (`prompt_tokens`, `output_tokens`, `latency_s` — see agent.get_usage) into a
USD estimate from a per-model price table, plus latency stats. Prices are USD per
1M tokens and fully configurable via the NLSQL_PRICES env var (JSON:
`{"model": {"input": <usd>, "output": <usd>}}`), which is merged over the defaults.
Offline and deterministic.
"""
import json
import os

# USD per 1M tokens. Defaults for the models this project uses; override via NLSQL_PRICES.
_DEFAULT_PRICES = {
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
    "text-embedding-004": {"input": 0.0, "output": 0.0},
}


def _prices() -> dict:
    prices = {k: dict(v) for k, v in _DEFAULT_PRICES.items()}
    override = os.environ.get("NLSQL_PRICES")
    if override:
        for model, p in json.loads(override).items():
            prices.setdefault(model, {"input": 0.0, "output": 0.0}).update(p)
    return prices


def cost_usd(model: str, prompt_tokens: int, output_tokens: int) -> float:
    """Estimated USD for one model's token usage. Unknown model → 0.0 (no price on file)."""
    p = _prices().get(model, {"input": 0.0, "output": 0.0})
    return (prompt_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000


def run_cost(rows: list[dict], model: str) -> dict:
    """Aggregate cost + latency over run-file result rows.

    Reads `prompt_tokens` / `output_tokens` / `llm_calls` / `latency_s` per row (all
    default to 0 when a run predates token/latency tracking, so old runs still summarize).
    """
    prompt = sum(r.get("prompt_tokens", 0) for r in rows)
    output = sum(r.get("output_tokens", 0) for r in rows)
    calls = sum(r.get("llm_calls", 0) for r in rows)
    lat = [r["latency_s"] for r in rows if r.get("latency_s") is not None]
    lat_sorted = sorted(lat)
    n = len(lat_sorted)
    total_cost = cost_usd(model, prompt, output)
    return {
        "model": model,
        "prompt_tokens": prompt,
        "output_tokens": output,
        "llm_calls": calls,
        "est_cost_usd": round(total_cost, 6),
        "cost_per_example_usd": round(total_cost / len(rows), 6) if rows else 0.0,
        "latency_total_s": round(sum(lat), 1),
        "latency_mean_s": round(sum(lat) / n, 3) if n else 0.0,
        "latency_p50_s": round(lat_sorted[n // 2], 3) if n else 0.0,
        "latency_p95_s": round(lat_sorted[min(int(n * 0.95), n - 1)], 3) if n else 0.0,
    }


def _selfcheck():
    # 1M input + 1M output of flash at 0.30 / 2.50 -> 2.80 USD.
    assert abs(cost_usd("gemini-2.5-flash", 1_000_000, 1_000_000) - 2.80) < 1e-9
    assert cost_usd("unknown-model", 1_000_000, 1_000_000) == 0.0

    os.environ["NLSQL_PRICES"] = '{"unknown-model": {"input": 1.0, "output": 2.0}}'
    assert abs(cost_usd("unknown-model", 1_000_000, 0) - 1.0) < 1e-9
    del os.environ["NLSQL_PRICES"]

    rows = [
        {"prompt_tokens": 1_000_000, "output_tokens": 0, "llm_calls": 2, "latency_s": 1.0},
        {"prompt_tokens": 1_000_000, "output_tokens": 0, "llm_calls": 1, "latency_s": 3.0},
    ]
    rc = run_cost(rows, "gemini-2.5-flash")
    assert rc["prompt_tokens"] == 2_000_000 and rc["llm_calls"] == 3, rc
    assert abs(rc["est_cost_usd"] - 0.60) < 1e-9, rc  # 2M input * 0.30/1M
    assert rc["latency_mean_s"] == 2.0 and rc["latency_total_s"] == 4.0, rc
    # old run with no token/latency fields still summarizes to zeros
    assert run_cost([{"ex": 0}], "gemini-2.5-flash")["est_cost_usd"] == 0.0
    print("cost selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
