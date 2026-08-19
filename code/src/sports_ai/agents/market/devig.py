"""M02: devig methods - proportional (multiplicative), power, and Shin.

All take decimal odds for a full market (all selections) and return fair
probabilities summing to 1. Shin's method corrects for favourite-longshot bias.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DevigResult:
    method: str
    implied_probabilities: list[float]
    booksum: float
    probabilities: list[float]
    converged: bool
    iterations: int
    tolerance: float
    parameter_name: str | None = None
    parameter_value: float | None = None


def implied(odds: list[float]) -> list[float]:
    return [1.0 / odd for odd in odds]


def overround(odds: list[float]) -> float:
    return sum(implied(odds)) - 1.0


def compute_devig_proportional(odds: list[float]) -> DevigResult:
    imp = implied(odds)
    total = sum(imp)
    return DevigResult(
        method="proportional",
        implied_probabilities=imp,
        booksum=total,
        probabilities=[prob / total for prob in imp],
        converged=True,
        iterations=1,
        tolerance=0.0,
    )


def devig_proportional(odds: list[float]) -> list[float]:
    return compute_devig_proportional(odds).probabilities


def compute_devig_power(odds: list[float], tol: float = 1e-10) -> DevigResult:
    """Find k such that sum((1/o)^k) == 1 using bisection."""

    imp = implied(odds)
    lo, hi = 0.5, 3.0
    k = 1.0
    converged = False
    iterations = 0
    total = 0.0
    for iteration in range(1, 201):
        iterations = iteration
        k = (lo + hi) / 2
        total = sum(prob**k for prob in imp)
        if abs(total - 1.0) < tol:
            converged = True
            break
        if total > 1.0:
            lo = k
        else:
            hi = k
    probs = [prob**k for prob in imp]
    total_probs = sum(probs)
    return DevigResult(
        method="power",
        implied_probabilities=imp,
        booksum=sum(imp),
        probabilities=[prob / total_probs for prob in probs],
        converged=converged,
        iterations=iterations,
        tolerance=tol,
        parameter_name="k",
        parameter_value=k,
    )


def devig_power(odds: list[float], tol: float = 1e-10) -> list[float]:
    return compute_devig_power(odds, tol=tol).probabilities


def compute_devig_shin(odds: list[float], tol: float = 1e-12) -> DevigResult:
    """Shin (1992/1993) devig with favourite-longshot correction."""

    pi = implied(odds)
    total = sum(pi)

    def probs_given_z(z: float) -> list[float]:
        return [
            ((((z**2 + 4 * (1 - z) * (p**2) / total) ** 0.5) - z) / (2 * (1 - z)))
            for p in pi
        ]

    lo, hi = 0.0, 0.5
    z = 0.0
    converged = False
    iterations = 0
    for iteration in range(1, 201):
        iterations = iteration
        z = (lo + hi) / 2
        summed = sum(probs_given_z(z))
        if abs(summed - 1.0) < tol:
            converged = True
            break
        if summed > 1.0:
            lo = z
        else:
            hi = z
    probs = probs_given_z(z)
    summed = sum(probs)
    return DevigResult(
        method="shin",
        implied_probabilities=pi,
        booksum=total,
        probabilities=[prob / summed for prob in probs],
        converged=converged,
        iterations=iterations,
        tolerance=tol,
        parameter_name="z",
        parameter_value=z,
    )


def devig_shin(odds: list[float], tol: float = 1e-12) -> list[float]:
    return compute_devig_shin(odds, tol=tol).probabilities


def consensus(
    books: dict[str, list[float]],
    tiers: dict[str, str],
    method=devig_shin,
) -> list[float]:
    """Weighted consensus across books: devig per book, THEN aggregate."""

    weights = {"sharp": 3.0, "exchange": 2.5, "soft": 1.0}
    acc: list[float] | None = None
    weight_sum = 0.0

    for book, odds in books.items():
        weight = weights.get(tiers.get(book, "soft"), 1.0)
        probs = method(odds)
        if acc is None:
            acc = [0.0] * len(probs)
        for idx, prob in enumerate(probs):
            acc[idx] += weight * prob
        weight_sum += weight

    if acc is None:
        raise ValueError("no books")

    return [value / weight_sum for value in acc]
