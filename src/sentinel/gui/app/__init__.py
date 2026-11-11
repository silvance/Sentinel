"""The Sentinel window (owner request 2026-11-10): one compact desktop window in which every task can
be done, from first-run setup to camera, schedule, motion, drives, review export and the service.

Layout: a section list on the left, a page on the right, a status bar along the bottom.
Everyone sees every section. Changing anything needs Windows administrator rights: an operator
(standard user) sees the settings read-only; "Unlock" asks Windows for administrator approval
(UAC) and reopens the window elevated.

Actions reuse the command-line commands (the same checks, refusals and audit records): the window
runs them as child processes and shows their output. Settings are written by ``configedit``, which
replaces only the edited sections of the configuration file and keeps a dated .bak copy.
"""
