"""Operator GUI (PySide6). Read-only window onto the recorder's measured state.

The GUI reads ``status.json`` (via :func:`sentinel.status.read_status`) and the ledger through a
read-only SQLite connection. It never starts, stops or signals the recorder and holds no handle to
it, so closing or misusing the GUI cannot affect recording (requirement U6).

Qt-free modules (``viewmodel``, ``ledger_view``, ``admin_pin``, ``report``, ``textfmt``) hold all
decisions so they can be unit-tested without a display; the Qt modules only render them.
"""
