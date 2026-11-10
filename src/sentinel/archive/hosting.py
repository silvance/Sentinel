"""How the archive process is hosted (so it is always running, restarts by itself, and is visible).

**Windows (planned, NOT EXECUTED — needs administrator rights and the service host from
``design/windows-service``).** The archive runs as a SECOND Windows service, ``SentinelArchive``,
next to ``SentinelRecorder``, using the same approach as that branch: the stdlib/ctypes service host
(the SCM requires a control handler; a plain ``python -m sentinel.archive run`` is not a service and
would be killed by the SCM after 30 s), a virtual service account ``NT SERVICE\\SentinelArchive``,
restart-forever failure actions, a restricted SID and a reduced privilege set. That host is not on
this branch, so :func:`windows_service_commands` takes the host command line as a parameter; after
the merge it is the host's entry point with the archive program selected.

The archive service needs: read access to the spool, read/write access to the data directory
(ledger, status, control files) and the log directory, and write access to the archive volume
(an NTFS drive prepared by Sentinel; its ACL must grant the service SID — to be set by drive
preparation, NOT verified here).

Until the service exists, the recorder's status shows A-007 ("Archive copying is not running"),
escalating to critical after ``[archive] not_running_critical_after_s`` (default 30 min), and the
backlog / A-004 are still computed by the recorder from the ledger.

**Linux:** ``packaging/systemd/sentinel-archive.service`` (draft, NOT EXECUTED).
"""

from __future__ import annotations

from pathlib import Path

SERVICE_NAME = "SentinelArchive"
DISPLAY_NAME = "Sentinel archive-drive copy"
ACCOUNT = f"NT SERVICE\\{SERVICE_NAME}"


def windows_service_commands(host_command: str, spool: Path, data: Path, logs: Path) -> list[list[str]]:
    """The commands an administrator-run installer would execute, as argv lists (never run here).
    ``host_command``: full command line of the service host process (quoted as needed)."""
    return [
        ["sc.exe", "create", SERVICE_NAME, "binPath=", host_command, "start=", "delayed-auto",
         "obj=", ACCOUNT, "DisplayName=", DISPLAY_NAME],
        ["sc.exe", "description", SERVICE_NAME,
         "Copies finished recordings to the archive drive and checks every copy. Never deletes recordings."],
        ["sc.exe", "failure", SERVICE_NAME, "reset=", "86400", "actions=", "restart/10000/restart/30000/restart/60000"],
        ["sc.exe", "failureflag", SERVICE_NAME, "1"],
        ["sc.exe", "sidtype", SERVICE_NAME, "unrestricted"],
        ["sc.exe", "privs", SERVICE_NAME, "SeChangeNotifyPrivilege"],
        ["icacls", str(spool), "/grant", f"{ACCOUNT}:(OI)(CI)RX"],
        ["icacls", str(data), "/grant", f"{ACCOUNT}:(OI)(CI)M"],
        ["icacls", str(logs), "/grant", f"{ACCOUNT}:(OI)(CI)M"],
    ]
