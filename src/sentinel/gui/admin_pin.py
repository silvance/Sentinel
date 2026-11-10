"""Administrator PIN for the GUI's diagnostics area (requirement U5).

Design choice (see docs/branches/req-operator-gui.md): a PIN, stored only as a salted
PBKDF2-HMAC-SHA256 hash in ``<data_dir>/gui_admin_pin.json``, set with

    python -m sentinel.gui --config <toml> --set-admin-pin

Why a PIN and not an OS administrator check: deployment PCs are often run under an account that
*is* a local administrator (so an OS check would let every operator in), while the development
and many operator accounts are not (so it would lock real administrators out). A PIN is simple to
explain on the phone and works the same on Windows and Linux.

What it is NOT: a security boundary. The administrator area is read-only (it shows diagnostics
already present in files the GUI account can read) and offers no destructive action. The PIN
keeps technical detail away from operators; access control to the evidence itself is the job
of OS accounts and file permissions (docs/07).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PIN_FILE_NAME = "gui_admin_pin.json"
ITERATIONS = 200_000
MIN_LEN, MAX_LEN = 6, 12
MAX_FAILURES = 5
LOCKOUT_S = 60.0


class PinError(ValueError):
    pass


def pin_path(data_dir: Path) -> Path:
    return Path(data_dir) / PIN_FILE_NAME


def admin_pin_dir(cfg: Any) -> Path:
    """Folder of the PIN file (integration with design/windows-service).

    Default: the data folder, as before. With a separate ``[storage] status_dir`` (the Windows
    service layout) the data folder is service-only, so operators could not read the PIN file
    there; it goes into the configuration folder instead, which the service installer makes
    readable by Users and changeable only by Administrators (the service branch's rule: the PIN
    verifier must never be operator-writable). ``--set-admin-pin`` then needs an administrator."""
    if getattr(cfg.storage, "status_dir", None) and getattr(cfg, "source_path", None):
        return Path(cfg.source_path).resolve().parent
    return Path(cfg.data_path)


def validate_new_pin(pin: str) -> None:
    if not pin.isdigit() or not MIN_LEN <= len(pin) <= MAX_LEN:
        raise PinError(f"The PIN must be {MIN_LEN} to {MAX_LEN} digits.")
    if len(set(pin)) == 1 or pin in "01234567890123456789" or pin in "98765432109876543210":
        raise PinError("The PIN is too easy to guess (repeated or sequential digits).")


def _hash(pin: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)


def set_pin(data_dir: Path, pin: str) -> Path:
    validate_new_pin(pin)
    salt = secrets.token_bytes(16)
    doc = {"algorithm": "pbkdf2_sha256", "iterations": ITERATIONS, "salt": salt.hex(),
           "hash": _hash(pin, salt, ITERATIONS).hex()}
    target = pin_path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".partial")
    tmp.write_text(json.dumps(doc, indent=2), "utf-8")
    os.replace(tmp, target)
    return target


def pin_configured(data_dir: Path) -> bool:
    return _load(data_dir) is not None


def _load(data_dir: Path) -> dict | None:
    try:
        doc = json.loads(pin_path(data_dir).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict) or doc.get("algorithm") != "pbkdf2_sha256":
        return None
    if not all(isinstance(doc.get(k), str) for k in ("salt", "hash")) or not isinstance(doc.get("iterations"), int):
        return None
    return doc


@dataclass
class AdminGate:
    """Checks PINs with a simple lockout after repeated failures (in-memory, per GUI session)."""
    data_dir: Path
    failures: int = 0
    locked_until: float = 0.0

    def configured(self) -> bool:
        return pin_configured(self.data_dir)

    def seconds_locked(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, self.locked_until - now)

    def check(self, pin: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if self.seconds_locked(now) > 0:
            return False
        doc = _load(self.data_dir)
        if doc is None:
            return False
        try:
            salt = bytes.fromhex(doc["salt"])
            expected = bytes.fromhex(doc["hash"])
        except ValueError:
            return False
        ok = hmac.compare_digest(_hash(pin, salt, int(doc["iterations"])), expected)
        if ok:
            self.failures = 0
            return True
        self.failures += 1
        if self.failures >= MAX_FAILURES:
            self.failures = 0
            self.locked_until = now + LOCKOUT_S
        return False
