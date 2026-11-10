"""Windows Service Control Manager (SCM) integration through ctypes (no pywin32).

Two parts:

* :class:`ServiceHost` — the service-side protocol: ServiceMain, the control handler, and
  status reporting (START_PENDING → RUNNING → STOP_PENDING with checkpoints → STOPPED). It talks
  to the SCM only through a small API object, so the protocol logic is unit-tested with a fake;
  :class:`CtypesScmApi` is the real implementation.
* Query/configure helpers used by ``install``/``status``: :func:`query_service`,
  :func:`set_preshutdown_timeout`, :func:`wait_for_state`, :func:`is_admin`.

Process model: the recorder runs inside the service process (the ServiceMain thread), so a
stop / pre-shutdown control calls ``RecorderService.request_stop`` directly and FFmpeg receives
its graceful ``q`` — the last segment is finalised. A service has no console, so console control
events (CTRL_BREAK) cannot be used for this; that is why a wrapper that launches
``sentinel.service run`` as a child process was not chosen.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

SERVICE_WIN32_OWN_PROCESS = 0x10
SERVICE_STOPPED, SERVICE_START_PENDING, SERVICE_STOP_PENDING, SERVICE_RUNNING = 1, 2, 3, 4
STATE_NAMES = {1: "STOPPED", 2: "START_PENDING", 3: "STOP_PENDING", 4: "RUNNING", 5: "CONTINUE_PENDING",
               6: "PAUSE_PENDING", 7: "PAUSED"}
START_TYPE_NAMES = {0: "boot", 1: "system", 2: "automatic", 3: "manual", 4: "disabled"}
SERVICE_ACCEPT_STOP = 0x1
SERVICE_ACCEPT_SHUTDOWN = 0x4
SERVICE_ACCEPT_PRESHUTDOWN = 0x100
SERVICE_CONTROL_STOP = 0x1
SERVICE_CONTROL_INTERROGATE = 0x4
SERVICE_CONTROL_SHUTDOWN = 0x5
SERVICE_CONTROL_PRESHUTDOWN = 0xF
NO_ERROR = 0
ERROR_CALL_NOT_IMPLEMENTED = 120
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063
ERROR_SERVICE_SPECIFIC_ERROR = 1066
SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_CONFIG = 0x0001
SERVICE_CHANGE_CONFIG = 0x0002
SERVICE_QUERY_STATUS = 0x0004
SC_STATUS_PROCESS_INFO = 0
SERVICE_CONFIG_PRESHUTDOWN_INFO = 7

# Exit code used when a requested stop has not completed within the host's stop deadline.
EXIT_STOP_TIMEOUT = 6


class NotStartedByScm(RuntimeError):
    """``host`` was run from a console instead of by the Service Control Manager."""


@dataclass(frozen=True)
class ServiceStatus:
    state: int
    controls_accepted: int = 0
    win32_exit_code: int = NO_ERROR
    service_exit_code: int = 0
    checkpoint: int = 0
    wait_hint_ms: int = 0


class ScmApi(Protocol):
    def run_dispatcher(self, service_name: str, service_main: Callable[[], None]) -> None: ...
    def register_handler(self, service_name: str, handler: Callable[[int, int], int]) -> Any: ...
    def set_status(self, handle: Any, status: ServiceStatus) -> None: ...


class ServiceHost:
    """The SCM protocol around one long-running function.

    ``run_service(host)`` loads configuration, constructs the recorder, registers its stop
    callback with :meth:`on_stop`, calls :meth:`report_running`, and returns the recorder's exit
    code when it ends. The host maps that code to the SCM's exit fields: 0 → clean stop (no
    restart); anything else → ``ERROR_SERVICE_SPECIFIC_ERROR`` + the code, which with
    ``failureflag 1`` triggers the configured restart.
    """

    ACCEPTS = SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN | SERVICE_ACCEPT_PRESHUTDOWN

    def __init__(self, service_name: str, run_service: Callable[[ServiceHost], int], api: ScmApi,
                 stop_deadline_s: float = 600.0, checkpoint_interval_s: float = 2.0,
                 exit_fn: Callable[[int], None] | None = None) -> None:
        self.service_name = service_name
        self._run_service = run_service
        self._api = api
        self._handle: Any = None
        self._lock = threading.Lock()
        self._checkpoint = 0
        self._stop_callbacks: list[Callable[[], None]] = []
        self.stop_requested = threading.Event()
        self.finished = threading.Event()
        self._stop_deadline_s = stop_deadline_s
        self._checkpoint_interval_s = checkpoint_interval_s
        self._exit_fn = exit_fn or os._exit
        self.reported: list[ServiceStatus] = []      # history, for diagnostics and tests
        self.exit_code: int | None = None

    # ------------------------------------------------------------------ SCM entry points
    def dispatch(self) -> None:
        """Block in the SCM dispatcher until the service has stopped (main thread)."""
        self._api.run_dispatcher(self.service_name, self.service_main)

    def service_main(self) -> None:
        """Called by the SCM on its own thread."""
        try:
            self._handle = self._api.register_handler(self.service_name, self.handle_control)
        except Exception:
            log.exception("RegisterServiceCtrlHandlerEx failed")
            return
        self._report(SERVICE_START_PENDING, wait_hint_ms=30_000)
        code = 1
        try:
            code = int(self._run_service(self))
        except BaseException:  # noqa: BLE001 - anything escaping must still report STOPPED
            log.exception("service failed")
            code = 4
        finally:
            self.exit_code = code
            self.finished.set()
            self._report_stopped(code)

    def handle_control(self, control: int, event_type: int = 0, *_: Any) -> int:
        """HandlerEx. Must return quickly: it runs on the dispatcher thread."""
        if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN, SERVICE_CONTROL_PRESHUTDOWN):
            self._begin_stop(control)
            return NO_ERROR
        if control == SERVICE_CONTROL_INTERROGATE:
            return NO_ERROR
        return ERROR_CALL_NOT_IMPLEMENTED

    # ------------------------------------------------------------------ used by run_service
    def report_running(self) -> None:
        if not self.stop_requested.is_set():
            self._report(SERVICE_RUNNING, controls=self.ACCEPTS)

    def on_stop(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._stop_callbacks.append(callback)
            already = self.stop_requested.is_set()
        if already:
            callback()

    # ------------------------------------------------------------------ internals
    def _begin_stop(self, control: int) -> None:
        with self._lock:
            if self.stop_requested.is_set():
                return
            self.stop_requested.set()
            callbacks = list(self._stop_callbacks)
        log.info("service control %#x: stopping", control)
        self._report(SERVICE_STOP_PENDING, wait_hint_ms=int(self._checkpoint_interval_s * 5000))
        for cb in callbacks:
            try:
                cb()
            except Exception:
                log.exception("stop callback failed")
        threading.Thread(target=self._stop_progress, name="scm-stop-progress", daemon=True).start()

    def _stop_progress(self) -> None:
        """Keep the SCM informed while the recorder finalises; enforce a last-resort deadline."""
        deadline = time.monotonic() + self._stop_deadline_s
        while not self.finished.wait(self._checkpoint_interval_s):
            if time.monotonic() >= deadline:
                log.critical("service did not stop within %.0f s; ending the process", self._stop_deadline_s)
                self._exit_fn(EXIT_STOP_TIMEOUT)
                return
            self._report(SERVICE_STOP_PENDING, wait_hint_ms=int(self._checkpoint_interval_s * 5000))

    def _report_stopped(self, code: int) -> None:
        if code == 0 or self.stop_requested.is_set():
            # A stop that was asked for is never a "failure": reporting one would make the SCM
            # restart a service an administrator deliberately stopped. The real code is in the log
            # and the ledger's service_stop event.
            if code != 0:
                log.warning("recorder exited with code %d after a stop request", code)
            self._report(SERVICE_STOPPED)
        else:
            self._report(SERVICE_STOPPED, win32_exit=ERROR_SERVICE_SPECIFIC_ERROR, service_exit=code)

    def _report(self, state: int, controls: int = 0, wait_hint_ms: int = 0, win32_exit: int = NO_ERROR,
                service_exit: int = 0) -> None:
        with self._lock:
            pending = state in (SERVICE_START_PENDING, SERVICE_STOP_PENDING)
            self._checkpoint = self._checkpoint + 1 if pending else 0
            if state == SERVICE_STOP_PENDING:
                controls = 0
            st = ServiceStatus(state, controls, win32_exit, service_exit, self._checkpoint if pending else 0,
                               wait_hint_ms if pending else 0)
            self.reported.append(st)
            if self._handle is None:
                return
            try:
                self._api.set_status(self._handle, st)
            except Exception:
                log.exception("SetServiceStatus failed")


# ---------------------------------------------------------------------- ctypes implementation
class _SERVICE_STATUS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in (
        "dwServiceType", "dwCurrentState", "dwControlsAccepted", "dwWin32ExitCode",
        "dwServiceSpecificExitCode", "dwCheckPoint", "dwWaitHint")]


class _SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in (
        "dwServiceType", "dwCurrentState", "dwControlsAccepted", "dwWin32ExitCode",
        "dwServiceSpecificExitCode", "dwCheckPoint", "dwWaitHint", "dwProcessId", "dwServiceFlags")]


class _QUERY_SERVICE_CONFIGW(ctypes.Structure):
    _fields_ = [("dwServiceType", ctypes.c_uint32), ("dwStartType", ctypes.c_uint32),
                ("dwErrorControl", ctypes.c_uint32), ("lpBinaryPathName", ctypes.c_wchar_p),
                ("lpLoadOrderGroup", ctypes.c_wchar_p), ("dwTagId", ctypes.c_uint32),
                ("lpDependencies", ctypes.c_wchar_p), ("lpServiceStartName", ctypes.c_wchar_p),
                ("lpDisplayName", ctypes.c_wchar_p)]


class _SERVICE_PRESHUTDOWN_INFO(ctypes.Structure):
    _fields_ = [("dwPreshutdownTimeout", ctypes.c_uint32)]


def _advapi():  # noqa: ANN202
    if sys.platform != "win32":
        raise OSError("the Service Control Manager is only available on Windows")
    from ctypes import wintypes

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    adv.OpenSCManagerW.restype = ctypes.c_void_p
    adv.OpenServiceW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD]
    adv.OpenServiceW.restype = ctypes.c_void_p
    adv.CloseServiceHandle.argtypes = [ctypes.c_void_p]
    adv.CloseServiceHandle.restype = wintypes.BOOL
    adv.QueryServiceStatusEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]
    adv.QueryServiceStatusEx.restype = wintypes.BOOL
    adv.QueryServiceConfigW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD)]
    adv.QueryServiceConfigW.restype = wintypes.BOOL
    adv.ChangeServiceConfig2W.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    adv.ChangeServiceConfig2W.restype = wintypes.BOOL
    adv.SetServiceStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(_SERVICE_STATUS)]
    adv.SetServiceStatus.restype = wintypes.BOOL
    return adv


SERVICE_MAIN_FUNCTION = ctypes.WINFUNCTYPE(None, ctypes.c_uint32, ctypes.c_void_p) \
    if sys.platform == "win32" else None
HANDLER_FUNCTION_EX = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                         ctypes.c_void_p) if sys.platform == "win32" else None


class CtypesScmApi:
    """Real SCM calls. Callback objects are kept on ``self`` so they are not garbage-collected
    while Windows holds pointers to them."""

    def __init__(self) -> None:
        self._adv = _advapi()
        self._keep: list[Any] = []

    def run_dispatcher(self, service_name: str, service_main: Callable[[], None]) -> None:
        class _ENTRY(ctypes.Structure):
            _fields_ = [("lpServiceName", ctypes.c_wchar_p), ("lpServiceProc", SERVICE_MAIN_FUNCTION)]

        def _main(_argc: int, _argv: int) -> None:
            try:
                service_main()
            except BaseException:  # noqa: BLE001 - never unwind into the SCM
                log.exception("ServiceMain failed")

        cb = SERVICE_MAIN_FUNCTION(_main)
        name = ctypes.create_unicode_buffer(service_name)
        table = (_ENTRY * 2)()
        table[0].lpServiceName = ctypes.cast(name, ctypes.c_wchar_p)
        table[0].lpServiceProc = cb
        self._keep += [cb, name, table]
        self._adv.StartServiceCtrlDispatcherW.argtypes = [ctypes.c_void_p]
        self._adv.StartServiceCtrlDispatcherW.restype = ctypes.c_int
        if not self._adv.StartServiceCtrlDispatcherW(ctypes.byref(table)):
            err = ctypes.get_last_error()
            if err == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
                raise NotStartedByScm("not started by the Windows Service Control Manager")
            raise OSError(err, f"StartServiceCtrlDispatcher failed (Windows error {err})")

    def register_handler(self, service_name: str, handler: Callable[[int, int], int]) -> Any:
        def _h(control: int, event_type: int, _data: int, _ctx: int) -> int:
            try:
                return int(handler(control, event_type))
            except BaseException:  # noqa: BLE001
                log.exception("service control handler failed")
                return NO_ERROR
        cb = HANDLER_FUNCTION_EX(_h)
        self._keep.append(cb)
        self._adv.RegisterServiceCtrlHandlerExW.argtypes = [ctypes.c_wchar_p, HANDLER_FUNCTION_EX, ctypes.c_void_p]
        self._adv.RegisterServiceCtrlHandlerExW.restype = ctypes.c_void_p
        handle = self._adv.RegisterServiceCtrlHandlerExW(service_name, cb, None)
        if not handle:
            err = ctypes.get_last_error()
            raise OSError(err, f"RegisterServiceCtrlHandlerEx failed (Windows error {err})")
        return handle

    def set_status(self, handle: Any, status: ServiceStatus) -> None:
        st = _SERVICE_STATUS(SERVICE_WIN32_OWN_PROCESS, status.state, status.controls_accepted,
                             status.win32_exit_code, status.service_exit_code, status.checkpoint,
                             status.wait_hint_ms)
        if not self._adv.SetServiceStatus(handle, ctypes.byref(st)):
            err = ctypes.get_last_error()
            raise OSError(err, f"SetServiceStatus failed (Windows error {err})")


# ---------------------------------------------------------------------- query / configure
@dataclass(frozen=True)
class ServiceInfo:
    name: str
    state: int
    pid: int
    win32_exit_code: int
    service_exit_code: int
    start_type: int
    binary_path: str
    account: str
    display_name: str

    @property
    def state_name(self) -> str:
        return STATE_NAMES.get(self.state, str(self.state))

    @property
    def start_type_name(self) -> str:
        return START_TYPE_NAMES.get(self.start_type, str(self.start_type))


def query_service(name: str) -> ServiceInfo | None:
    """Status and configuration of an installed service; ``None`` if not installed.
    Needs no administrator rights (query access is granted to authenticated users)."""
    from ctypes import wintypes

    adv = _advapi()
    scm = adv.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not scm:
        err = ctypes.get_last_error()
        raise OSError(err, f"OpenSCManager failed (Windows error {err})")
    try:
        svc = adv.OpenServiceW(scm, name, SERVICE_QUERY_STATUS | SERVICE_QUERY_CONFIG)
        if not svc:
            err = ctypes.get_last_error()
            if err == ERROR_SERVICE_DOES_NOT_EXIST:
                return None
            raise OSError(err, f"OpenService({name}) failed (Windows error {err})")
        try:
            st = _SERVICE_STATUS_PROCESS()
            need = wintypes.DWORD()
            if not adv.QueryServiceStatusEx(svc, SC_STATUS_PROCESS_INFO, ctypes.byref(st), ctypes.sizeof(st),
                                            ctypes.byref(need)):
                err = ctypes.get_last_error()
                raise OSError(err, f"QueryServiceStatusEx failed (Windows error {err})")
            adv.QueryServiceConfigW(svc, None, 0, ctypes.byref(need))
            buf = ctypes.create_string_buffer(max(need.value, ctypes.sizeof(_QUERY_SERVICE_CONFIGW)))
            if not adv.QueryServiceConfigW(svc, buf, len(buf), ctypes.byref(need)):
                err = ctypes.get_last_error()
                raise OSError(err, f"QueryServiceConfig failed (Windows error {err})")
            cfg = _QUERY_SERVICE_CONFIGW.from_buffer(buf)
            return ServiceInfo(name, st.dwCurrentState, st.dwProcessId, st.dwWin32ExitCode,
                               st.dwServiceSpecificExitCode, cfg.dwStartType, cfg.lpBinaryPathName or "",
                               cfg.lpServiceStartName or "", cfg.lpDisplayName or "")
        finally:
            adv.CloseServiceHandle(svc)
    finally:
        adv.CloseServiceHandle(scm)


def set_preshutdown_timeout(name: str, timeout_ms: int) -> None:
    """ChangeServiceConfig2(SERVICE_CONFIG_PRESHUTDOWN_INFO). Needs administrator rights.
    ``sc.exe`` has no sub-command for this setting, hence the API call."""
    adv = _advapi()
    scm = adv.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not scm:
        err = ctypes.get_last_error()
        raise OSError(err, f"OpenSCManager failed (Windows error {err})")
    try:
        svc = adv.OpenServiceW(scm, name, SERVICE_CHANGE_CONFIG)
        if not svc:
            err = ctypes.get_last_error()
            raise OSError(err, f"OpenService({name}) for configuration failed (Windows error {err})")
        try:
            info = _SERVICE_PRESHUTDOWN_INFO(timeout_ms)
            if not adv.ChangeServiceConfig2W(svc, SERVICE_CONFIG_PRESHUTDOWN_INFO, ctypes.byref(info)):
                err = ctypes.get_last_error()
                raise OSError(err, f"ChangeServiceConfig2 failed (Windows error {err})")
        finally:
            adv.CloseServiceHandle(svc)
    finally:
        adv.CloseServiceHandle(scm)


def wait_for_state(name: str, state: int, timeout_s: float, poll_s: float = 1.0) -> ServiceInfo | None:
    end = time.monotonic() + timeout_s
    info = query_service(name)
    while info is not None and info.state != state and time.monotonic() < end:
        time.sleep(poll_s)
        info = query_service(name)
    return info


def is_admin() -> bool:
    if sys.platform != "win32":
        return hasattr(os, "geteuid") and os.geteuid() == 0  # type: ignore[attr-defined]
    try:
        return bool(ctypes.WinDLL("shell32").IsUserAnAdmin())
    except (OSError, AttributeError):
        return False
