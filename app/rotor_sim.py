"""Local rotctld-compatible TCP rotor simulator for integration tests.

Speaks the rotctld subset used by app.rotctl, newline-framed:
P <az> <el> -> RPRT 0 (set position; rotor slews toward target),
p -> az/el reply lines, S -> RPRT 0 (stop, hold position).
Unknown commands get RPRT -1. Run:
.venv/bin/python -m app.rotor_sim --port 4533 --az 180 --el 10
"""
from __future__ import annotations

import argparse
import socketserver
import threading
import time


class RotorState:
    def __init__(self, az: float, el: float, rate_dps: float = 30.0):
        self.az = az
        self.el = el
        self.target_az = az
        self.target_el = el
        self.rate = rate_dps
        self.moving = True
        self.lock = threading.Lock()
        self._last = time.monotonic()

    def _advance(self) -> None:
        now = time.monotonic()
        dt = now - self._last
        self._last = now
        if self.moving:
            for axis in ("az", "el"):
                cur = getattr(self, axis)
                tgt = getattr(self, f"target_{axis}")
                step = self.rate * dt
                if abs(tgt - cur) <= step:
                    setattr(self, axis, tgt)
                else:
                    setattr(self, axis, cur + step * (1 if tgt > cur else -1))

    def handle(self, line: str) -> str:
        parts = line.split()
        if not parts:
            return "RPRT -1\n"
        cmd = parts[0]
        with self.lock:
            self._advance()
            if cmd == "P" and len(parts) == 3:
                try:
                    self.target_az = float(parts[1])
                    self.target_el = float(parts[2])
                except ValueError:
                    return "RPRT -1\n"
                self.moving = True
                return "RPRT 0\n"
            if cmd == "p":
                return f"{self.az:.3f}\n{self.el:.3f}\n"
            if cmd == "S":
                self.moving = False
                return "RPRT 0\n"
            return "RPRT -1\n"


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        for raw in self.rfile:
            line = raw.decode("ascii", "replace").strip()
            self.wfile.write(self.server.rotor.handle(line).encode("ascii"))


def main() -> None:
    ap = argparse.ArgumentParser(description="rotctld TCP simulator")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4533)
    ap.add_argument("--az", type=float, default=180.0)
    ap.add_argument("--el", type=float, default=10.0)
    ap.add_argument("--rate", type=float, default=30.0,
                    help="slew rate, deg/s")
    args = ap.parse_args()
    rotor = RotorState(args.az, args.el, rate_dps=args.rate)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server((args.host, args.port), _Handler) as srv:
        srv.rotor = rotor
        print(f"rotor simulator on {args.host}:{args.port} "
              f"(az={args.az} el={args.el})", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
