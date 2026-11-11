"""Change sections of the configuration file from the window.

Only the edited sections are rewritten; every other line (comments, other sections) is kept.
The result is validated as a whole before anything is written, the previous file is kept as a
dated ``.bak``, and the new file replaces the old one in one rename.
"""

from __future__ import annotations

import os
import re
import shutil
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...config import AppConfig, ConfigError, load_config_dict

_HEADER = re.compile(r"^\s*(\[{1,2})\s*([^\]]+?)\s*\]{1,2}\s*(?:#.*)?$")


class ConfigEditError(Exception):
    """Shown to the person; nothing was changed."""


def toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        out = []
        for ch in v:
            if ch == "\\":
                out.append("\\\\")
            elif ch == '"':
                out.append('\\"')
            elif ord(ch) < 0x20 or ord(ch) == 0x7F:
                out.append(f"\\u{ord(ch):04x}")
            else:
                out.append(ch)
        return '"' + "".join(out) + '"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def render_section(name: str, table: dict[str, Any]) -> list[str]:
    """``[name]`` with its plain values, then ``[[name.key]]`` blocks for lists of tables."""
    lines = [f"[{name}]"]
    subs: list[tuple[str, list[dict[str, Any]]]] = []
    for k, v in table.items():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            subs.append((k, v))
        elif isinstance(v, list) and not v and k in ("weekly", "dates"):
            continue
        elif isinstance(v, dict):
            raise TypeError(f"nested table {name}.{k} is not supported")
        else:
            lines.append(f"{k} = {toml_value(v)}")
    for k, items in subs:
        for item in items:
            lines.append("")
            lines.append(f"[[{name}.{k}]]")
            lines.extend(f"{ik} = {toml_value(iv)}" for ik, iv in item.items())
    return lines


def _belongs(header: str, name: str) -> bool:
    return header == name or header.startswith(name + ".")


def replace_sections(text: str, tables: dict[str, dict[str, Any]]) -> str:
    """``text`` with each named section (and its ``[name.*]`` / ``[[name.*]]`` sub-tables) replaced.
    A section that is not in the file yet is added at the end."""
    lines = text.splitlines()
    for name, table in tables.items():
        heads = [(i, m.group(2)) for i, ln in enumerate(lines) if (m := _HEADER.match(ln))]
        blocks: list[tuple[int, int]] = []
        for n, (i, h) in enumerate(heads):
            if not _belongs(h, name):
                continue
            end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
            # Comment/blank lines directly above the next header stay with that header.
            if n + 1 < len(heads) and not _belongs(heads[n + 1][1], name):
                while end - 1 > i and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
                    end -= 1
            blocks.append((i, end))
        new = render_section(name, table)
        if not blocks:
            lines = lines + ([""] if lines and lines[-1].strip() else []) + new
            continue
        first = blocks[0][0]
        for s, e in reversed(blocks):
            del lines[s:e]
        lines[first:first] = new + ([""] if first < len(lines) and lines[first].strip() else [])
    return "\n".join(lines).rstrip("\n") + "\n"


def current_tables(cfg_path: Path) -> dict[str, Any]:
    return tomllib.loads(Path(cfg_path).read_text(encoding="utf-8"))


def check_text(cfg_path: Path, text: str) -> AppConfig:
    try:
        return load_config_dict(tomllib.loads(text), base_dir=Path(cfg_path).parent, source=str(cfg_path))
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        raise ConfigEditError(str(exc)) from None


def save_sections(cfg_path: Path, tables: dict[str, dict[str, Any]],
                  clock: Callable[[], float] = time.time) -> tuple[AppConfig, Path]:
    """Validate and write. Returns (new configuration, backup path)."""
    cfg_path = Path(cfg_path)
    text = cfg_path.read_text(encoding="utf-8")
    new_text = replace_sections(text, tables)
    new_cfg = check_text(cfg_path, new_text)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(clock()))
    backup = cfg_path.with_name(f"{cfg_path.name}.{stamp}.bak")
    n = 1
    while backup.exists():                    # two saves in one second keep both backups
        backup = cfg_path.with_name(f"{cfg_path.name}.{stamp}-{n}.bak")
        n += 1
    tmp = cfg_path.with_name(cfg_path.name + ".partial")
    # The live file is never moved away: copy it to the backup, then replace it in one atomic step
    # (review 2026-11-10: a failed second rename used to leave no configuration at all).
    try:
        shutil.copy2(cfg_path, backup)
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, cfg_path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConfigEditError(f"Could not write {cfg_path}: {exc}. The settings were not changed.") from None
    return new_cfg, backup
