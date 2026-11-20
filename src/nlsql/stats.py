"""Small pure-Python stats for paired EX comparisons and judge agreement (no scipy).

  mcnemar_exact(b, c)                 -> two-sided exact McNemar p (binomial on discordant pairs)
  paired_bootstrap_ci(a, b, ...)      -> 95% CI for mean(b - a), in percentage points
  cohen_kappa(tp, fn, fp, tn)         -> Cohen's kappa for a 2x2 agreement table
"""
import random
from math import comb


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar: P(X <= min(b,c)) * 2 under Binomial(b+c, 0.5), capped at 1."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def paired_bootstrap_ci(
    a: list[int], b: list[int], n_boot: int = 10000, seed: int = 0, alpha: float = 0.05
) -> tuple[float, float]:
    """Percentile bootstrap CI for the paired EX difference mean(b - a), in pp. Fixed seed."""
    assert len(a) == len(b) and a, "paired samples must be non-empty and equal length"
    d = [y - x for x, y in zip(a, b)]
    n = len(d)
    rng = random.Random(seed)
    means = sorted(sum(d[rng.randrange(n)] for _ in range(n)) / n * 100 for _ in range(n_boot))
    lo = means[int(alpha / 2 * n_boot)]
    hi = means[min(int((1 - alpha / 2) * n_boot), n_boot - 1)]
    return round(lo, 2), round(hi, 2)


def cohen_kappa(tp: int, fn: int, fp: int, tn: int) -> float:
    """Kappa for rater A (rows: yes=tp+fn) vs rater B (cols: yes=tp+fp)."""
    n = tp + fn + fp + tn
    if n == 0:
        return 0.0
    po = (tp + tn) / n
    pe = ((tp + fn) * (tp + fp) + (fp + tn) * (fn + tn)) / n ** 2
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def _selfcheck():
    assert round(mcnemar_exact(10, 2), 4) == 0.0386, mcnemar_exact(10, 2)
    assert mcnemar_exact(2, 10) == mcnemar_exact(10, 2)
    assert mcnemar_exact(0, 0) == 1.0 and mcnemar_exact(5, 5) == 1.0
    assert round(mcnemar_exact(0, 6), 4) == 0.0312  # 2 / 64
    a, b = [1, 0, 1, 0] * 25, [1, 1, 1, 0] * 25
    ci = paired_bootstrap_ci(a, b, seed=1)
    assert ci == paired_bootstrap_ci(a, b, seed=1) and ci[0] <= 25.0 <= ci[1], ci
    assert paired_bootstrap_ci([1] * 10, [1] * 10) == (0.0, 0.0)
    assert abs(cohen_kappa(40, 10, 5, 45) - 0.7) < 1e-9
    assert cohen_kappa(50, 0, 0, 50) == 1.0
    print("stats selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
