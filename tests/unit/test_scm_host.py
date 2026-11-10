"""Windows service host protocol (ServiceMain / control handler / status reports) with a fake SCM,
plus the real ctypes calls that work without administrator rights."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from sentinel.winsvc import scm
from sentinel.winsvc.scm import (ERROR_CALL_NOT_IMPLEMENTED, ERROR_SERVICE_SPECIFIC_ERROR, NO_ERROR,
                                 SERVICE_CONTROL_INTERROGATE, SERVICE_CONTROL_PRESHUTDOWN, SERVICE_CONTROL_STOP,
                                 SERVICE_RUNNING, SERVICE_START_PENDING, SERVICE_STOP_PENDING, SERVICE_STOPPED,
                                 ServiceHost, ServiceStatus)


class FakeScm:
    def __init__(self) -> None:
        self.statuses: list[ServiceStatus] = []
        self.handler: Callable[[int, int], int] | None = None

    def run_dispatcher(self, name: str, service_main: Callable[[], None]) -> None:
        t = threading.Thread(target=service_main)   # the real SCM also uses its own thread
        t.start()
        t.join(30)

    def register_handler(self, name: str, handler: Callable[[int, int], int]) -> Any:
        self.handler = handler
        return 1234

    def set_status(self, handle: Any, status: ServiceStatus) -> None:
        assert handle == 1234
        self.statuses.append(status)


def _states(api: FakeScm) -> list[int]:
    return [s.state for s in api.statuses]


def test_clean_stop_sequence_and_exit_zero() -> None:
    api = FakeScm()
    stop = threading.Event()

    def run(host: ServiceHost) -> int:
        host.on_stop(stop.set)
        host.report_running()
        threading.Timer(0.2, lambda: api.handler(SERVICE_CONTROL_STOP, 0)).start()  # type: ignore[misc]
        assert stop.wait(5)
        time.sleep(0.3)        # "finalising the last segment"
        return 0

    host = ServiceHost("SentinelRecorder", run, api, checkpoint_interval_s=0.1)
    host.dispatch()
    states = _states(api)
    assert states[0] == SERVICE_START_PENDING and states[1] == SERVICE_RUNNING
    assert states[-1] == SERVICE_STOPPED and SERVICE_STOP_PENDING in states
    running = api.statuses[1]
    assert running.controls_accepted & scm.SERVICE_ACCEPT_STOP and running.controls_accepted & \
        scm.SERVICE_ACCEPT_PRESHUTDOWN and running.controls_accepted & scm.SERVICE_ACCEPT_SHUTDOWN
    pend = [s for s in api.statuses if s.state == SERVICE_STOP_PENDING]
    assert len(pend) >= 2 and [p.checkpoint for p in pend] == sorted({p.checkpoint for p in pend})  # increasing
    assert all(p.controls_accepted == 0 and p.wait_hint_ms > 0 for p in pend)
    assert api.statuses[-1].win32_exit_code == NO_ERROR and host.exit_code == 0


def test_failure_exit_is_reported_as_service_specific_error_for_restart() -> None:
    api = FakeScm()
    ServiceHost("S", lambda h: (h.report_running(), 5)[1], api).dispatch()      # e.g. watchdog code 5
    last = api.statuses[-1]
    assert last.state == SERVICE_STOPPED and last.win32_exit_code == ERROR_SERVICE_SPECIFIC_ERROR
    assert last.service_exit_code == 5


def test_exception_in_service_still_reports_stopped_with_error() -> None:
    api = FakeScm()

    def boom(host: ServiceHost) -> int:
        raise RuntimeError("x")
    ServiceHost("S", boom, api).dispatch()
    assert api.statuses[-1].state == SERVICE_STOPPED and api.statuses[-1].service_exit_code == 4


def test_nonzero_exit_after_requested_stop_is_not_a_failure() -> None:
    api = FakeScm()

    def run(host: ServiceHost) -> int:
        host.report_running()
        api.handler(SERVICE_CONTROL_PRESHUTDOWN, 0)  # type: ignore[misc]
        return 4
    ServiceHost("S", run, api).dispatch()
    assert api.statuses[-1].state == SERVICE_STOPPED and api.statuses[-1].win32_exit_code == NO_ERROR


def test_stop_before_recorder_exists_is_delivered_when_it_registers() -> None:
    api = FakeScm()
    got = threading.Event()

    def run(host: ServiceHost) -> int:
        api.handler(SERVICE_CONTROL_STOP, 0)  # type: ignore[misc]   # arrives during config loading
        host.on_stop(got.set)
        host.report_running()                   # must not claim RUNNING after a stop request
        return 0
    ServiceHost("S", run, api).dispatch()
    assert got.is_set() and SERVICE_RUNNING not in _states(api)


def test_control_codes() -> None:
    host = ServiceHost("S", lambda h: 0, FakeScm())
    assert host.handle_control(SERVICE_CONTROL_INTERROGATE) == NO_ERROR
    assert host.handle_control(0x2) == ERROR_CALL_NOT_IMPLEMENTED       # pause: not supported
    assert not host.stop_requested.is_set()


def test_stop_deadline_ends_a_hung_stop() -> None:
    api = FakeScm()
    exits: list[int] = []
    release = threading.Event()

    def run(host: ServiceHost) -> int:
        host.report_running()
        api.handler(SERVICE_CONTROL_STOP, 0)  # type: ignore[misc]
        release.wait(5)     # the recorder never finishes stopping
        return 0
    host = ServiceHost("S", run, api, stop_deadline_s=0.3, checkpoint_interval_s=0.05,
                       exit_fn=lambda c: (exits.append(c), release.set()))
    host.dispatch()
    assert exits == [scm.EXIT_STOP_TIMEOUT]


win = pytest.mark.skipif(sys.platform != "win32", reason="real SCM calls")


@win
def test_real_dispatcher_outside_scm_raises_not_started_by_scm() -> None:
    with pytest.raises(scm.NotStartedByScm):
        scm.CtypesScmApi().run_dispatcher("SentinelRecorder", lambda: None)


@win
def test_real_query_service_without_admin() -> None:
    info = scm.query_service("W32Time")       # present on every Windows installation
    assert info is not None and info.binary_path and info.state in scm.STATE_NAMES
    assert scm.query_service("SentinelNoSuchService-xyz") is None
