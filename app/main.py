"""FastAPI entrypoint: offline satellite pass forecast service."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from .delivery import build_zip, interval_csv_rows, interval_to_dict, render_csv
from .passes import HorizonMask, PropagationError, Site, find_passes
from .player import controller
from .schemas import (MAX_EPOCH_AGE, ForecastRequest, ForecastResponse,
                      IntervalOut, PlayRequest, TrackPlanRequest,
                      TrackPlanResponse, TrackTargetOut)
from .tle import TLEError, parse_tle
from .tracker import MechLimits, PlanError, plan_track

app = FastAPI(title="Offline Pass Forecast", version="1.0.0")

NOTES = [
    "Positions: SGP4/SDP4 (WGS72) in TEME, rotated to ECEF with GMST; "
    "UTC approximates UT1; no polar motion, refraction or light-time.",
    "Site coordinates are WGS84 geodetic; azimuth is clockwise from "
    "true north.",
    "Visibility requires elevation strictly above the interpolated "
    "horizon mask; tangential touches are not valid intervals.",
    "Search grid is 1 s with crossings bisected to 0.1 s; intervals "
    "shorter than ~1 s may be missed.",
    "Doppler shift: negative means the received frequency is below "
    "nominal (range increasing).",
]


def _prepare(req: ForecastRequest):
    try:
        sats = []
        for s in req.satellites:
            tle = parse_tle(s.tle_line1, s.tle_line2)
            age_start = req.window.start - tle.epoch
            age_end = req.window.end - tle.epoch
            if (not -MAX_EPOCH_AGE <= age_start <= MAX_EPOCH_AGE
                    or not -MAX_EPOCH_AGE <= age_end <= MAX_EPOCH_AGE):
                raise TLEError(
                    f"satellite {s.id}: query window end is more than 7 days "
                    f"from the TLE epoch {tle.epoch.isoformat()}")
            sats.append((s, tle))
    except TLEError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    sites = [Site(station_id=st.id, lat_deg=st.lat_deg, lon_deg=st.lon_deg,
                  alt_m=st.alt_m, mask=HorizonMask(st.mask))
             for st in req.stations]
    return sats, sites


def _compute(req: ForecastRequest):
    sats, sites = _prepare(req)
    results = []  # (sat_in, site, interval)
    try:
        for sat_in, tle in sats:
            for site in sites:
                for iv in find_passes(tle.satrec, site,
                                      req.window.start, req.window.end):
                    results.append((sat_in, tle, site, iv))
    except PropagationError as exc:
        # any propagation failure fails the whole request
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    results.sort(key=lambda r: (r[3].start, r[0].id))
    return results


@app.post("/api/passes", response_model=ForecastResponse)
def forecast(req: ForecastRequest):
    results = _compute(req)
    intervals = [IntervalOut(**interval_to_dict(s.id, st.station_id, iv))
                 for s, _t, st, iv in results]
    return ForecastResponse(window=req.window, interval_count=len(intervals),
                            intervals=intervals, notes=NOTES)


@app.post("/api/passes/download")
def forecast_download(req: ForecastRequest):
    results = _compute(req)
    summary = {
        "window": {"start": req.window.start.isoformat().replace("+00:00", "Z"),
                   "end": req.window.end.isoformat().replace("+00:00", "Z")},
        "interval_count": len(results),
        "intervals": [interval_to_dict(s.id, st.station_id, iv)
                      for s, _t, st, iv in results],
        "units": {"azimuth": "deg", "elevation": "deg", "range": "km",
                  "range_rate": "km/s", "doppler_shift": "Hz"},
        "notes": NOTES,
    }
    csv_files = {}
    for idx, (s, tle, st, iv) in enumerate(results):
        rows = interval_csv_rows(tle.satrec, st, iv, s.downlink_frequency_hz)
        csv_files[f"{s.id}_{st.station_id}_{idx:03d}.csv"] = render_csv(rows)
    payload = build_zip(summary, csv_files)
    return Response(
        content=payload, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="passes.zip"'})


@app.get("/api/health")
def health():
    return {"status": "ok"}


def _build_plan(req: TrackPlanRequest):
    results = _compute(req.forecast)
    if req.interval_index >= len(results):
        raise HTTPException(
            status_code=422,
            detail=f"interval_index {req.interval_index} out of range: "
                   f"{len(results)} interval(s) found")
    sat_in, tle, site, iv = results[req.interval_index]
    m = req.mechanics
    lim = MechLimits(az_min=m.az_min_deg, az_max=m.az_max_deg,
                     el_min=m.el_min_deg, el_max=m.el_max_deg,
                     max_az_rate=m.max_az_rate_dps,
                     max_el_rate=m.max_el_rate_dps)
    try:
        targets = plan_track(tle.satrec, site, iv, lim,
                             current=(m.current_az_deg, m.current_el_deg),
                             park=(m.park_az_deg, m.park_el_deg),
                             preset_s=m.preset_s, park_s=m.park_s)
    except PlanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return sat_in, site, iv, targets


@app.post("/api/track/plan", response_model=TrackPlanResponse)
def track_plan(req: TrackPlanRequest):
    sat_in, site, iv, targets = _build_plan(req)
    total_az = sum(abs(b.az_deg - a.az_deg)
                   for a, b in zip(targets, targets[1:]))
    return TrackPlanResponse(
        interval=IntervalOut(**interval_to_dict(sat_in.id, site.station_id, iv)),
        target_count=len(targets),
        total_az_travel_deg=round(total_az, 3),
        targets=[TrackTargetOut(t_rel_s=round(t.t_rel_s, 3),
                                az_deg=round(t.az_deg, 3),
                                el_deg=round(t.el_deg, 3), phase=t.phase)
                 for t in targets])


@app.post("/api/track/play")
def track_play(req: PlayRequest):
    _sat, _site, _iv, targets = _build_plan(req.plan)
    try:
        controller.start(targets, req.rotctld.host, req.rotctld.port,
                         timeout=req.rotctld.timeout_s)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"state": "running", "targets_total": len(targets)}


@app.get("/api/track/status")
def track_status():
    return controller.status().to_dict()


@app.post("/api/track/cancel")
def track_cancel():
    cancelled = controller.cancel()
    return {"cancelled": cancelled, "state": controller.status().state}
