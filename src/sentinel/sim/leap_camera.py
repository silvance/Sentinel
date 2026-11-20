"""A camera whose time labels leap ahead a few seconds after it is connected to (development and tests only).

Field report 2026-11-20 (KTNC KNC-HMSS347): about 7 s after every new connection the timestamps of the stream
leap forward once, by up to 13 hours, and run on smoothly from there, with no picture missing. The cause is in
the camera's RTCP sender reports, which FFmpeg re-bases the timestamps on; see
``sentinel.recorder.timebase.StreamRelabel``.

This source reproduces what reaches the recorder, not the RTSP exchange behind it: for every client it starts
an encoder of its own (a test pattern, in real time) whose packets get ``leap_s`` added to their timestamps
from ``leap_after_s`` on, and sends them as NUT over TCP. NUT, because FFmpeg passes such a leap through
unchanged from a NUT input as it does from RTSP, whereas it smooths it out of MPEG-TS (the other simulator's
format), which is a different case: time the stream lost.

The recorder connects with ``url = "tcp://127.0.0.1:<port>"``, ``input_format = "nut"`` and
``development_source = true``.

CLI: ``python -m sentinel.sim.leap_camera --port 18555 --leap-after 7 --leap 11984``
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import threading
import time


class LeapingCamera:
    def __init__(self, port: int = 0, host: str = "127.0.0.1", size: str = "320x240", fps: int = 15,
                 leap_after_s: float = 5.0, leap_s: float = 3600.0, ffmpeg: str = "ffmpeg") -> None:
        self.host, self.port = host, port
        self.size, self.fps, self.ffmpeg = size, fps, ffmpeg
        self.leap_after_s, self.leap_s = leap_after_s, leap_s
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._procs: list[subprocess.Popen[bytes]] = []
        self._lock = threading.Lock()
        self.connections = 0

    @property
    def url(self) -> str:
        return f"tcp://{self.host}:{self.port}"

    def start(self) -> LeapingCamera:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform != "win32":
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(4)
        srv.settimeout(0.2)
        self.port = srv.getsockname()[1]
        self._listener = srv
        t = threading.Thread(target=self._accept_loop, name="leap-accept", daemon=True)
        t.start()
        self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            if p.poll() is None:
                p.kill()
        for t in self._threads:
            t.join(3)

    def __enter__(self) -> LeapingCamera:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def encoder_argv(self) -> list[str]:
        k = max(1, round(self.leap_after_s * self.fps))        # the packet from which the labels are ahead
        ahead = f"if(gte(N\\,{k})\\,{self.leap_s:.6f}/TB\\,0)"
        return [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-re", "-f", "lavfi", "-i",
                f"testsrc2=size={self.size}:rate={self.fps}", "-c:v", "libx264", "-preset", "ultrafast",
                "-tune", "zerolatency", "-g", str(self.fps), "-keyint_min", str(self.fps), "-bf", "0",
                "-pix_fmt", "yuv420p", "-x264-params", "repeat-headers=1", "-b:v", "400k",
                "-bsf:v", f"setts=pts=PTS+{ahead}:dts=DTS+{ahead}", "-f", "nut", "-flush_packets", "1", "pipe:1"]

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            srv = self._listener
            if srv is None:
                return
            try:
                conn, _ = srv.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.connections += 1
            t = threading.Thread(target=self._serve, args=(conn,), name="leap-client", daemon=True)
            t.start()
            self._threads.append(t)

    def _serve(self, conn: socket.socket) -> None:
        kwargs = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        proc = subprocess.Popen(self.encoder_argv(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, **kwargs)
        with self._lock:
            self._procs.append(proc)
        try:
            assert proc.stdout is not None
            while not self._stop.is_set():
                data = proc.stdout.read1(65536)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass                           # the recorder went away
        finally:
            if proc.poll() is None:
                proc.kill()
            try:
                conn.close()
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=18555)
    ap.add_argument("--leap-after", type=float, default=7.0, help="seconds after a connection starts")
    ap.add_argument("--leap", type=float, default=11984.0, help="seconds the labels leap ahead")
    ap.add_argument("--fps", type=int, default=15)
    args = ap.parse_args(argv)
    cam = LeapingCamera(port=args.port, fps=args.fps, leap_after_s=args.leap_after, leap_s=args.leap).start()
    print(f"leaping camera at {cam.url} (input_format = \"nut\"); Ctrl+C to stop", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        cam.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
