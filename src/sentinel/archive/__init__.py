"""Continuous, verified archive copy to a swappable archive drive (requirement S1, docs/04 §2-3).

Modules: ``worker`` (the copy loop and swap workflow), ``io`` (throttle + file primitives reused
from ``sentinel.export.fileio``), ``ledger`` (``archive_copies``), ``index`` (self-describing
``Sentinel/INDEX/``), ``layout`` (paths on the drive), ``status`` (``archive_status.json``,
A-0xx alerts), ``locator`` (which drive is attached), ``priority``, ``bench``, ``__main__`` (CLI).
Local recordings are never modified or deleted by anything in this package.
"""
