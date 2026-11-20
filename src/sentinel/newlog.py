"""Start a new recording log: the supported way to clear test recordings before real use (0.3.27).

Owner's report 2026-11-20, from the laptop: "I have to clear the folder to remove test recordings before the
real recordings start and there's no other way to do so." The test recordings had been deleted from the storage
folder by hand; the recording log still listed all 327, and Sentinel rightly showed V-001, A-006 and A-008 with
no way to clear them.

``Sentinel.exe new-recording-log`` (administrator) does it in one step, in this order:

1. stops the recorder (it finishes and saves its current file) and archive copying, and holds both locks;
2. writes ``recording_log_closed`` into the log in use, then sets that log aside, whole and unchanged, in
   ``<data>/previous-recording-logs/<UTC time>/`` with the copier's state and the diagnostic reports. It is
   never deleted: every recording that existed stays listed there, and its fingerprint (chain head) is written
   into the new log. If the log cannot be moved (a Sentinel window still has it open) nothing else has been
   touched: the log says so, recording is started again and the command stops;
3. deletes the recordings on this computer (the storage folder);
4. empties the archive drives that are plugged in: Sentinel's own folders on them. The drive's marker, and
   with it its name (SSD_1), stays, so it is still a set-up archive drive;
5. starts a new log whose first entry, ``recording_log_started``, says who did this, why, what was removed,
   and where the earlier log is;
6. starts recording and archive copying again.

Settings (camera, schedule, motion, storage levels), the camera's sign-in and installed versions are not touched.
"""

from __future__ import annotations

import getpass
import json
import os
import signal
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .archive import layout
from .clock import iso_utc
from .config import AppConfig

CONFIRM_WORDS = "NEW LOG"
SET_ASIDE_DIR = "previous-recording-logs"
LEDGER_FILES = ("sentinel.db", "sentinel.db-wal", "sentinel.db-shm")
STATE_FILES = ("chain_heads.log", "archive_status.json")
STATE_DIRS = ("archive_control", "diagnostics")
DRIVE_ENTRIES = (layout.ROOT, layout.WATCH_DIR, layout.START_HERE)     # Sentinel's own things on an archive drive
NAME_CARRIED = "drive_name_carried_over"       # read by sentinel.drives.prepare.next_label
EXIT_OK, EXIT_CONFIG, EXIT_REFUSED, EXIT_FAILED = 0, 2, 3, 4


class NewLogRefused(RuntimeError):
    """Nothing was removed. The message is for the person at the keyboard."""


@dataclass
class DrivePlan:
    root: str
    label: str
    drive_uuid: str
    files: int = 0
    bytes: int = 0
    not_removed: list[str] = field(default_factory=list)


@dataclass
class Plan:
    log_exists: bool = False
    recordings: int = 0
    first_start_utc: str | None = None
    last_end_utc: str | None = None
    entries: int = 0                       # events in the log
    chain_seq: int | None = None
    chain_hash: str | None = None
    drive_names: list[str] = field(default_factory=list)      # names given to drives so far (kept in use)
    spool_files: int = 0
    spool_bytes: int = 0
    drives: list[DrivePlan] = field(default_factory=list)
    skipped_drives: list[str] = field(default_factory=list)


def is_administrator() -> bool:
    if sys.platform == "win32":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (OSError, AttributeError):
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _tree(path: Path) -> tuple[int, int]:
    """(files, bytes) under ``path`` (a file counts as itself)."""
    if path.is_file():
        try:
            return 1, path.stat().st_size
        except OSError:
            return 1, 0
    n = size = 0
    for base, _dirs, names in os.walk(path):
        for name in names:
            n += 1
            try:
                size += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
    return n, size


def _remove(path: Path) -> list[str]:
    """Delete a file or a whole folder. Recordings and their copies are sealed read-only, so each file is made
    writable first. Returns what could not be deleted (for example a video that is open in a player)."""
    failed: list[str] = []

    def unlink(p: str) -> None:
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            os.unlink(p)
        except FileNotFoundError:
            pass
        except OSError:
            failed.append(p)

    if path.is_symlink() or path.is_file():
        unlink(str(path))
        return failed
    for base, dirs, names in os.walk(path, topdown=False):
        for name in names:
            unlink(os.path.join(base, name))
        for name in dirs:
            try:
                os.rmdir(os.path.join(base, name))
            except OSError:
                pass                      # not empty: a file in it is already in ``failed``
    try:
        os.rmdir(path)
    except OSError:
        pass
    return failed


def _old_log_facts(db: Path, plan: Plan) -> None:
    from .db import Ledger

    try:
        if not db.is_file() or db.stat().st_size == 0:      # an empty file is a log that was never written
            return
    except OSError:
        return
    plan.log_exists = True
    led = Ledger(db, readonly=True)
    try:
        row = led.query("SELECT COUNT(*), MIN(start_utc), MAX(end_utc) FROM segments")[0]
        plan.recordings, plan.first_start_utc, plan.last_end_utc = int(row[0] or 0), row[1], row[2]
        plan.entries = int(led.query("SELECT COUNT(*) FROM events")[0][0] or 0)
        head = led.chain_head()
        if head is not None:
            plan.chain_seq, plan.chain_hash = head.seq, head.hash
        names: list[str] = []
        for r in led.query("SELECT detail FROM events WHERE category='drives' AND code IN ('drive_prepared', "
                           f"'drive_renamed', '{NAME_CARRIED}') ORDER BY event_id"):
            try:
                d = json.loads(r[0] or "{}")
            except ValueError:
                continue
            for key in ("label", "new_label"):
                if isinstance(d, dict) and d.get(key) and str(d[key]) not in names:
                    names.append(str(d[key]))
        plan.drive_names = names
    finally:
        led.close()


def _default_locator(cfg: AppConfig) -> Any:
    from .archive.locator import VolumeScanLocator

    a = cfg.archive
    return VolumeScanLocator([a.drive_path] if a.drive_path else [], require_role=a.require_drive_marker,
                             roots=None if a.scan_volumes else (lambda: []))


def survey(cfg: AppConfig, *, locator: Any = None, empty_drives: bool = True,
           same_volume_ok: bool = False) -> Plan:
    """What a new recording log would set aside and remove. Changes nothing."""
    from .export.target import TargetError, protected_volume_conflicts, system_paths

    plan = Plan()
    _old_log_facts(cfg.data_path / "sentinel.db", plan)
    if cfg.spool_path.is_dir():
        plan.spool_files, plan.spool_bytes = _tree(cfg.spool_path)
    if not empty_drives or not cfg.archive.enabled:
        return plan
    scan = (locator or _default_locator(cfg)).scan()
    for root, why in scan.unusable:
        plan.skipped_drives.append(f"{root}: {why}")
    protected = {"recording storage": cfg.spool_path, "recorder database": cfg.data_path,
                 "recorder logs": cfg.log_path, **system_paths()}
    for t in scan.candidates:
        root = Path(t.root)
        if not same_volume_ok:
            # The copier's own rule: an "archive drive" on the volume of the recordings, the log or Windows is
            # never written, so it is never emptied either.
            try:
                clash = protected_volume_conflicts(root, protected)
            except TargetError as exc:
                clash = [f"cannot confirm it is a separate drive: {exc}"]
            if clash:
                plan.skipped_drives.append(f"{t.label or root}: on the same volume as the {', '.join(clash)}")
                continue
        d = DrivePlan(root=str(root), label=str(t.label or "unnamed archive drive"),
                      drive_uuid=str(t.identity.get("drive_uuid") or ""))
        for name in DRIVE_ENTRIES:
            p = root / name
            if p.exists():
                n, size = _tree(p)
                d.files += n
                d.bytes += size
        plan.drives.append(d)
    return plan


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e8 else f"{n / 1e6:.1f} MB"


def _day(iso: str | None) -> str:
    return (iso or "?")[:10]


def describe(plan: Plan, cfg: AppConfig) -> list[str]:
    """What will happen, for the person to read before confirming."""
    out = ["START A NEW RECORDING LOG", "",
           "Use this once, when testing is over and before the real recordings start.", ""]
    if plan.log_exists:
        span = (f", {_day(plan.first_start_utc)} to {_day(plan.last_end_utc)}" if plan.recordings else "")
        out += [f"1. The recording log in use lists {plan.recordings} recording(s){span} and {plan.entries} "
                "log entries.",
                "   It is closed and SET ASIDE, whole and unchanged, in",
                f"      {cfg.data_path / SET_ASIDE_DIR}",
                "   It is not deleted."]
    else:
        out += ["1. There is no recording log yet, so there is nothing to set aside."]
    out += [f"2. The recordings on this computer are DELETED: {plan.spool_files} file(s), {_gb(plan.spool_bytes)}, in",
            f"      {cfg.spool_path}"]
    if plan.drives:
        out.append("3. These archive drives are plugged in and are EMPTIED (they keep their names):")
        out += [f"      {d.label}: {d.files} file(s), {_gb(d.bytes)}" for d in plan.drives]
    else:
        out.append("3. No archive drive is plugged in, so no drive is emptied.")
    for s in plan.skipped_drives:
        out.append(f"      NOT emptied: {s}")
    out += ["   An archive drive that is NOT plugged in now keeps its test recordings. Plug in every archive",
            "   drive before you go on, or cancel now.",
            "4. A new, empty recording log is started. Its first entry records this step and who did it.",
            "5. Recording and archive copying are started again.", "",
            "NOT changed: the settings (camera, schedule, motion, storage), the camera's sign-in, the installed",
            "version of Sentinel."]
    return out


def _end_copier(cfg: AppConfig, control: Any, sleep: Callable[[float], None]) -> None:
    stopper = getattr(control, "stop_archive", None)
    if callable(stopper):                       # Windows service
        stopper()
        return
    from .archive.status import STATUS_FILE, read_archive_status
    from .launcher.control import pid_alive

    doc = read_archive_status(cfg.data_path / STATUS_FILE) or {}
    pid = doc.get("pid")
    if not isinstance(pid, int) or pid == os.getpid() or pid_alive(pid) is False:
        return
    # The copier has no stop channel; an update ends it the same way. It is built to be cut off at any point
    # (a drive can be pulled mid-copy), and what it was copying is about to be removed anyway.
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(40):
        if pid_alive(pid) is False:
            return
        sleep(0.5)


def _locks(cfg: AppConfig, sleep: Callable[[float], None]) -> list[Any]:
    """Hold the recorder's and the copier's instance locks, so neither can start while the log is replaced."""
    from .platform import get_platform
    from .platform.base import InstanceLockError

    plat, held = get_platform(), []
    for name in ("recorder.lock", "archive.lock"):
        for attempt in range(20):
            try:
                held.append(plat.acquire_instance_lock(cfg.data_path / name))
                break
            except InstanceLockError:
                if attempt == 19:
                    _release(held)
                    what = "recorder" if name.startswith("recorder") else "archive copying"
                    raise NewLogRefused(f"The {what} is still running and could not be stopped. Nothing was "
                                        "changed.") from None
                sleep(0.5)
    return held


def _release(held: list[Any]) -> None:
    for h in held:
        try:
            h.close()
        except Exception:  # noqa: BLE001 - a lock handle of another platform; the process ends soon anyway
            pass
    held.clear()


@dataclass
class Result:
    set_aside: str | None
    removed_files: int
    removed_bytes: int
    not_removed: list[str]
    drives: list[DrivePlan]
    left_in_place: list[str]
    started: bool | None                # None: not asked to start
    start_detail: str = ""


def run(cfg_path: Path, cfg: AppConfig, *, who: str, reason: str, empty_drives: bool = True, start: bool = True,
        control: Any = None, locator: Any = None, same_volume_ok: bool = False,
        say: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time) -> Result:
    """Do it (see the module text). The caller has checked administrator rights and the confirmation."""
    from .db import Ledger
    from .launcher.control import select_control

    data = cfg.data_path
    db = data / "sentinel.db"
    control = control or select_control(Path(cfg_path), cfg)
    plan = survey(cfg, locator=locator, empty_drives=empty_drives, same_volume_ok=same_volume_ok)

    def restart() -> tuple[bool, str]:
        res = control.start()
        starter = getattr(control, "start_archive", None)
        if res.ok and callable(starter):
            starter()
        return bool(res.ok), str(res.detail or res.code or "")

    say("Stopping the recorder (it finishes and saves its current file first)...")
    was_running = bool(control.is_running())
    if was_running and not control.stop(120.0):
        raise NewLogRefused("The recorder did not stop in time. Nothing was changed.")
    say("Stopping archive copying...")
    _end_copier(cfg, control, sleep)
    try:
        held = _locks(cfg, sleep)
    except NewLogRefused:
        if was_running:
            restart()
        raise

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now()))
    dest, n = data / SET_ASIDE_DIR / stamp, 1
    while dest.exists():                    # never the folder of a log set aside earlier
        n += 1
        dest = data / SET_ASIDE_DIR / f"{stamp}-{n}"
    user = _windows_user()
    left: list[str] = []
    set_aside: str | None = None
    try:
        if plan.log_exists:
            say("Closing the recording log and setting it aside...")
            led = Ledger(db)
            try:
                led.add_event("audit", "service", "recording_log_closed",
                              "This recording log was closed and set aside; a new recording log was started",
                              {"who": who, "windows_user": user, "reason": reason, "set_aside_to": str(dest),
                               "app_version": __version__})
                head = led.chain_head()
            finally:
                led.close()
            if head is not None:
                plan.chain_seq, plan.chain_hash = head.seq, head.hash
            try:
                _set_log_aside(db, dest)
            except OSError as exc:
                # Another program has the log open (a Sentinel window), or it could not be copied. Nothing else
                # has been touched (_set_log_aside takes away the copy it had begun).
                try:
                    (data / SET_ASIDE_DIR).rmdir()          # only if empty: earlier logs set aside stay
                except OSError:
                    pass
                try:
                    led = Ledger(db)
                    try:
                        led.add_event("audit", "service", "recording_log_close_abandoned",
                                      "The recording log could not be set aside and stays in use",
                                      {"who": who, "windows_user": user, "error": f"{type(exc).__name__}: {exc}"})
                    finally:
                        led.close()
                except Exception:  # noqa: BLE001 - the refusal below is what matters
                    pass
                _release(held)
                if was_running:                 # never leave the recorder stopped because of a refusal
                    restart()
                raise NewLogRefused(
                    f"The recording log could not be set aside ({exc}). Close every Sentinel window and run this "
                    "again. No recording was deleted and no drive was emptied; the recording log is still the "
                    "one in use" + (", and recording was started again." if was_running else ".")) from exc
            set_aside = str(dest)
            for name in (*STATE_FILES, *STATE_DIRS):
                src = data / name
                if not src.exists():
                    continue
                try:
                    os.replace(src, dest / name)
                except OSError:
                    # Not the log itself. The copier's state must not survive into the new log, so what cannot
                    # be moved is removed; a diagnostic report that is open somewhere simply stays where it is.
                    if name != "diagnostics" and _remove(src):
                        left.append(str(src))
                    elif name == "diagnostics":
                        left.append(str(src))
            _about_file(dest, who, user, reason, plan, now())
        else:
            for name in (*STATE_FILES, STATE_DIRS[0]):
                _remove(data / name)

        # From here on the earlier log is set aside. Whatever goes wrong below must not keep the new log from
        # getting its first entry, so each part is tried by itself and what failed is written into that entry.
        say("Deleting the recordings on this computer...")
        not_removed: list[str] = []
        try:
            if cfg.spool_path.is_dir():
                for child in sorted(cfg.spool_path.iterdir()):
                    not_removed += _remove(child)
        except Exception as exc:  # noqa: BLE001 - see above
            left.append(f"recordings on this computer: {type(exc).__name__}: {exc}")
        for d in plan.drives:
            say(f"Emptying archive drive {d.label}...")
            try:
                for name in DRIVE_ENTRIES:
                    p = Path(d.root) / name
                    if p.exists() or p.is_symlink():
                        d.not_removed += _remove(p)
            except Exception as exc:  # noqa: BLE001 - see above
                d.not_removed.append(f"{d.label}: {type(exc).__name__}: {exc}")
        # What the window would show from these is about the log that was set aside. The recorder writes both
        # again when it starts.
        for stale in (cfg.status_path, cfg.ledger_snapshot_path):
            if stale is not None:
                try:
                    stale.unlink()
                except OSError:
                    pass

        say("Starting the new recording log...")
        led = Ledger(db)
        try:
            led.add_event("audit", "service", "recording_log_started",
                          "A new recording log was started; the earlier one was closed and set aside",
                          {"who": who, "windows_user": user, "reason": reason, "app_version": __version__,
                           "previous_log": {"set_aside_to": set_aside, "chain_seq": plan.chain_seq,
                                            "chain_hash": plan.chain_hash, "recordings": plan.recordings,
                                            "first_start_utc": plan.first_start_utc,
                                            "last_end_utc": plan.last_end_utc, "entries": plan.entries},
                           "removed_from_this_computer": {"files": plan.spool_files, "bytes": plan.spool_bytes,
                                                          "not_removed": not_removed[:50],
                                                          "not_removed_count": len(not_removed)},
                           "archive_drives_emptied": [{"label": d.label, "drive_uuid": d.drive_uuid,
                                                       "files": d.files, "bytes": d.bytes,
                                                       "not_removed": d.not_removed[:50],
                                                       "not_removed_count": len(d.not_removed)}
                                                      for d in plan.drives],
                           "archive_drives_not_emptied": plan.skipped_drives, "left_in_place": left})
            # Drive names stay taken: the next new drive is not given the name of one that already exists.
            for name in plan.drive_names:
                led.add_event("audit", "drives", NAME_CARRIED,
                              f"Drive name {name} was in use before this recording log was started",
                              {"label": name})
        finally:
            led.close()
    finally:
        _release(held)

    started: bool | None = None
    detail = ""
    if start:
        say("Starting recording again...")
        started, detail = restart()
    return Result(set_aside=set_aside, removed_files=plan.spool_files - len(not_removed),
                  removed_bytes=plan.spool_bytes, not_removed=not_removed, drives=plan.drives,
                  left_in_place=left, started=started, start_detail=detail)


def _in_use_by_another_program(path: Path) -> bool:
    """Windows: does any other program have the file open? (An open that shares nothing fails if so.)"""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)   # GENERIC_READ, no sharing, OPEN_EXISTING
    if h is None or h == wintypes.HANDLE(-1).value:
        return True
    k32.CloseHandle(h)
    return False


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _set_log_aside(db: Path, dest: Path) -> None:
    """Copy the closed log to ``dest``, check the copy byte for byte, then empty the file in place.

    The file itself stays (an empty file is a new, empty log to SQLite), and with it its owner and permissions:
    a log created afresh by this administrator command could not be written by a recorder that runs without
    administrator rights. Raises OSError, with nothing emptied, when the log is open elsewhere or the copy is
    not exact."""
    leftovers = [n for n in LEDGER_FILES[1:] if (db.parent / n).exists()]
    if leftovers or _in_use_by_another_program(db):
        raise OSError("it is open in another program")
    dest.mkdir(parents=True, exist_ok=False)        # a folder that exists is another log: never written into
    copy = dest / db.name
    try:
        with open(db, "rb") as src, open(copy, "xb") as out:
            for block in iter(lambda: src.read(1 << 20), b""):
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        if _sha256(copy) != _sha256(db):
            raise OSError("the copy is not the same as the log")
    except OSError:
        for p in (copy,):                           # only what this call made
            try:
                p.unlink()
            except OSError:
                pass
        try:
            dest.rmdir()
        except OSError:
            pass
        raise
    with open(db, "r+b") as fh:
        fh.truncate(0)
        fh.flush()
        os.fsync(fh.fileno())


def _windows_user() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return "unknown"


def _about_file(dest: Path, who: str, user: str, reason: str, plan: Plan, when: float) -> None:
    text = ("SENTINEL RECORDING LOG, SET ASIDE\n"
            "=================================\n"
            f"Set aside (UTC):  {iso_utc(when)}\n"
            f"By:               {who} (Windows account {user})\n"
            f"Reason:           {reason}\n"
            f"Sentinel version: {__version__}\n\n"
            "This folder holds the recording log that was in use until then (sentinel.db), unchanged, with the\n"
            "archive copier's state and the diagnostic reports of that time. A new recording log was started\n"
            "in its place; its first entry names this folder and the fingerprint below.\n\n"
            f"Recordings listed in it: {plan.recordings}"
            + (f" ({plan.first_start_utc} to {plan.last_end_utc})" if plan.recordings else "") + "\n"
            f"Log entries:             {plan.entries}\n"
            f"Last entry (fingerprint): seq {plan.chain_seq}  {plan.chain_hash}\n\n"
            "The recordings it lists were deleted from this computer and from the archive drives that were\n"
            "plugged in, on purpose, in the same step. Do not delete this folder: it is the record of what\n"
            "existed before.\n")
    try:
        (dest / "ABOUT THIS FOLDER.txt").write_text(text, encoding="utf-8", newline="\r\n")
    except OSError:
        pass


def main(cfg_path: Path | None, *, who: str | None = None, reason: str | None = None, yes: bool = False,
         confirm: str | None = None, keep_drives: bool = False, no_start: bool = False, dry_run: bool = False,
         reader: Callable[[str], str] = input, admin: Callable[[], bool] = is_administrator,
         interactive: bool | None = None, control: Any = None, locator: Any = None) -> int:
    """``Sentinel.exe new-recording-log``. Prints for the person at the keyboard; returns the exit code."""
    from .config import ConfigError, load_config

    if cfg_path is None or not Path(cfg_path).is_file():
        print("No configuration file found: this computer has not been set up, so there is no recording log.")
        return EXIT_CONFIG
    try:
        cfg = load_config(Path(cfg_path))
    except (ConfigError, OSError) as exc:
        print(f"The settings cannot be read ({exc}). Nothing was changed.")
        return EXIT_CONFIG
    plan = survey(cfg, locator=locator, empty_drives=not keep_drives)
    for line in describe(plan, cfg):
        print(line)
    print(flush=True)
    if dry_run:
        print("DRY RUN: nothing was changed.")
        return EXIT_OK
    if not admin():
        print("This needs Windows administrator rights (right-click, \"Run as administrator\"). Nothing was changed.")
        return EXIT_REFUSED
    interactive = sys.stdin.isatty() if interactive is None else interactive
    if yes:
        typed = (confirm or "").strip()
    elif interactive:
        try:
            typed = reader(f"To go on, type {CONFIRM_WORDS} and press Enter (anything else cancels): ").strip()
        except EOFError:
            typed = ""
    else:
        typed = ""
    if typed.upper() != CONFIRM_WORDS:
        print("Not confirmed. Nothing was changed.")
        return EXIT_REFUSED
    if not who and interactive and not yes:
        try:
            who = reader("Your name (it is recorded): ").strip()
        except EOFError:
            who = ""
    who = (who or "").strip() or _windows_user()
    reason = (reason or "").strip() or "testing finished; start of real recording"
    print()
    try:
        res = run(Path(cfg_path), cfg, who=who, reason=reason, empty_drives=not keep_drives, start=not no_start,
                  control=control, locator=locator, say=lambda s: print(s, flush=True))
    except NewLogRefused as exc:
        print(f"\nNOT DONE. {exc}")
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 - the person must be told where things stand, not shown a traceback
        aside = cfg.data_path / SET_ASIDE_DIR
        print(f"\nSTOPPED by an unexpected problem: {type(exc).__name__}: {exc}")
        if aside.is_dir():
            print(f"  A recording log that was set aside is kept in {aside}")
        print("  Open Sentinel: opening it starts recording. Then send a diagnostic report to technical support.")
        return EXIT_FAILED
    print()
    print("DONE: a new recording log was started.")
    if res.set_aside:
        print(f"  The earlier log is kept in {res.set_aside}")
    print(f"  Recordings deleted from this computer: {res.removed_files} file(s), {_gb(res.removed_bytes)}")
    for d in res.drives:
        print(f"  Archive drive {d.label} emptied: {d.files - len(d.not_removed)} file(s), {_gb(d.bytes)}")
    problems = [*res.not_removed, *[x for d in res.drives for x in d.not_removed], *res.left_in_place]
    if problems:
        print(f"  {len(problems)} file(s) could NOT be deleted (open in another program?). They are named in the "
              "new log's first entry:")
        for p in problems[:10]:
            print(f"    {p}")
    if res.started is True:
        print("  Recording was started again. Open Sentinel and check that Status says what you expect.")
    elif res.started is False:
        print(f"  Recording could NOT be started ({res.start_detail}). Open Sentinel: opening it starts recording.")
    else:
        print("  Recording was not started (--no-start). Open Sentinel: opening it starts recording.")
    return EXIT_FAILED if (problems or res.started is False) else EXIT_OK


def plan_as_dict(plan: Plan) -> dict[str, Any]:
    return asdict(plan)
