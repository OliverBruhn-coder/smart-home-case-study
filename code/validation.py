"""Validate floorplan documents without depending on Home Assistant."""

from __future__ import annotations

import json
import math
from typing import Any

MAX_PLAN_BYTES = 5 * 1024 * 1024
MAX_COUNTS = {"rooms": 500, "items": 5000, "walls": 5000}
SUPPORTED_SCHEMA_VERSIONS = {6, 7}


class InvalidPlan(ValueError):
    """A floorplan cannot safely be stored or rendered."""


def _number(value: Any, path: str, *, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidPlan(f"{path} must be a number")
    if not math.isfinite(value):
        raise InvalidPlan(f"{path} must be finite")
    if positive and value <= 0:
        raise InvalidPlan(f"{path} must be greater than zero")


def validate_plan(plan: Any) -> dict[str, Any]:
    """Return a valid JSON floorplan, preserving all additional fields."""
    if not isinstance(plan, dict):
        raise InvalidPlan("plan must be an object")

    version = plan.get("schemaVersion")
    _number(version, "schemaVersion")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise InvalidPlan("schemaVersion must be 6 or 7")

    for category, limit in MAX_COUNTS.items():
        entries = plan.get(category)
        if not isinstance(entries, list):
            raise InvalidPlan(f"{category} must be an array")
        if len(entries) > limit:
            raise InvalidPlan(f"{category} exceeds the limit of {limit}")

        seen: set[str] = set()
        for index, entry in enumerate(entries):
            path = f"{category}[{index}]"
            if not isinstance(entry, dict):
                raise InvalidPlan(f"{path} must be an object")
            identifier = entry.get("id")
            if not isinstance(identifier, str) or not identifier.strip():
                raise InvalidPlan(f"{path}.id must be a nonempty string")
            if identifier in seen:
                raise InvalidPlan(f"{category} contains duplicate id {identifier!r}")
            seen.add(identifier)

            required = ("x", "y", "w", "h") if category == "rooms" else (
                ("x", "y", "w", "d", "h") if category == "items" else ()
            )
            for field in required:
                if field not in entry:
                    raise InvalidPlan(f"{path}.{field} is required")

            for field in ("x", "y", "x1", "y1", "x2", "y2", "z", "rot", "angle"):
                if field in entry:
                    _number(entry[field], f"{path}.{field}")
            for field in ("w", "h", "d", "width", "height", "depth", "thickness", "wallThickness"):
                if field in entry:
                    if category == "rooms" and field == "wallThickness" and entry[field] is None:
                        continue
                    _number(entry[field], f"{path}.{field}", positive=True)

            if category == "rooms":
                openings = entry.get("openings", [])
                if not isinstance(openings, list):
                    raise InvalidPlan(f"{path}.openings must be an array")
                notches = entry.get("notches", {})
                if notches is None:
                    notches = {}
                if not isinstance(notches, dict):
                    raise InvalidPlan(f"{path}.notches must be an object")
                edge_thickness = entry.get("edgeThickness", {})
                if edge_thickness is None:
                    edge_thickness = {}
                if not isinstance(edge_thickness, dict):
                    raise InvalidPlan(f"{path}.edgeThickness must be an object")
                for edge, thickness in edge_thickness.items():
                    _number(thickness, f"{path}.edgeThickness.{edge}", positive=True)
                features = [
                    *((f"{path}.openings[{i}]", feature) for i, feature in enumerate(openings)),
                    *((f"{path}.notches.{key}", feature) for key, feature in notches.items()),
                ]
                for feature_path, feature in features:
                    if feature is None and feature_path.startswith(f"{path}.notches."):
                        continue
                    if not isinstance(feature, dict):
                        raise InvalidPlan(f"{feature_path} must be an object")
                    for field in ("offset", "x", "y"):
                        if field in feature:
                            _number(feature[field], f"{feature_path}.{field}")
                    for field in ("w", "h", "width", "height"):
                        if field in feature:
                            _number(feature[field], f"{feature_path}.{field}", positive=True)

    try:
        encoded = json.dumps(plan, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError, RecursionError) as err:
        raise InvalidPlan(f"plan must contain finite JSON data: {err}") from err
    if len(encoded.encode("utf-8")) > MAX_PLAN_BYTES:
        raise InvalidPlan("plan exceeds the 5 MiB limit")
    return plan
