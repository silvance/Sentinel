"""Administrator command line for drives.

    python -m sentinel.drives list      [--config FILE] [--json]
    python -m sentinel.drives classify  --disk ID [--config FILE] [--json]
    python -m sentinel.drives prepare   --disk ID --config FILE [--who NAME] [--admin]
                                        [--yes --serial-tail XXXX] [--dry-run]
    python -m sentinel.drives eject     --disk ID [--config FILE]
    python -m sentinel.drives snapshot  (raw read-only enumeration JSON, for support)

``--disk`` is the Windows disk number or Linux kernel name shown by ``list``. It only *locates*
the disk: the confirmation is bound to the disk's identity, which is re-checked before erasing.

``prepare`` is the administrator name of the operator action "Erase and Set Up Drive". It requires
the installed recorder configuration (see ``installed.py``; otherwise refused with M-008) and never
erases a drive classified SENTINEL_EVIDENCE. It never runs without a confirmation. Interactively it asks the person to type ERASE
(BLANK / WRONG_FORMAT) or the last characters of the serial number (FOREIGN_DATA). When standard
input is not a terminal it refuses unless both ``--yes`` and ``--serial-tail`` are given, and the
serial tail is then checked for every drive class.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..alerts import Severity, make_alert
from .classify import Classification, classify_all
from .errors import DriveError, PrepareFailed, PrepareRefused
from . import installed as _installed
from .known import KnownDrives, load_known
from .model import DiskInfo, DriveClass, human_size, serial_tail
from .policy import DrivePolicy
from .protected import ProtectedStorage, default_paths, resolve

EXIT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_FAILED = 0, 1, 2, 3


class _Env:
    def __init__(self, args: argparse.Namespace) -> None:
        self.policy = DrivePolicy()
        self.cfg = None
        self.config_path: Path | None = None
        if args.config:
            from ..config import ConfigError, load_config

            self.config_path = Path(args.config)
            try:
                self.cfg = load_config(self.config_path)   # validates [drives] too (finding 1.5)
            except (OSError, ConfigError) as exc:
                raise SystemExit(f"configuration problem: {exc}") from None
            self.policy = self.cfg.drives
        st = self.cfg.storage if self.cfg else None
        self.protected: ProtectedStorage = resolve(default_paths(st.spool_dir if st else None,
                                                                 st.data_dir if st else None,
                                                                 st.log_dir if st else None))
        self.windows = sys.platform == "win32"

    def enumerate(self) -> list[DiskInfo]:
        if self.windows:
            from .windows_enum import enumerate_windows

            return enumerate_windows()
        from .linux_enum import enumerate_linux

        return enumerate_linux()

    def known(self) -> KnownDrives:
        """Drives the ledger knows (read-only). Without a config no ledger is known: listing still
        works, and erasing is refused anyway (prepare needs the installed configuration)."""
        if self.cfg is None:
            return KnownDrives()
        db = self.cfg.data_path / "sentinel.db"
        present, why = _installed.path_state(db)
        if present is None:   # review 2026-11-10: access denied is NOT "no ledger" (fail closed)
            return KnownDrives(error=f"the recorder ledger {db} cannot be read by this account ({why}); "
                                     f"{_installed.ADMIN_HINT}")
        if not present:
            return KnownDrives()
        from ..db import Ledger

        try:
            led = Ledger(db, readonly=True)
        except Exception as exc:  # noqa: BLE001 - unreadable ledger => fail closed
            return KnownDrives(error=str(exc))
        try:
            return load_known(led)
        finally:
            led.close()

    def classify(self) -> tuple[list[Classification], list[str]]:
        return classify_all(self.enumerate(), self.policy, self.protected, known=self.known())


def _print_table(classes: list[Classification]) -> None:
    print(f"{'Disk':<6}{'Class':<14}{'Size':>9}  {'Bus':<8}{'Model':<26}{'Serial':<24}")
    for c in classes:
        d = c.disk
        print(f"{d.disk_id:<6}{c.drive_class:<14}{human_size(d.size_bytes):>9}  {(d.bus_type or '?')[:7]:<8}"
              f"{(d.model or '?')[:25]:<26}{(d.serial or '-')[:23]:<24}")
        print(f"      {c.message}")
        if c.reasons:
            print(f"      reasons: {', '.join(c.reasons)}")


def _alert_line(code: str | None) -> str:
    return make_alert(code, Severity.WARNING).message if code else ""


def cmd_list(env: _Env, args: argparse.Namespace) -> int:
    classes, problems = env.classify()
    if args.json:
        print(json.dumps({"drives": [c.to_dict() for c in classes], "protection_problems": problems,
                          "policy": asdict(env.policy)}, indent=2))
    else:
        if problems:
            print("WARNING: erasing and setting up drives is blocked (fail closed):")
            for p in problems:
                print(f"  - {p}")
            print()
        _print_table(classes)
    return EXIT_OK


def _find(classes: list[Classification], disk_id: str) -> Classification:
    c = next((x for x in classes if x.disk.disk_id == disk_id), None)
    if c is None:
        raise SystemExit(f"no disk {disk_id!r}; run 'list' to see disk identifiers")
    return c


def cmd_classify(env: _Env, args: argparse.Namespace) -> int:
    classes, problems = env.classify()
    c = _find(classes, args.disk)
    if args.json:
        print(json.dumps({**c.to_dict(), "protection_problems": problems}, indent=2))
    else:
        _print_table([c])
        from .prepare import requirements

        req = requirements(c)
        print(f"      erase and set up allowed: {req['can_prepare']}  administrator needed: {req['needs_admin']}  "
              f"serial confirmation needed: {req['needs_serial']}")
        for p in problems:
            print(f"      blocked: {p}")
    return EXIT_OK


def _ask(prompt: str, reader: Callable[[str], str]) -> str:
    try:
        return reader(prompt).strip()
    except EOFError:
        return ""


def cmd_prepare(env: _Env, args: argparse.Namespace, *, reader: Callable[[str], str] = input,
                interactive: bool | None = None) -> int:
    from .prepare import Authorization, Confirmation, PrepareContext, build_plan, next_label, os_administrator_check

    if env.cfg is None:
        print("prepare needs --config (to know where recordings are kept and where to write the audit record).")
        return EXIT_USAGE
    if not env.policy.erase_enabled and not args.dry_run:
        from .classify import ERASE_DISABLED_MESSAGE

        print(f"{_alert_line('M-010')}\n{ERASE_DISABLED_MESSAGE} Nothing was erased.")
        print("(Administrators: see docs/branches/design-drive-prepare.md section 9 before setting "
              "[drives] erase_enabled = true.)")
        return EXIT_REFUSED
    not_installed = _installed.installed_config_problems(env.config_path, env.cfg)
    if not_installed and not args.dry_run:
        print(_alert_line("M-008"))
        print("Erasing drives is refused because the recording storage cannot be confirmed (fail closed):")
        for p in not_installed:
            print(f"  - {p}")
        return EXIT_REFUSED
    interactive = sys.stdin.isatty() if interactive is None else interactive
    classes, problems = env.classify()
    c = _find(classes, args.disk)
    _print_table([c])
    if not c.can_prepare:
        print(f"\nThis drive cannot be erased and set up ({c.refusal}). Nothing was erased.")
        for p in problems:
            print(f"  blocked: {p}")
        return EXIT_REFUSED

    from ..db import Ledger

    ledger = Ledger(env.cfg.data_path / "sentinel.db")
    try:
        if env.windows:
            from .prepare import WindowsUacRunner

            runner: Any = WindowsUacRunner()
        else:
            from .prepare import LinuxPkexecRunner, udisks_mount

            runner = LinuxPkexecRunner()
        ctx = PrepareContext(policy=env.policy, protected=env.protected, enumerate=env.enumerate, runner=runner,
                             audit=ledger, work_dir=env.cfg.data_path / "prepare")
        if not env.windows:
            ctx.mount_volume = udisks_mount
        if args.dry_run:
            label = next_label(env.policy, ledger, [x.disk for x in classes])
            plan = build_plan(ctx, c.disk, label, "00000000-0000-4000-8000-000000000000")
            print("\nDRY RUN - nothing will be executed. Script that would run elevated:\n")
            print(plan.script)
            return EXIT_OK

        print(f"\nTHIS WILL ERASE EVERYTHING on: {c.disk.describe()}")
        if c.drive_class is DriveClass.FOREIGN_DATA:
            print(f"It currently holds about {human_size(c.used_bytes)} of other files.")
        admin = False
        method = "not requested"
        if c.needs_admin or args.admin:
            ok, method = os_administrator_check()
            if not args.admin:
                print("An administrator must approve erasing this drive: re-run with --admin as an administrator.")
                return EXIT_REFUSED
            if not ok:
                print("--admin was given but this account is not a computer administrator. Nothing was erased.")
                return EXIT_REFUSED
            admin = True
        want = serial_tail(c.disk.serial, env.policy.serial_confirm_chars)
        if not interactive or args.yes:
            if not (args.yes and args.serial_tail):
                print("Refusing to erase without a person confirming: give both --yes and --serial-tail. "
                      "Nothing was erased.")
                return EXIT_REFUSED
            typed = args.serial_tail.strip().upper()
            if typed != want:
                print("The serial number characters given do not match this drive. Nothing was erased.")
                return EXIT_REFUSED
        elif c.needs_serial:
            typed = _ask(f"Type the last {env.policy.serial_confirm_chars} characters of the serial number "
                         "shown above to erase it: ", reader).upper()
        else:
            if _ask("Type ERASE to erase this drive and set it up: ", reader) != "ERASE":
                print("Not confirmed. Nothing was erased.")
                return EXIT_REFUSED
            typed = None
        who = args.who or _default_who()
        conf = Confirmation.for_classification(c, typed)
        auth = Authorization(who=who, administrator=admin, method=method)
        if env.windows:
            print("A Windows permission prompt will appear. Approve it to continue.")
        from .prepare import prepare_drive

        try:
            res = prepare_drive(ctx, c.disk.disk_id, conf, auth)
        except PrepareRefused as exc:
            print(f"\n{_alert_line(exc.code)}\n{exc}")
            return EXIT_REFUSED
        except PrepareFailed as exc:
            print(f"\n{_alert_line(exc.code)}\n{exc}")
            return EXIT_FAILED
        print(f"\nDrive erased and set up: {res.label} ({res.filesystem}). Write test passed "
              f"({'cache bypassed' if res.write_test.get('uncached') else 'cache NOT bypassed'}).")
        return EXIT_OK
    finally:
        ledger.close()


def _default_who() -> str:
    from .prepare import _os_user

    return _os_user()


def cmd_eject(env: _Env, args: argparse.Namespace) -> int:
    from .eject import safe_remove

    disks = env.enumerate()
    d = next((x for x in disks if x.disk_id == args.disk), None)
    if d is None:
        raise SystemExit(f"no disk {args.disk!r}")
    try:
        res = safe_remove(d, disks, env.protected, enumerate_fn=env.enumerate)
    except DriveError as exc:
        print(f"{_alert_line(exc.code)}\n{exc}")
        return EXIT_REFUSED
    print(res.message if res.ok else f"{_alert_line('M-007')}\n{res.message}")
    if args.verbose:
        print(json.dumps(res.steps, indent=2))
    return EXIT_OK if res.ok else EXIT_FAILED


def cmd_snapshot(env: _Env, args: argparse.Namespace) -> int:
    if env.windows:
        from .windows_enum import capture_windows_snapshot

        print(json.dumps(capture_windows_snapshot(), indent=1, sort_keys=True))
    else:
        from .linux_enum import capture_lsblk

        print(json.dumps(capture_lsblk(), indent=1, sort_keys=True))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m sentinel.drives", description="Sentinel drive tools (administrators)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "classify", "prepare", "eject", "snapshot"):
        p = sub.add_parser(name)
        p.add_argument("--config", help="Sentinel TOML configuration (storage locations, [drives] policy)")
        if name in ("list", "classify"):
            p.add_argument("--json", action="store_true")
        if name in ("classify", "prepare", "eject"):
            p.add_argument("--disk", required=True, help="disk identifier from 'list'")
        if name == "prepare":
            p.add_argument("--who", help="name/ID of the person erasing the drive (recorded in the audit trail)")
            p.add_argument("--admin", action="store_true", help="act as administrator (verified against the OS)")
            p.add_argument("--yes", action="store_true", help="non-interactive confirmation (needs --serial-tail)")
            p.add_argument("--serial-tail", help="last characters of the drive's serial number")
            p.add_argument("--dry-run", action="store_true", help="print the elevated script; change nothing")
        if name == "eject":
            p.add_argument("--verbose", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = _Env(args)
    try:
        return {"list": cmd_list, "classify": cmd_classify, "prepare": cmd_prepare, "eject": cmd_eject,
                "snapshot": cmd_snapshot}[args.cmd](env, args)
    except DriveError as exc:
        print(f"{_alert_line(exc.code)}\n{exc}", file=sys.stderr)
        return EXIT_FAILED

