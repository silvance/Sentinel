"""Review-drive export (docs/04 §6; requirements U3, U4, S5, E2, E4, E7).

Runs as its own process (``python -m sentinel.export run``), never inside the recorder, so a
crashing or stuck export cannot interrupt collection. It reads sealed originals from the local
spool, writes to a mounted removable drive (an :class:`~sentinel.export.target.ExportTarget`),
and records every file it writes and verifies in the ledger (``drives``, ``export_batches``,
``export_items``).

What it never does: delete or modify local recordings, overwrite a file already on the drive,
claim a file is verified without reading it back from the drive, format/enumerate drives
(``sentinel.drives`` owns that), or claim forensic validation.
"""
