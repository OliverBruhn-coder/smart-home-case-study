"""What a dishwasher run costs depending on when it starts.

Prices are the day-ahead spot price plus the grid company's time-of-day
tariff and the charges that are the same every hour (taxes, the supplier's
markup), with VAT: what a kWh costs on the bill, subscriptions aside.

Pure Python, so it can be tested without Home Assistant. Times are unix seconds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

VAT = 1.25
SLOT_S = 900            # the spot price is set per quarter of an hour
HORIZON_S = 24 * 3600   # how far ahead a start is worth suggesting
SMALL_KR = 0.25         # a saving below this is not worth waiting for
AVOID_KR = 0.50         # ... and above this, an expensive hour is worth avoiding

NOW, WAIT, AVOID, NO_PRICES = "Tænd nu", "Kan vente", "Undgå nu", "Ingen priser"


def price_slots(spot: list[dict[str, Any]], tariff: list[float] | None,
                fixed: float = 0.0) -> list[list[float]]:
    """[[start, kr/kWh incl. VAT], ...] sorted, from Energi Data Service
    DayAheadPrices records and the grid tariff per local hour (kr/kWh).
    `fixed` is what is added every hour, before VAT: taxes and the supplier's markup."""
    slots = {}
    for record in spot:
        price = record.get("DayAheadPriceDKK")
        if price is None:
            continue
        start = datetime.fromisoformat(record["TimeUTC"] + "+00:00").timestamp()
        hour = datetime.fromisoformat(record["TimeDK"]).hour
        grid = tariff[hour] if tariff else 0.0
        slots[start] = round((price / 1000 + grid + fixed) * VAT, 4)
    return [[t, p] for t, p in sorted(slots.items())]


def current_tariff(records: list[dict[str, Any]], now_local: str) -> list[float] | None:
    """The 24 hourly prices of the tariff record valid at `now_local` (ISO)."""
    valid = [r for r in records if r["ValidFrom"] <= now_local and (r.get("ValidTo") or "9999") > now_local]
    if not valid:
        return None
    record = max(valid, key=lambda r: r["ValidFrom"])
    first = record.get("Price1") or 0.0
    return [record.get(f"Price{i}") if record.get(f"Price{i}") is not None else first for i in range(1, 25)]


def price_at(slots: list[list[float]], t: float) -> float | None:
    for start, price in slots:
        if start <= t < start + SLOT_S:
            return price
    return None


def typical_run(history: list[dict[str, Any]], fallback: dict[str, Any], unknown: str = "Ukendt") -> dict[str, Any]:
    """The run to price: the latest one of the most used program, or
    `fallback` (the programme table) until the machine has runs of its own."""
    known = [c for c in history if c.get("label") and c["label"] != unknown and c["kwh"] > 0]
    if not known:
        return fallback
    labels = [c["label"] for c in known]
    label = max(set(labels), key=lambda name: (labels.count(name), max(i for i, x in enumerate(labels) if x == name)))
    return next(c for c in reversed(known) if c["label"] == label)


def _used(curve: list[list[float]], total: float, duration: float, offset: float) -> float:
    """kWh used `offset` seconds into the run."""
    points = [[0, 0.0], *[p for p in curve if 0 < p[0] < duration], [duration, total]]
    if offset <= 0:
        return 0.0
    for (t0, e0), (t1, e1) in zip(points, points[1:]):
        if offset <= t1:
            return e0 + (min(e1, total) - e0) * (offset - t0) / max(t1 - t0, 1)
    return total


def run_cost(slots: list[list[float]], start: float, run: dict[str, Any]) -> float | None:
    """Kr for the run if it starts at `start`; None if prices do not cover it."""
    end = start + run["duration"]
    cost, covered = 0.0, start
    for slot, price in slots:
        if slot + SLOT_S <= start or slot >= end:
            continue
        if slot > covered + 1:
            return None
        a, b = max(slot, start), min(slot + SLOT_S, end)
        cost += price * (_used(run["curve"], run["kwh"], run["duration"], b - start)
                         - _used(run["curve"], run["kwh"], run["duration"], a - start))
        covered = b
    return cost if covered >= end - 1 else None


def advice(slots: list[list[float]], now: float, run: dict[str, Any]) -> dict[str, Any]:
    """Start now, or when? Starts are tried every quarter within the horizon."""
    now_cost = run_cost(slots, now, run)
    starts = [now] + [t for t, _ in slots if now < t <= now + HORIZON_S]
    costs = [(c, t) for t in starts if (c := run_cost(slots, t, run)) is not None]
    if now_cost is None or not costs:
        return {"verdict": NO_PRICES}
    best_cost, best = min(costs)
    worst_cost = max(costs)[0]
    saving = now_cost - best_cost
    spread = worst_cost - best_cost
    rank = saving / spread if spread > 0 else 0.0
    if saving < SMALL_KR or rank < 0.2:
        verdict = NOW
    elif saving >= AVOID_KR and rank > 0.66:
        verdict = AVOID
    else:
        verdict = WAIT
    # The cheapest start can be most of a day away; the first start that is
    # nearly as cheap is the one to act on.
    good_cost, good = next((c, t) for c, t in sorted(costs, key=lambda row: row[1])
                           if c <= best_cost + SMALL_KR)
    if verdict == NOW:
        good_cost, good = now_cost, now
    return {"verdict": verdict, "program": run["label"], "now_cost": round(now_cost, 2),
            "good_start": good, "good_cost": round(good_cost, 2),
            "best_start": best, "best_cost": round(best_cost, 2),
            "worst_cost": round(worst_cost, 2), "saving": round(saving, 2),
            "until": max(t for _, t in costs) + run["duration"]}
