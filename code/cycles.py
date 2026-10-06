"""Dishwasher runs, read from the power and energy of the plug it is in.

The plug (Tuya, through the cloud) drops messages now and then: on
2026-10-01 a whole heating phase never showed up as power, and on 2026-09-24
the power stayed at 1965 W for three days after the machine had stopped. So
the plug's energy meter is a second witness, both for a run that starts
without a power reading and for a power reading that has gone stale.

Which program is running cannot be read from the plug. It is guessed by
comparing the run so far with earlier runs (and, until there are some, with
the programs' rough figures). A program chosen by hand during or shortly
after a run is remembered as certain, so the guesses improve with use.

Pure Python, so it can be tested on recorded data without Home Assistant.
Times are unix seconds.
"""

from __future__ import annotations

from statistics import median
from typing import Any

RUN_W = 5.0          # standby is ~0.4 W; the machine's own pauses read 0 W
HEAT_W = 1000.0      # the heater draws ~1950 W
QUIET_S = 5 * 60     # this long at or under RUN_W ends a run (pauses seen: < 1 min)
STALE_S = 45 * 60    # the meter reports every 10-30 min while the machine runs
START_KWH = 0.02     # a meter rise this big starts a run without a power reading
LATE_S = 45 * 60     # a meter reading this soon after a run still belongs to it
MIN_KWH = 0.005      # a run that used less was a stale or stray reading
FINISHED_S = 3 * 3600  # how long "Færdig" is shown, and a program can be set after
MAX_CYCLES = 200

READY, RUNNING, FINISHED = "Klar", "Kører", "Færdig"
UNKNOWN = "Ukendt"

# Hisense HBU642 programs. Rough minutes and kWh from the manuals of its
# sister models; they are only used until the machine's own runs are known.
PROGRAMS = {
    "Eco": (220, 0.85),
    "Auto": (140, 1.0),
    "Intensiv": (160, 1.5),
    "Hygiejne": (170, 1.6),
    "Glas": (110, 0.9),
    "Hurtig": (30, 0.5),
    "Forvask": (15, 0.02),
}


def _kwh_at(curve: list[list[float]], offset: float) -> float:
    """Energy used `offset` seconds into a run, interpolated on its curve."""
    if not curve or offset <= curve[0][0]:
        return 0.0 if not curve or offset <= 0 else curve[0][1] * offset / max(curve[0][0], 1)
    for (t0, e0), (t1, e1) in zip(curve, curve[1:]):
        if offset <= t1:
            return e0 + (e1 - e0) * (offset - t0) / max(t1 - t0, 1)
    return curve[-1][1]


def _run_kwh(run: dict[str, Any]) -> float:
    return run["curve"][-1][1] if run["curve"] else 0.0


def _seed_cycles() -> list[dict[str, Any]]:
    return [{"label": name, "confirmed": False, "seed": True, "duration": minutes * 60,
             "kwh": kwh, "curve": [[0, 0.0], [minutes * 60, kwh]], "heat": []}
            for name, (minutes, kwh) in PROGRAMS.items()]


def _distance(run: dict[str, Any], elapsed: float, kwh: float, cycle: dict[str, Any]) -> float:
    """How unlike a past cycle the run is after `elapsed` seconds."""
    d = abs(kwh - _kwh_at(cycle["curve"], elapsed)) / 0.1
    first = run["heat"][0][0] if run["heat"] else None
    other = cycle["heat"][0][0] if cycle["heat"] else None
    if not cycle.get("seed"):
        if first is not None and other is not None:
            d += abs(first - other) / 300
        elif (first is not None) != (other is not None and other <= elapsed):
            d += 1.0
    if cycle.get("seed"):
        d += 1.0  # the machine's own runs beat the manual's figures
    elif cycle.get("confirmed"):
        d *= 0.8
    return d


def classify(cycle: dict[str, Any], history: list[dict[str, Any]]) -> str:
    """Nearest program for a finished cycle, by duration and energy."""
    best, best_d = UNKNOWN, None
    for other in history + _seed_cycles():
        if not other.get("label") or other.get("label") == UNKNOWN:
            continue
        d = abs(cycle["duration"] - other["duration"]) / 600 + abs(cycle["kwh"] - other["kwh"]) / 0.15
        d += 1.0 if other.get("seed") else (0 if other.get("confirmed") else 0.3)
        if best_d is None or d < best_d:
            best, best_d = other["label"], d
    return best


class DishwasherCycles:
    """State of one dishwasher: the run in progress and the finished ones."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        self.run: dict[str, Any] | None = data.get("run")
        self.history: list[dict[str, Any]] = data.get("history", [])
        self.meter: list[float] | None = data.get("meter")  # [t, kWh] last reading
        self.watts: float | None = None
        self.aborted: float | None = data.get("aborted")  # when a run was cut off

    def as_dict(self) -> dict[str, Any]:
        return {"run": self.run, "history": self.history, "meter": self.meter,
                "aborted": self.aborted}

    @property
    def last(self) -> dict[str, Any] | None:
        return self.history[-1] if self.history else None

    def state(self, t: float) -> str:
        if self.run:
            return RUNNING
        if self.last and t - self.last["end"] < FINISHED_S:
            return FINISHED
        return READY

    # --- readings -------------------------------------------------------

    def _start(self, t: float) -> None:
        kwh0 = self.meter[1] if self.meter else None
        self.run = {"start": t, "kwh0": kwh0, "active": t, "power_at": t, "kwh_at": t,
                    "heat": [], "curve": [], "peak": 0.0, "label": None}

    def power(self, t: float, watts: float | None) -> bool:
        """A new power reading; returns True if anything changed."""
        if watts is None or watts == self.watts:
            return False
        self.watts = watts
        run = self.run
        if run is None:
            if watts <= RUN_W:
                return False
            self._start(t)
            run = self.run
        run["power_at"] = t
        if watts > RUN_W:
            run["active"] = t
        run["peak"] = max(run["peak"], watts)
        heat = run["heat"]
        heating = bool(heat) and heat[-1][1] is None
        if watts >= HEAT_W and not heating:
            heat.append([t - run["start"], None])
        elif watts < HEAT_W and heating:
            heat[-1][1] = t - run["start"]
        return True

    def energy(self, t: float, kwh: float | None) -> bool:
        """A new reading of the appliance's (never decreasing) kWh total."""
        if kwh is None:
            return False
        previous, self.meter = self.meter, [t, kwh]
        if previous is None:
            if self.run and self.run["kwh0"] is None:
                self.run["kwh0"] = kwh
            return True
        rise = kwh - previous[1]
        if rise <= 0:
            return False
        if self.run is None:
            last = self.last
            if last and t - last["end"] < LATE_S:
                last["kwh"] = round(last["kwh"] + rise, 4)  # the run's last minutes
            elif self.aborted is not None and t - self.aborted < LATE_S:
                pass  # what the cut-off run used, reported late
            elif rise >= START_KWH:
                # No power reading came; the run began after the previous reading.
                self._start(max(previous[0], t - 15 * 60))
                self.run["kwh0"] = previous[1]
                self.run["kwh_at"] = t
                self.run["curve"].append([t - self.run["start"], round(rise, 4)])
            return True
        run = self.run
        if run["kwh0"] is None:
            run["kwh0"] = previous[1]
        run["kwh_at"] = t
        run["active"] = max(run["active"], t)
        run["curve"].append([t - run["start"], round(kwh - run["kwh0"], 4)])
        return True

    def run_kwh(self) -> float:
        return _run_kwh(self.run) if self.run else 0.0

    # --- time -----------------------------------------------------------

    def tick(self, t: float) -> dict[str, Any] | None:
        """Ends the run when it has gone quiet; returns the finished cycle."""
        run = self.run
        if run is None:
            return None
        if self.watts is not None and self.watts <= RUN_W and t - run["active"] >= QUIET_S:
            return self._finish(run["active"], stale=False)
        # Neither the power reading nor the meter has moved for longer than
        # the meter ever stays still while the machine runs: power is stale.
        if t - max(run["power_at"], run["kwh_at"]) >= STALE_S:
            return self._finish(run["active"], stale=True)
        return None

    def _finish(self, end: float, stale: bool) -> dict[str, Any] | None:
        run, self.run = self.run, None
        end = max(end, run["start"])
        if stale:
            self.watts = None  # the next real reading starts afresh
        kwh = round(_run_kwh(run), 4)
        if kwh < MIN_KWH and (stale or end - run["start"] < 120):
            return None
        heat = [[s, e if e is not None else end - run["start"]] for s, e in run["heat"]]
        cycle = {"start": run["start"], "end": end, "duration": round(end - run["start"]),
                 "kwh": kwh, "heat": heat, "curve": run["curve"], "peak": run["peak"],
                 "label": run["label"], "confirmed": run["label"] is not None}
        if cycle["label"] is None:
            # A run that ended on a stale reading has no reliable length to learn from.
            cycle["label"] = UNKNOWN if stale else classify(cycle, self.history)
        self.history = (self.history + [cycle])[-MAX_CYCLES:]
        return cycle

    def abort(self, t: float) -> bool:
        """The plug was switched off mid-run: not a run to learn from or announce."""
        if self.run is None:
            return False
        self.run, self.watts, self.aborted = None, None, t
        return True

    # --- program --------------------------------------------------------

    def estimate(self, t: float) -> dict[str, Any] | None:
        """Program guess and expected end for the run in progress."""
        run = self.run
        if run is None:
            return None
        elapsed = t - run["start"]
        kwh = self.run_kwh()
        known = [c for c in self.history if c.get("label") and c["label"] != UNKNOWN]
        candidates = [c for c in known + _seed_cycles() if c["duration"] >= elapsed - 300]
        if run["label"]:
            label, basis = run["label"], "valgt"
        elif candidates:
            best = min(candidates, key=lambda c: _distance(run, elapsed, kwh, c))
            label, basis = best["label"], "programtabel" if best.get("seed") else "tidligere kørsler"
        else:
            label, basis = UNKNOWN, None
        durations = [c["duration"] for c in known if c["label"] == label and c["duration"] >= elapsed - 300]
        if not durations and label in PROGRAMS:
            durations = [PROGRAMS[label][0] * 60]
            basis = basis if basis == "valgt" else "programtabel"
        end = run["start"] + median(durations) if durations else None
        if end is not None:
            end = max(end, t + 120)  # running longer than usual: "a couple of minutes"
        return {"program": label, "end": end, "basis": basis,
                "phase": "Opvarmer" if (self.watts or 0) >= HEAT_W else
                         "Vasker" if (self.watts or 0) > RUN_W else "Pause"}

    def program(self, t: float) -> str | None:
        """The program to show: the run's, or the last run's while it is shown."""
        if self.run:
            return (self.estimate(t) or {}).get("program")
        if self.last and t - self.last["end"] < FINISHED_S:
            return self.last.get("label")
        return None

    def set_program(self, t: float, label: str) -> bool:
        """Program chosen by hand for the run in progress or the one just finished."""
        label = None if label == UNKNOWN else label
        if self.run:
            self.run["label"] = label
            return True
        if self.last and t - self.last["end"] < FINISHED_S:
            self.last["label"] = label or classify(self.last, self.history[:-1])
            self.last["confirmed"] = label is not None
            return True
        return False
