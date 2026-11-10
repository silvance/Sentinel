"""Windows clock-synchronisation check (``w32tm /query /status``) parser."""

from __future__ import annotations

import sys

import pytest

from sentinel.platform.windows import parse_w32tm_status, query_w32time

# REAL, captured on the development host (Windows 11 Home 10.0.26200, W32Time stopped, not admin)
# with subprocess.run(["w32tm", "/query", "/status"], capture_output=True):
CAPTURED_RC_NOT_STARTED = 2147943462            # 0x80070426
CAPTURED_OUT_NOT_STARTED = "The following error occurred: The service has not been started. (0x80070426)\n"

# NOT CAPTURED: the development host's time service is stopped and starting it needs admin. These
# follow the documented English output layout of `w32tm /query /status` and must be replaced by
# real captures from the deployment machine (docs/branches/design-windows-service.md, "Not verified").
DOC_SYNCED = """Leap Indicator: 0(no warning)
Stratum: 4 (secondary reference - syncd by (S)NTP)
Precision: -23 (119.209ns per tick)
Root Delay: 0.0312500s
Root Dispersion: 7.7799116s
ReferenceId: 0xC0000201 (source IP:  192.0.2.1)
Last Successful Sync Time: 11/9/2026 10:15:22 AM
Source: ntp.example.internal,0x9
Poll Interval: 10 (1024s)
"""
DOC_NEVER_SYNCED = """Leap Indicator: 3(not synchronized)
Stratum: 0 (unspecified)
Precision: -23 (119.209ns per tick)
Root Delay: 0.0000000s
Root Dispersion: 0.0000000s
ReferenceId: 0x00000000 (unspecified)
Last Successful Sync Time: unspecified
Source: Local CMOS Clock
Poll Interval: 6 (64s)
"""


def test_captured_service_not_started_is_not_synchronised() -> None:
    st = parse_w32tm_status(CAPTURED_RC_NOT_STARTED, CAPTURED_OUT_NOT_STARTED)
    assert st.synchronized is False and "not running" in st.reason
    # Signed exit code (as PowerShell shows it) and the HRESULT in text are recognised too.
    assert parse_w32tm_status(-2147023834, "").synchronized is False
    assert parse_w32tm_status(1, "... (0x80070426)").synchronized is False


def test_access_denied_is_unknown() -> None:
    assert parse_w32tm_status(0x80070005, "The following error occurred: Access is denied. (0x80070005)").synchronized \
        is None


def test_documented_synchronised_layout() -> None:
    st = parse_w32tm_status(0, DOC_SYNCED)
    assert st.synchronized is True and st.stratum == 4 and st.leap_indicator == 0
    assert st.source == "ntp.example.internal,0x9"


def test_documented_never_synchronised_layout() -> None:
    st = parse_w32tm_status(0, DOC_NEVER_SYNCED)
    assert st.synchronized is False and st.leap_indicator == 3


@pytest.mark.parametrize("change,expected", [
    (("Source: ntp.example.internal,0x9", "Source: Free-running System Clock"), False),
    (("Stratum: 4 (secondary reference - syncd by (S)NTP)", "Stratum: 16 (unsynchronized)"), False),
    (("Last Successful Sync Time: 11/9/2026 10:15:22 AM", "Last Successful Sync Time: unspecified"), False),
])
def test_local_clock_or_invalid_stratum_is_not_synchronised(change: tuple[str, str], expected: bool) -> None:
    assert parse_w32tm_status(0, DOC_SYNCED.replace(*change)).synchronized is expected


def test_unrecognised_output_is_unknown_not_a_guess() -> None:
    german = "Sprungindikator: 0(keine Warnung)\nStratum-Ebene: 4\nQuelle: ntp.example.internal\n"
    assert parse_w32tm_status(0, german).synchronized is None
    assert parse_w32tm_status(0x8000FFFF, "").synchronized is None


def test_test_camera_warning_stays_the_banner_when_clock_is_not_synchronised() -> None:
    from sentinel.alerts import most_severe
    from sentinel.models import StorageLevel
    from sentinel.status import Measurements, evaluate

    m = Measurements(now_mono=100.0, service_started_mono=0.0, ffmpeg_running=True, media_seen_this_run=True,
                     seconds_since_media_advance=0.5, seconds_since_last_seal=1.0, storage_level=StorageLevel.OK,
                     storage_error=None, component_errors=[], in_backoff=False, clock_synchronized=False,
                     simulated=True)
    d = evaluate(m, stall_timeout_s=6, segment_seconds=2, connect_timeout_s=10)
    assert [a.code for a in d.alerts] == ["Z-001", "T-002"]
    assert most_severe(d.alerts).code == "Z-001"   # type: ignore[union-attr]


@pytest.mark.skipif(sys.platform != "win32", reason="runs the real w32tm")
def test_real_query_returns_a_reasoned_answer() -> None:
    st = query_w32time()
    assert st.reason   # value depends on the machine; on the dev host: False, "service is not running"
