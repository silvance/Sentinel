# Recording schedule and unattended (closet) operation — 2026-11-10

Owner requests: (1) control recording where an agent cannot easily get to the recorder — set day
and time ranges ahead of time to turn recording on or off; (2) a laptop hidden in, for example, a
communications closet must not show that it is recording to someone who opens it, without
locking affecting recording.

**Declined:** a fake Windows 11 login screen. The project brief forbids "covert activation,
operating-system concealment, evasion, or stealth persistence", an imitation of a Windows
password prompt is not something we build, and it would be weaker than the real lock screen (it is
an ordinary window: Ctrl+Alt+Del, Task Manager or the power button get past it). The real Windows
lock does the job: the recorder is a Windows service and keeps recording while the computer is
locked, signed out, or with nobody signed in.

## 1. Recording schedule (`src/sentinel/schedule.py`, `[schedule]`)
* Weekly periods (days + start/end time; an end before the start runs past midnight) and one-off
  date periods (`"2026-12-24 18:00"`, or a date alone; as an end a date alone means the end of that
  day). Each says `record` or `pause`; outside all periods `default` applies. Precedence: dates >
  weekly > default. Local wall-clock time in `[schedule] timezone` (default: the display time zone);
  daylight-saving changes follow the wall clock.
* Refused at load (configuration error C-001, plain message naming the period): unknown keys or
  days, bad times/dates, start = end, end before start (dates), overlapping periods that disagree,
  a schedule that never records, more than 500 periods.
* `Sentinel.exe schedule --days 14` (and `check-config`) prints exactly when it will record and
  pause, so a technician can check a holiday plan before leaving.
* Recorder (`recorder/supervisor.py`): when the schedule says pause, FFmpeg is stopped the same way
  as a service stop (`q`, last file closed and sealed normally); the run ends with
  `scheduled_pause`; the finaliser opens a gap with that reason, closed by the first footage after
  the resume. Audit events `schedule_pause` (which period decided, resume time) and
  `schedule_resume`, in the hash-chained ledger. No retry back-off after a planned pause. If the
  schedule cannot be evaluated the recorder records (fail-safe).
* Status: overall `PAUSED` ("Paused by schedule", informational tone), alert **P-001** NOTICE
  "Recording is paused by the recording schedule (P-001). Recording resumes automatically on
  Mon 16 Nov 2026 06:00. No action is needed." No R-00x alert and no escalation during a pause; after
  the resume the normal start grace applies and an outage is measured from the scheduled restart.
  Other problems (storage, database, chain, archive) are still shown. `status.json` has a
  `schedule` section (paused, next change). Review exports and the GUI timeline word the gap
  "Recording was paused by the recording schedule (planned)".
* Archive copying, retention, integrity checks and the time checks keep running during a pause.

## 2. Unattended laptop (`src/sentinel/power.py`)
* **Keep-awake:** while the recorder service runs it holds `ES_SYSTEM_REQUIRED`
  (`SetThreadExecutionState`, own thread), so Windows does not idle-sleep or idle-hibernate. The
  screen may still turn off. It cannot prevent sleep from closing the lid or the power button.
* **Power plan at install:** `python -m sentinel.winsvc install` now also sets the active plan:
  never sleep / never hibernate on mains **and battery** (a power cut keeps it recording on the
  battery), lid closed = do nothing (mains and battery; may fail harmlessly on desktops), screen
  off after 5 min (mains) / 2 min (battery). `--keep-power-settings` skips this.
* **H-001** WARNING "This computer is set to go to sleep, which would stop recording. Recording
  continues for now. Do not close the lid. Contact technical support…": checked at start and every
  10 min from the active plan (`powrprof`: lid action if a lid is present; idle sleep/hibernate only
  if keep-awake failed). Event `sleep_risk` when the risk appears or changes; details in
  `status.json` → `power`.
* **"Lock this computer"** on the operator screen (Windows; in the built `Sentinel.exe` since the
  operator screen was bundled the same day): minimises the window and locks Windows
  (`LockWorkStation`, same as Windows key + L); tooltip says recording continues.
* **BitLocker** for the laptop's disk is in the deployment checklist (docs/07): a Windows setting,
  not something Sentinel switches on.

## 3. Tests (Windows 11, Python 3.14, LGPL FFmpeg 9.0.2)
* `tests/unit/test_schedule.py` (25): parsing, refusals, weekly/overnight/week wrap, date
  precedence, date-only ends, next change and plan, DST both directions, operator wording.
* `tests/unit/test_schedule_status.py` (9): configuration, PAUSED decision without outage alerts,
  other alerts still shown, start grace after resume, gap wording, P-001/H-001 text, H-001 raised
  and cleared by the service's power check.
* `tests/unit/test_power.py` (11): keep-awake holds and releases on its own thread, failure
  reported not raised, no-op off Windows, sleep-risk rules, install plan power steps (before the
  service starts; skipped with `--keep-power-settings`), reading this PC's real power plan.
* `tests/unit/test_gui_lock.py` (2): button text/tooltip, minimise + lock, restore on failure
  (the test never locks the machine).
* `tests/integration/test_schedule_service.py` (2, real recorder + simulated camera): pause on
  schedule with the file closed normally, PAUSED + P-001 with the resume time and no outage alert,
  nothing recorded while paused, automatic resume, one `scheduled_pause` gap from pause to resume,
  pause/resume events, chain verifies; and a service that starts inside a pause records nothing.

## 4. NOT executed / not verified
* Keep-awake under the real `NT SERVICE\SentinelRecorder` account, and the `powercfg` install steps
  (needs an administrator install). Lid detection (`GetPwrCapabilities`) was read only on a
  desktop (no lid); not yet on a laptop. Modern Standby laptops: lid action "do nothing" is
  expected to keep them running; NOT VERIFIED.
* A real multi-day schedule (only seconds-long periods were run end to end); DST transitions are
  unit-tested only.
* Linux: keep-awake is a no-op; `systemd-inhibit` in the unit is the equivalent (not done).
