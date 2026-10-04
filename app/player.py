"""Exclusive playback controller: replays mechanical targets to rotctld.

One playback may run at a time. Targets are sent on a monotonic clock
schedule (t_rel_s). On timeout, disconnect, RPRT error or cancel the
controller stops sending, makes a best-effort S (stop) and releases the
exclusive slot, keeping the real final state for status queries.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .rotctl import RotctlClient, RotctlError
from .tracker import TrackTarget

START_POS_TOL_DEG = 2.0  # startup actual-position check tolerance


@dataclass
class PlaybackStatus:
    state: str = "idle"  # idle|running|done|error|cancelled
    detail: str = ""
    targets_total: int = 0
    targets_sent: int = 0
    started_mono: float | None = None
    last_command: dict | None = None
    final_position: dict | None = None

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "detail": self.detail,
            "targets_total": self.targets_total,
            "targets_sent": self.targets_sent,
            "elapsed_s": (round(time.monotonic() - self.started_mono, 3)
                          if self.state == "running"
                          and self.started_mono is not None else None),
            "last_command": self.last_command,
            "final_position": self.final_position,
        }


class PlaybackController:
    def __init__(self):
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = PlaybackStatus()

    def status(self) -> PlaybackStatus:
        with self._lock:
            return PlaybackStatus(**vars(self._status))

    def start(self, targets: list[TrackTarget], host: str, port: int,
              timeout: float = 5.0) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("playback already running")
            self._cancel.clear()
            self._status = PlaybackStatus(state="running",
                                          targets_total=len(targets))
            self._thread = threading.Thread(
                target=self._run, args=(targets, host, port, timeout),
                daemon=True)
            self._thread.start()

    def cancel(self) -> bool:
        with self._lock:
            alive = self._thread and self._thread.is_alive()
        if alive:
            self._cancel.set()
        return bool(alive)

    def _finish(self, state: str, detail: str,
                client: RotctlClient | None) -> None:
        final_pos = None
        if client is not None:
            try:
                client.stop()  # best effort
            except RotctlError:
                pass
            try:
                az, el = client.get_position()
                final_pos = {"az_deg": az, "el_deg": el}
            except RotctlError:
                pass
            client.close()
        with self._lock:
            self._status.state = state
            self._status.detail = detail
            if final_pos is not None:
                self._status.final_position = final_pos

    def _run(self, targets: list[TrackTarget], host: str, port: int,
             timeout: float) -> None:
        client = None
        try:
            client = RotctlClient(host, port, timeout=timeout)
            az, el = client.get_position()
            first = targets[0]
            if (abs(az - first.az_deg) > START_POS_TOL_DEG
                    or abs(el - first.el_deg) > START_POS_TOL_DEG):
                self._finish(
                    "error",
                    f"actual position ({az:.2f}, {el:.2f}) differs from "
                    f"plan start ({first.az_deg:.2f}, {first.el_deg:.2f})",
                    client)
                return
            t0 = time.monotonic()
            with self._lock:
                self._status.started_mono = t0
            for tgt in targets:
                if self._cancel.is_set():
                    self._finish("cancelled", "cancelled by user", client)
                    return
                delay = t0 + tgt.t_rel_s - time.monotonic()
                if delay > 0:
                    self._cancel.wait(delay)
                    if self._cancel.is_set():
                        self._finish("cancelled", "cancelled by user", client)
                        return
                client.set_position(tgt.az_deg, tgt.el_deg)
                with self._lock:
                    self._status.targets_sent += 1
                    self._status.last_command = {
                        "t_rel_s": tgt.t_rel_s, "az_deg": tgt.az_deg,
                        "el_deg": tgt.el_deg, "phase": tgt.phase}
            self._finish("done", "playback completed", client)
        except RotctlError as exc:
            self._finish("error", str(exc), client)
        except Exception as exc:  # keep the slot release unconditional
            self._finish("error", f"unexpected: {exc}", client)


controller = PlaybackController()
