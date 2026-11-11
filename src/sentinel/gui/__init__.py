"""The Sentinel window (PySide6): status, recordings, settings and actions in one window.

See ``sentinel.gui.app`` for the window itself. It reads ``status.json`` and a read-only copy of
the ledger; opening or closing it never starts or stops the recorder. Changing settings and
running actions needs Windows administrator rights and reuses the command-line commands.

Qt-free modules (``viewmodel``, ``ledger_view``, ``report``, ``textfmt``) hold the decisions so they
can be unit-tested without a display.
"""
