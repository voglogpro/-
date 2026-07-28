"""Copy Compose file-secrets into private tmpfs, then permanently drop root.

Standalone Compose may preserve root:0600 source permissions for file-backed
secrets.  This short launcher is the only root process: it opens the two exact
secret mounts, creates user-readable 0400 copies in a private tmpfs, prepares
the state directory, drops supplementary groups/GID/UID, and execs the fixed
watchdog command.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys


RUNTIME_UID = 10001
RUNTIME_GID = 10001
PRIVATE_DIR = Path("/run/bibitasks-monitor-private")
STATE_DIR = Path("/var/lib/bibitasks-monitor")
SOURCES = (
    (Path("/run/secrets/monitor_alert_bot_token"), PRIVATE_DIR / "alert-token"),
    (Path("/run/secrets/monitor_health_token"), PRIVATE_DIR / "health-token"),
)
ALLOWED_COMMAND = ("python", "scripts/pilot_monitor.py")


def _assert_privileges_dropped() -> None:
    status = Path("/proc/self/status").read_text("ascii")
    capabilities = {}
    for line in status.splitlines():
        if line.startswith(("CapEff:", "CapPrm:")):
            name, raw = line.split(":", 1)
            capabilities[name] = int(raw.strip(), 16)
    if capabilities != {"CapPrm": 0, "CapEff": 0}:
        raise RuntimeError("monitor launcher retained Linux capabilities")


def _secure_read(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        mode = stat.S_IMODE(info.st_mode)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or mode not in (0o400, 0o600)
        ):
            raise RuntimeError("monitor secret must be a root-owned regular file")
        if not _mount_is_read_only(path):
            raise RuntimeError("monitor secret must be mounted read-only")
        if info.st_size <= 0 or info.st_size > 4096:
            raise RuntimeError("monitor secret has an invalid size")
        value = os.read(descriptor, 4097)
        if len(value) != info.st_size:
            raise RuntimeError("monitor secret changed while being read")
        return value
    finally:
        os.close(descriptor)


def _mount_is_read_only(path: Path) -> bool:
    expected = str(path).replace(" ", "\\040")
    try:
        lines = Path("/proc/self/mountinfo").read_text("utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    for line in lines:
        fields = line.split()
        if len(fields) >= 6 and fields[4] == expected:
            return "ro" in fields[5].split(",")
    return False


def _private_write(path: Path, value: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o400)
    try:
        written = os.write(descriptor, value)
        if written != len(value):
            raise RuntimeError("short write while preparing monitor secret")
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
        os.fchown(descriptor, RUNTIME_UID, RUNTIME_GID)
    finally:
        os.close(descriptor)


def _runtime_directory_ready(path: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        info = path.stat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == RUNTIME_UID and info.st_gid == RUNTIME_GID
        and stat.S_IMODE(info.st_mode) == 0o700
    )


def _runtime_secret_ready(path: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        info = path.stat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == RUNTIME_UID and info.st_gid == RUNTIME_GID
        and stat.S_IMODE(info.st_mode) == 0o400
        and 0 < info.st_size <= 4096
    )


def _prepare_runtime_files() -> None:
    # PRIVATE_DIR is a fresh root-owned tmpfs on every container start.  Create
    # and close files before transferring directory ownership: container root
    # intentionally has no DAC_OVERRIDE/FOWNER capability.
    PRIVATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_info = PRIVATE_DIR.stat()
    if (
        PRIVATE_DIR.is_symlink() or not stat.S_ISDIR(private_info.st_mode)
        or private_info.st_uid != 0 or private_info.st_gid != 0
    ):
        raise RuntimeError("private monitor tmpfs must start root-owned")
    os.chmod(PRIVATE_DIR, 0o700)
    created = []
    try:
        for source, target in SOURCES:
            if target.exists() or target.is_symlink():
                raise RuntimeError("private monitor secret already exists")
            _private_write(target, _secure_read(source))
            created.append(target)
        if not all(_runtime_secret_ready(target) for _, target in SOURCES):
            raise RuntimeError("monitor runtime secrets were not prepared safely")
        os.chown(PRIVATE_DIR, RUNTIME_UID, RUNTIME_GID)
    except BaseException:
        # Cleanup is only possible while the root-owned directory has not been
        # transferred.  After a successful chown there is nothing left to do.
        if PRIVATE_DIR.stat().st_uid == 0:
            for target in created:
                target.unlink(missing_ok=True)
        raise

    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _runtime_directory_ready(STATE_DIR):
        info = STATE_DIR.stat()
        if (
            STATE_DIR.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0 or info.st_gid != 0
        ):
            raise RuntimeError("monitor state directory has unsafe ownership")
        os.chmod(STATE_DIR, 0o700)
        os.chown(STATE_DIR, RUNTIME_UID, RUNTIME_GID)
    if not _runtime_directory_ready(PRIVATE_DIR) or not _runtime_directory_ready(STATE_DIR):
        raise RuntimeError("monitor runtime directories were not prepared safely")


def _verify_runtime_files() -> None:
    if not _runtime_directory_ready(PRIVATE_DIR) or not _runtime_directory_ready(STATE_DIR):
        raise RuntimeError("monitor runtime directories are unavailable")
    if not all(_runtime_secret_ready(target) for _, target in SOURCES):
        raise RuntimeError("monitor runtime secrets are unavailable")


def prepare_and_drop(command, *, drop_only=False) -> None:
    if os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("monitor launcher must start as container root")
    if tuple(command[:2]) != ALLOWED_COMMAND:
        raise RuntimeError("monitor launcher refused an unexpected command")
    if drop_only:
        if not _runtime_directory_ready(PRIVATE_DIR) or not _runtime_directory_ready(STATE_DIR):
            raise RuntimeError("monitor runtime directories are unavailable")
    else:
        _prepare_runtime_files()
    os.environ["MONITOR_ALERT_TOKEN_FILE"] = str(SOURCES[0][1])
    os.environ["MONITOR_HEALTH_TOKEN_FILE"] = str(SOURCES[1][1])
    os.setgroups([])
    os.setgid(RUNTIME_GID)
    os.setuid(RUNTIME_UID)
    if (
        os.getuid() != RUNTIME_UID or os.geteuid() != RUNTIME_UID
        or os.getgid() != RUNTIME_GID or os.getegid() != RUNTIME_GID
        or os.getgroups()
        or (hasattr(os, "getresuid") and os.getresuid() != (RUNTIME_UID,) * 3)
        or (hasattr(os, "getresgid") and os.getresgid() != (RUNTIME_GID,) * 3)
    ):
        raise RuntimeError("monitor launcher could not drop privileges")
    _assert_privileges_dropped()
    _verify_runtime_files()


def main() -> None:
    command = sys.argv[1:]
    drop_only = bool(command and command[0] == "--drop-only")
    if drop_only:
        command = command[1:]
    prepare_and_drop(command, drop_only=drop_only)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
