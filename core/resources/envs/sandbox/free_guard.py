"""Fail-closed admission of measured free capacity and runtime."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class FreeMeasurement:
    machines: int
    free_machines: int
    cost: float
    remaining_min: float
    run_max_min: float
    elapsed_min: float
    max_concurrent: int = 1


def check_free(measurement: FreeMeasurement | None, requested_min: float) -> str | None:
    if not isinstance(measurement, FreeMeasurement):
        return "free_unverified"
    numbers = (measurement.cost, measurement.remaining_min, measurement.run_max_min,
               measurement.elapsed_min, requested_min)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in numbers):
        return "free_unverified"
    if any(type(v) is not int for v in (measurement.machines, measurement.free_machines, measurement.max_concurrent)):
        return "free_unverified"
    if measurement.machines == 0:
        return "no_machine"
    if measurement.machines > 1:
        return "machines_over_limit"
    if measurement.machines != 1 or measurement.free_machines != 1 or measurement.max_concurrent != 1:
        return "not_free"
    if measurement.cost > 0:
        return "cost_nonzero"
    if requested_min <= 0:
        return "free_unverified"
    if requested_min > measurement.remaining_min:
        return "quota"
    if measurement.elapsed_min + requested_min > measurement.run_max_min:
        return "run_budget_exhausted"
    return None
