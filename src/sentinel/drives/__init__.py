"""Archive/review drive detection, classification, preparation and safe removal (S2, S3, U4, U9).

Design reference: docs/04_EXPORT_STATE_MACHINE.md §4–5. Notes and open decisions:
docs/branches/design-drive-prepare.md.

Nothing in this package formats a disk automatically. Preparation happens only through
``prepare.prepare_drive`` with an explicit confirmation bound to the exact disk identity that was
shown to the person confirming, and every guard is re-checked immediately before the destructive
step (also inside the elevated helper itself).
"""
