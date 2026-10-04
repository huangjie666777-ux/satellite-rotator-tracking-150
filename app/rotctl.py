"""Minimal rotctld TCP client: newline framing, P/p/S, RPRT errors."""
from __future__ import annotations

import socket


class RotctlError(RuntimeError):
    """Connection, timeout or RPRT error from the rotctld endpoint."""


class RotctlClient:
    def __init__(self, host: str, port: int, timeout: float = 5.0):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._buf = b""

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _readline(self) -> str:
        while b"\n" not in self._buf:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout as exc:
                raise RotctlError("timeout waiting for rotctld reply") from exc
            except OSError as exc:
                raise RotctlError(f"rotctld connection error: {exc}") from exc
            if not chunk:
                raise RotctlError("rotctld closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("ascii", "replace").strip()

    def _send(self, cmd: str) -> None:
        try:
            self._sock.sendall(cmd.encode("ascii") + b"\n")
        except OSError as exc:
            raise RotctlError(f"rotctld send failed: {exc}") from exc

    def _check_rprt(self) -> None:
        reply = self._readline()
        if reply.startswith("RPRT"):
            try:
                code = int(reply.split()[1])
            except (IndexError, ValueError) as exc:
                raise RotctlError(f"malformed rotctld reply: {reply!r}") from exc
            if code != 0:
                raise RotctlError(f"rotctld reported error RPRT {code}")
        else:
            raise RotctlError(f"unexpected rotctld reply: {reply!r}")

    def set_position(self, az_deg: float, el_deg: float) -> None:
        self._send(f"P {az_deg:.3f} {el_deg:.3f}")
        self._check_rprt()

    def get_position(self) -> tuple[float, float]:
        self._send("p")
        try:
            az = float(self._readline())
            el = float(self._readline())
        except ValueError as exc:
            raise RotctlError("malformed position reply from rotctld") from exc
        return az, el

    def stop(self) -> None:
        self._send("S")
        self._check_rprt()
