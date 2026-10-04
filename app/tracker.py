"""Dual-axis antenna tracker path planning.

Given a forecast pass interval and mechanical limits, build a full
mechanical target sequence (preset -> track -> park) on a 1 s grid that
includes both interval endpoints. Azimuth may be unwrapped by +360*k to
fit the mechanical range; no overhead flip is performed. The unwrapped
sequence minimising total azimuth travel is chosen; ties are broken by
lexicographic order of the mechanical azimuth sequence. If any segment
violates position or rate limits the whole interval is rejected.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .passes import PassInterval, Site, look_angles

MAX_TRACK_DURATION_S = 1800.0  # single segment must not exceed 30 min
RATE_EPS = 1e-9
# numerical slack for limit checks: interval endpoints come from a 0.1 s
# bisection, so elevation may undershoot the horizon by a few 1e-3 deg
LIMIT_TOL_DEG = 0.1


class PlanError(ValueError):
    """Raised when no feasible mechanical path exists."""


@dataclass
class MechLimits:
    az_min: float
    az_max: float
    el_min: float
    el_max: float
    max_az_rate: float  # deg/s
    max_el_rate: float  # deg/s

    def az_ok(self, az: float) -> bool:
        return self.az_min - LIMIT_TOL_DEG <= az <= self.az_max + LIMIT_TOL_DEG

    def el_ok(self, el: float) -> bool:
        return self.el_min - LIMIT_TOL_DEG <= el <= self.el_max + LIMIT_TOL_DEG


@dataclass
class TrackTarget:
    t_rel_s: float   # seconds relative to playback start (t=0)
    az_deg: float    # mechanical azimuth (may exceed [0, 360))
    el_deg: float
    phase: str       # "preset" | "track" | "park"


def _az_candidates(az: float, lim: MechLimits) -> list[float]:
    """All az + 360*k values inside the mechanical azimuth range."""
    base = az % 360.0
    k_lo = math.ceil((lim.az_min - base) / 360.0 - 1e-9)
    k_hi = math.floor((lim.az_max - base) / 360.0 + 1e-9)
    return [base + 360.0 * k for k in range(k_lo, k_hi + 1)]


def _unwrap_azimuth(azs: list[float], lim: MechLimits,
                    az_start: float, az_end: float) -> list[float]:
    """Pick az+360k per sample minimising total travel (incl. endpoints).

    Ties broken by lexicographic order of the mechanical az sequence.
    """
    cand = [_az_candidates(a, lim) for a in azs]
    for i, c in enumerate(cand):
        if not c:
            raise PlanError(
                f"sample {i}: azimuth {azs[i]:.3f} deg cannot be reached "
                f"within mechanical range [{lim.az_min}, {lim.az_max}]")
    prev: list[tuple[float, tuple[float, ...]]] = []
    for c in cand[0]:
        prev.append((abs(c - az_start), (c,)))
    for i in range(1, len(cand)):
        cur = []
        for c in cand[i]:
            best = None
            for cost, seq in prev:
                total = (cost + abs(c - seq[-1]), seq + (c,))
                if best is None or total < best:
                    best = total
            cur.append(best)
        prev = cur
    best = None
    for cost, seq in prev:
        total = (cost + abs(az_end - seq[-1]), seq)
        if best is None or total < best:
            best = total
    return list(best[1])


def _check_rate(daz: float, del_: float, dt: float, lim: MechLimits,
                where: str) -> None:
    if dt <= 0.0:
        raise PlanError(f"{where}: non-positive time step")
    if abs(daz) > lim.max_az_rate * dt + RATE_EPS:
        raise PlanError(
            f"{where}: azimuth rate {abs(daz) / dt:.3f} deg/s exceeds "
            f"limit {lim.max_az_rate} deg/s")
    if abs(del_) > lim.max_el_rate * dt + RATE_EPS:
        raise PlanError(
            f"{where}: elevation rate {abs(del_) / dt:.3f} deg/s exceeds "
            f"limit {lim.max_el_rate} deg/s")


def plan_track(satrec, site: Site, interval: PassInterval, lim: MechLimits,
               current: tuple[float, float], park: tuple[float, float],
               preset_s: float, park_s: float) -> list[TrackTarget]:
    """Full mechanical plan: preset ramp, 1 s tracking, park ramp.

    t_rel_s = 0 at playback start; the first track sample lands at
    t_rel_s == preset_s (the interval start).
    """
    duration = (interval.end - interval.start).total_seconds()
    if duration > MAX_TRACK_DURATION_S + 1e-9:
        raise PlanError(
            f"interval duration {duration:.1f} s exceeds "
            f"{MAX_TRACK_DURATION_S:.0f} s limit")
    if not lim.az_ok(current[0]) or not lim.el_ok(current[1]):
        raise PlanError("current position outside mechanical limits")
    if not lim.az_ok(park[0]) or not lim.el_ok(park[1]):
        raise PlanError("park position outside mechanical limits")

    # 1 s grid including both interval endpoints
    times: list[datetime] = []
    t = interval.start
    while t < interval.end:
        times.append(t)
        t += timedelta(seconds=1)
    times.append(interval.end)

    looks = [look_angles(satrec, site, t) for t in times]
    for i, la in enumerate(looks):
        if not lim.el_ok(la.el_deg):
            raise PlanError(
                f"sample {i}: elevation {la.el_deg:.3f} deg outside "
                f"[{lim.el_min}, {lim.el_max}]")
    mech_az = _unwrap_azimuth([la.az_deg for la in looks], lim,
                              current[0], park[0])
    mech_el = [la.el_deg for la in looks]

    # rate check along the track (dt may be fractional at the tail)
    for i in range(len(times) - 1):
        dt = (times[i + 1] - times[i]).total_seconds()
        _check_rate(mech_az[i + 1] - mech_az[i],
                    mech_el[i + 1] - mech_el[i], dt, lim,
                    f"track sample {i}->{i + 1}")
    _check_rate(mech_az[0] - current[0], mech_el[0] - current[1],
                preset_s, lim, "preset")
    _check_rate(park[0] - mech_az[-1], park[1] - mech_el[-1],
                park_s, lim, "park")

    targets: list[TrackTarget] = []
    # preset ramp: linear from current to first track target
    n_pre = max(1, int(preset_s))
    for k in range(n_pre):
        frac = k / preset_s
        targets.append(TrackTarget(
            t_rel_s=float(k),
            az_deg=current[0] + frac * (mech_az[0] - current[0]),
            el_deg=current[1] + frac * (mech_el[0] - current[1]),
            phase="preset"))
    # track samples
    for i, t in enumerate(times):
        t_rel = preset_s + (t - interval.start).total_seconds()
        targets.append(TrackTarget(t_rel_s=t_rel, az_deg=mech_az[i],
                                   el_deg=mech_el[i], phase="track"))
    # park ramp
    t0 = targets[-1].t_rel_s
    n_park = max(1, int(park_s))
    for k in range(1, n_park + 1):
        frac = k / park_s
        targets.append(TrackTarget(
            t_rel_s=t0 + float(k),
            az_deg=mech_az[-1] + frac * (park[0] - mech_az[-1]),
            el_deg=mech_el[-1] + frac * (park[1] - mech_el[-1]),
            phase="park"))
    return targets
