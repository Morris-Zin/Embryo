"""Cross-platform crash-recoverable mutexes and resource leases (Windows + POSIX).

Pure Python 3.11+ stdlib. Never calls os.kill(pid, 0) on Windows.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import random
import secrets
import socket
import sys
import tempfile
import time
from typing import Any, Iterator

from . import EXIT_LEASE_CONFLICT

# Windows OpenProcess access rights: SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
WIN_PROCESS_QUERY_SYNC = 0x00101000
WIN_ERROR_ACCESS_DENIED = 5
WIN_WAIT_TIMEOUT = 0x00000102


class LeaseError(RuntimeError):
    exit_code = 1


class LeaseConflictError(LeaseError):
    exit_code = EXIT_LEASE_CONFLICT


class MutexTimeoutError(LeaseError):
    exit_code = EXIT_LEASE_CONFLICT


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


def _filetime_to_ns(ft: _FILETIME) -> int:
    ticks_100ns = (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)
    return ticks_100ns * 100


class _DefaultWindowsAPI:
    """Thin ctypes wrapper around kernel32 so unit tests can mock it cleanly."""

    def __init__(self) -> None:
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
        ]
        self._kernel32.GetProcessTimes.restype = wintypes.BOOL
        self._kernel32.GetCurrentProcess.argtypes = []
        self._kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    def open_process(self, access: int, inherit: bool, pid: int) -> tuple[int, int]:
        ctypes.set_last_error(0)
        handle = self._kernel32.OpenProcess(access, inherit, int(pid))
        err = ctypes.get_last_error()
        return int(handle or 0), int(err)

    def wait_for_single_object(self, handle: int, timeout_ms: int) -> int:
        return int(self._kernel32.WaitForSingleObject(handle, timeout_ms))

    def get_process_creation_ns(self, handle: int) -> int | None:
        creation = _FILETIME()
        exit_time = _FILETIME()
        kernel_time = _FILETIME()
        user_time = _FILETIME()
        ok = self._kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        if not ok:
            return None
        return _filetime_to_ns(creation)

    def get_current_process_creation_ns(self) -> int | None:
        handle = self._kernel32.GetCurrentProcess()
        return self.get_process_creation_ns(int(handle))

    def close_handle(self, handle: int) -> None:
        if handle:
            self._kernel32.CloseHandle(handle)


_WIN_API_SINGLETON: _DefaultWindowsAPI | None = None


def _get_win_api() -> _DefaultWindowsAPI:
    global _WIN_API_SINGLETON
    if _WIN_API_SINGLETON is None:
        _WIN_API_SINGLETON = _DefaultWindowsAPI()
    return _WIN_API_SINGLETON


def default_lock_dir() -> Path:
    override = os.environ.get("AUTOLOOP_LOCK_DIR") or os.environ.get("AUTOLOOP_SHARED_LOCK_DIR")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "autoloop-locks"


def resource_key_hash(resource: str) -> str:
    return hashlib.sha256(resource.encode("utf-8")).hexdigest()


def get_process_start_ns(pid: int | None = None, *, _win_api: Any = None) -> int | None:
    if sys.platform == "win32" or _win_api is not None:
        api = _win_api or _get_win_api()
        if pid is None or pid == os.getpid():
            return api.get_current_process_creation_ns()
        handle, _err = api.open_process(WIN_PROCESS_QUERY_SYNC, False, pid)
        if not handle:
            return None
        try:
            return api.get_process_creation_ns(handle)
        finally:
            api.close_handle(handle)
    target_pid = os.getpid() if pid is None else int(pid)
    stat_path = Path(f"/proc/{target_pid}/stat")
    try:
        raw = stat_path.read_text(encoding="utf-8")
        # Field 22 (0-indexed after comm closing ')': index 19) is starttime in clock ticks.
        after_comm = raw.rsplit(")", 1)[1].split()
        return int(after_comm[19])
    except (OSError, IndexError, ValueError):
        return None


def is_pid_alive(
    pid: int,
    host: str,
    process_start_ns: int | None = None,
    *,
    _win_api: Any = None,
    _platform: str | None = None,
) -> bool | None:
    """Return True if alive, False if provably dead, or None if host differs."""
    if host != socket.gethostname():
        return None
    if not isinstance(pid, int) or pid <= 0:
        return False

    platform = _platform or sys.platform
    if platform == "win32" or _win_api is not None:
        # NEVER call os.kill(pid, 0) on Windows.
        api = _win_api or _get_win_api()
        handle, err = api.open_process(WIN_PROCESS_QUERY_SYNC, False, pid)
        if not handle:
            if err == WIN_ERROR_ACCESS_DENIED:
                return True
            return False
        try:
            wait_result = api.wait_for_single_object(handle, 0)
            if wait_result != WIN_WAIT_TIMEOUT:
                return False
            if process_start_ns is not None:
                actual_start_ns = api.get_process_creation_ns(handle)
                if actual_start_ns is not None and actual_start_ns != process_start_ns:
                    return False
            return True
        finally:
            api.close_handle(handle)

    # POSIX path (same host only).
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False

    if process_start_ns is not None:
        actual = get_process_start_ns(pid)
        if actual is not None and actual != process_start_ns:
            return False
    return True


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    tmp_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    tmp_path = path.parent / tmp_name
    fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp_path), str(path))
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_json_or_none(path: Path) -> tuple[dict[str, Any] | None, bool]:
    """Return (data, exists_and_corrupt_or_empty)."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, False
    except OSError:
        return None, False
    if not raw.strip():
        return None, True
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, True
    if not isinstance(parsed, dict):
        return None, True
    return parsed, False


def _is_mutex_stale(
    mutex_path: Path,
    now: float,
    stale_after_s: float,
    empty_grace_s: float,
    *,
    _win_api: Any = None,
) -> bool:
    try:
        st = mutex_path.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return False

    age = now - st.st_mtime
    # Fresh non-empty mutexes are actively held; avoid opening them on Windows to prevent unlink sharing contention.
    if st.st_size > 0 and age < min(0.2, stale_after_s):
        return False
    if st.st_size == 0:
        return age >= empty_grace_s

    data, corrupt = _read_json_or_none(mutex_path)
    if corrupt:
        return age >= empty_grace_s
    if data is None:
        return False

    created_at = data.get("created_at")
    if isinstance(created_at, (int, float)) and (now - float(created_at)) >= stale_after_s:
        return True

    pid = data.get("pid")
    host = data.get("host")
    start_ns = data.get("process_start_ns")
    if isinstance(pid, int) and isinstance(host, str):
        alive = is_pid_alive(pid, host, start_ns if isinstance(start_ns, int) else None, _win_api=_win_api)
        if alive is False:
            return True
    return False


def _unlink_with_retry(path: Path, attempts: int = 25) -> None:
    for attempt in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                return
            time.sleep(0.005 * (attempt + 1))
        except OSError:
            return


@contextmanager
def acquire_mutex(
    mutex_path: Path,
    *,
    timeout_s: float = 10.0,
    stale_after_s: float = 30.0,
    empty_grace_s: float = 0.25,
    _win_api: Any = None,
) -> Iterator[None]:
    """Acquire an exclusive crash-recoverable lock file via O_CREAT | O_EXCL."""
    mutex_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    acquired = False
    token = secrets.token_hex(8)
    payload = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "process_start_ns": get_process_start_ns(_win_api=_win_api),
        "created_at": time.time(),
        "token": token,
    }
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")

    while True:
        try:
            fd = os.open(str(mutex_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)
            acquired = True
            break
        except (FileExistsError, PermissionError):
            now = time.time()
            if _is_mutex_stale(mutex_path, now, stale_after_s, empty_grace_s, _win_api=_win_api):
                try:
                    mutex_path.unlink()
                    continue
                except FileNotFoundError:
                    continue
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise MutexTimeoutError(f"timed out waiting for mutex {mutex_path.name}")
            time.sleep(random.uniform(0.005, 0.025))

    try:
        yield
    finally:
        if acquired:
            _unlink_with_retry(mutex_path)


def _lease_paths(resource: str, lock_dir: Path | None = None) -> tuple[Path, Path]:
    base_dir = lock_dir or default_lock_dir()
    digest = resource_key_hash(resource)
    return base_dir / f"{digest}.lease.json", base_dir / f"{digest}.mutex"


def _is_lease_reclaimable(
    lease: dict[str, Any],
    now: float,
    *,
    _win_api: Any = None,
) -> bool:
    expires_at = lease.get("expires_at")
    if not isinstance(expires_at, (int, float)) or now >= float(expires_at):
        return True
    pid = lease.get("pid")
    host = lease.get("host")
    start_ns = lease.get("process_start_ns")
    if isinstance(pid, int) and isinstance(host, str):
        alive = is_pid_alive(pid, host, start_ns if isinstance(start_ns, int) else None, _win_api=_win_api)
        if alive is False:
            return True
    return False


def read_lease(resource: str, *, lock_dir: Path | None = None) -> dict[str, Any] | None:
    lease_path, _ = _lease_paths(resource, lock_dir)
    data, _ = _read_json_or_none(lease_path)
    return data


def acquire_lease(
    resource: str,
    owner: str,
    *,
    ttl_s: float = 300.0,
    lock_dir: Path | None = None,
    timeout_s: float = 10.0,
    stale_mutex_s: float = 30.0,
    empty_grace_s: float = 0.25,
    _win_api: Any = None,
) -> dict[str, Any]:
    if not resource or not owner:
        raise LeaseError("resource and owner must be non-empty")
    if ttl_s <= 0:
        raise LeaseError("ttl_s must be positive")
    lease_path, mutex_path = _lease_paths(resource, lock_dir)
    with acquire_mutex(
        mutex_path,
        timeout_s=timeout_s,
        stale_after_s=stale_mutex_s,
        empty_grace_s=empty_grace_s,
        _win_api=_win_api,
    ):
        now = time.time()
        existing, corrupt = _read_json_or_none(lease_path)
        reclaimed = False
        if corrupt:
            # Crash recovery: 0-byte or half-written lease file from crashed creator.
            lease_path.unlink(missing_ok=True)
            reclaimed = True
        elif existing is not None:
            if _is_lease_reclaimable(existing, now, _win_api=_win_api):
                reclaimed = True
            elif (
                existing.get("owner") == owner
                and existing.get("pid") == os.getpid()
                and existing.get("host") == socket.gethostname()
            ):
                # Idempotent re-acquire by the same live owner process.
                existing["renewed_at"] = now
                existing["expires_at"] = now + float(ttl_s)
                atomic_write_json(lease_path, existing)
                return existing
            else:
                raise LeaseConflictError(
                    f"resource {resource!r} is leased by {existing.get('owner')!r} "
                    f"(pid={existing.get('pid')}, host={existing.get('host')})"
                )

        lease = {
            "resource": resource,
            "resource_hash": resource_key_hash(resource),
            "owner": owner,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "process_start_ns": get_process_start_ns(_win_api=_win_api),
            "acquired_at": now,
            "renewed_at": now,
            "expires_at": now + float(ttl_s),
            "token": secrets.token_hex(12),
            "reclaimed_previous": reclaimed,
        }
        atomic_write_json(lease_path, lease)
        return lease


def renew_lease(
    resource: str,
    owner: str,
    *,
    ttl_s: float = 300.0,
    lock_dir: Path | None = None,
    token: str | None = None,
    timeout_s: float = 10.0,
    _win_api: Any = None,
) -> dict[str, Any]:
    if ttl_s <= 0:
        raise LeaseError("ttl_s must be positive")
    lease_path, mutex_path = _lease_paths(resource, lock_dir)
    with acquire_mutex(mutex_path, timeout_s=timeout_s, _win_api=_win_api):
        existing, corrupt = _read_json_or_none(lease_path)
        if corrupt or existing is None:
            raise LeaseConflictError(f"no active lease exists for {resource!r}")
        if existing.get("owner") != owner:
            raise LeaseConflictError(
                f"cannot renew lease for {resource!r}: owned by {existing.get('owner')!r}, not {owner!r}"
            )
        if token is not None and existing.get("token") != token:
            raise LeaseConflictError(f"lease token mismatch for {resource!r}")
        now = time.time()
        existing["renewed_at"] = now
        existing["expires_at"] = now + float(ttl_s)
        existing["pid"] = os.getpid()
        existing["host"] = socket.gethostname()
        existing["process_start_ns"] = get_process_start_ns(_win_api=_win_api)
        atomic_write_json(lease_path, existing)
        return existing


def release_lease(
    resource: str,
    owner: str | None = None,
    *,
    lock_dir: Path | None = None,
    token: str | None = None,
    timeout_s: float = 10.0,
    _win_api: Any = None,
) -> bool:
    lease_path, mutex_path = _lease_paths(resource, lock_dir)
    with acquire_mutex(mutex_path, timeout_s=timeout_s, _win_api=_win_api):
        existing, corrupt = _read_json_or_none(lease_path)
        if corrupt:
            lease_path.unlink(missing_ok=True)
            return True
        if existing is None:
            return False
        if owner is not None and existing.get("owner") != owner:
            raise LeaseConflictError(
                f"cannot release lease for {resource!r}: owned by {existing.get('owner')!r}, not {owner!r}"
            )
        if token is not None and existing.get("token") != token:
            raise LeaseConflictError(f"lease token mismatch for {resource!r}")
        lease_path.unlink(missing_ok=True)
        return True
