"""Simulated IP camera with fault injection (development and test use only).

FFmpeg generates an H.264 test pattern (``testsrc2``, which shows a running timecode) in
real time and muxes it to MPEG-TS on a pipe. A Python TCP server broadcasts that byte
stream, aligned to 188-byte TS packets, to every connected client — so, like a real camera,
the stream keeps running whether or not anyone is recording it.

The recorder connects with ``url = "tcp://127.0.0.1:<port>"`` and ``input_format = "mpegts"``.
This exercises the recorder's supervision, segmenting, validation and recovery logic. It
does **not** exercise RTSP session handling or RTP depacketisation; those require the real
camera or an RTSP server (see docs/02 risk B2 and docs/06).

Faults:
    freeze()        connection stays open, no data (RTSP freeze / camera hang)
    resume()        data flows again (timestamps jump forward by the frozen duration)
    drop_clients()  close all connections (network blip / camera reboot)
    go_offline()    refuse connections (camera unplugged)
    go_online()
    restart_encoder()  new encoder process: stream timestamps restart (camera reboot)
    corrupt(n)      overwrite the next n TS packets' payloads with random bytes
    clock_rate=     (constructor) camera clock runs fast/slow: stream time = rate x real time

CLI: ``python -m sentinel.sim.camera --port 8554`` then type a fault name and Enter.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
import threading
import time

log = logging.getLogger(__name__)
TS_PACKET = 188
CHUNK_PACKETS = 7  # 1316 bytes, the conventional UDP/TS payload size


class SimulatedCamera:
    def __init__(self, port: int = 0, host: str = "127.0.0.1", size: str = "640x480", fps: int = 15,
                 bitrate: str = "800k", gop: int | None = None, ffmpeg: str = "ffmpeg",
                 clock_rate: float = 1.0) -> None:
        """``clock_rate``: camera clock speed relative to real time (1.01 = camera clock 1 % fast:
        each real second carries 1.01 s of stream time). Frames are still produced and sent in
        real time (``-re``); only their timestamps are scaled (``setpts``). Measured with FFmpeg
        9.0.1: rate 1.01 gave 1.011 s of stream time per monotonic second at the receiver
        (1.0007 without scaling), whereas ``-readrate`` pacing was less exact."""
        if not 0.5 <= clock_rate <= 2.0:
            raise ValueError("clock_rate must be between 0.5 and 2.0")
        self.host, self.port = host, port
        self.size, self.fps, self.bitrate = size, fps, bitrate
        self.clock_rate = clock_rate
        self.gop = gop or fps          # one keyframe per second by default
        self.ffmpeg = ffmpeg
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._frozen = threading.Event()
        self._online = threading.Event()
        self._stop = threading.Event()
        self._corrupt_left = 0
        self._encoder: subprocess.Popen[bytes] | None = None
        self._listener: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self.bytes_sent = 0

    # ------------------------------------------------------------------ lifecycle
    @property
    def url(self) -> str:
        return f"tcp://{self.host}:{self.port}"

    def start(self) -> SimulatedCamera:
        self._start_encoder()
        self._open_listener()
        self._online.set()
        for target, name in ((self._pump, "sim-pump"), (self._accept_loop, "sim-accept")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        self.go_offline()
        self._kill_encoder()
        for t in self._threads:
            t.join(3)

    def __enter__(self) -> SimulatedCamera:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------ faults
    def freeze(self) -> None:
        self._frozen.set()

    def resume(self) -> None:
        self._frozen.clear()

    def drop_clients(self) -> None:
        with self._lock:
            clients, self._clients = self._clients, []
        for c in clients:
            _close(c)

    def go_offline(self) -> None:
        self._online.clear()
        if self._listener is not None:
            _close(self._listener)
            self._listener = None
        self.drop_clients()

    def go_online(self) -> None:
        if self._online.is_set():
            return
        self._open_listener()
        self._online.set()

    def restart_encoder(self) -> None:
        self._kill_encoder()
        self._start_encoder()

    def corrupt(self, packets: int = 200) -> None:
        with self._lock:
            self._corrupt_left = packets

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    # ------------------------------------------------------------------ internals
    def _start_encoder(self) -> None:
        argv = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-re",
                "-f", "lavfi", "-i", f"testsrc2=size={self.size}:rate={self.fps}"]
        if self.clock_rate != 1.0:
            # Camera clock drift: fine timebase so scaled timestamps are not rounded to frame
            # ticks; passthrough so FFmpeg does not drop/duplicate frames to restore the rate.
            argv += ["-vf", f"settb=1/90000,setpts=PTS*{self.clock_rate:.6f}", "-fps_mode", "passthrough"]
        argv += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-g", str(self.gop), "-keyint_min", str(self.gop), "-bf", "0", "-pix_fmt", "yuv420p",
                # SPS/PPS before every keyframe, as IP cameras do, so late joiners can decode.
                "-x264-params", "repeat-headers=1",
                "-b:v", self.bitrate, "-f", "mpegts", "-flush_packets", "1", "pipe:1"]
        kwargs = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        self._encoder = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, **kwargs)

    def _kill_encoder(self) -> None:
        enc, self._encoder = self._encoder, None
        if enc is not None and enc.poll() is None:
            enc.kill()
            enc.wait(5)

    def _open_listener(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform != "win32":
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(4)
        srv.settimeout(0.2)
        self.port = srv.getsockname()[1]
        self._listener = srv

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            srv = self._listener
            if srv is None or not self._online.is_set():
                time.sleep(0.05)
                continue
            try:
                conn, _ = srv.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                time.sleep(0.05)
                continue
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(2.0)
            with self._lock:
                self._clients.append(conn)

    def _pump(self) -> None:
        chunk = TS_PACKET * CHUNK_PACKETS
        while not self._stop.is_set():
            enc = self._encoder
            if enc is None or enc.stdout is None:
                time.sleep(0.02)
                continue
            data = _read_exact(enc.stdout, chunk)
            if not data:  # encoder exited or was restarted
                time.sleep(0.02)
                continue
            if self._frozen.is_set():
                continue  # camera "hangs": frames produced while frozen are never delivered
            data = self._maybe_corrupt(data)
            with self._lock:
                clients = list(self._clients)
            dead = []
            for c in clients:
                try:
                    c.sendall(data)
                    self.bytes_sent += len(data)
                except OSError:
                    dead.append(c)
            if dead:
                with self._lock:
                    self._clients = [c for c in self._clients if c not in dead]
                for c in dead:
                    _close(c)

    def _maybe_corrupt(self, data: bytes) -> bytes:
        with self._lock:
            if self._corrupt_left <= 0:
                return data
            n = min(self._corrupt_left, len(data) // TS_PACKET)
            self._corrupt_left -= n
        buf = bytearray(data)
        for i in range(n):
            off = i * TS_PACKET
            buf[off + 4: off + TS_PACKET] = os.urandom(TS_PACKET - 4)  # keep sync byte + header
        return bytes(buf)


def _read_exact(stream, n: int) -> bytes:  # noqa: ANN001
    buf = bytearray()
    while len(buf) < n:
        part = stream.read(n - len(buf))
        if not part:
            return b""
        buf += part
    return bytes(buf)


def _close(s: socket.socket) -> None:
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        s.close()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Simulated camera for Sentinel development (not for production).")
    ap.add_argument("--port", type=int, default=8554)
    ap.add_argument("--size", default="640x480")
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--bitrate", default="800k")
    ap.add_argument("--clock-rate", type=float, default=1.0,
                    help="camera clock speed vs real time, e.g. 1.0001 = 100 ppm fast (drift testing)")
    args = ap.parse_args(argv)
    cam = SimulatedCamera(port=args.port, size=args.size, fps=args.fps, bitrate=args.bitrate,
                          clock_rate=args.clock_rate).start()
    print(f"simulated camera at {cam.url} (input_format = \"mpegts\")")
    print("commands: freeze resume drop offline online restart corrupt quit")
    actions = {"freeze": cam.freeze, "resume": cam.resume, "drop": cam.drop_clients, "offline": cam.go_offline,
               "online": cam.go_online, "restart": cam.restart_encoder, "corrupt": cam.corrupt}
    try:
        for line in sys.stdin:
            cmd = line.strip().lower()
            if cmd in ("quit", "exit"):
                break
            if cmd in actions:
                actions[cmd]()
                print(f"ok: {cmd}")
            elif cmd:
                print(f"unknown command {cmd!r}")
    except KeyboardInterrupt:
        pass
    finally:
        cam.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
