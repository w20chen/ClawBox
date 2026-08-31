from __future__ import annotations

import base64
import errno
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore, Condition, Event, Lock, Thread
from typing import Any, Protocol


def _read_int_file(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="ascii").strip()
        return None if value == "max" else int(value)
    except (OSError, ValueError):
        return None


def _read_key_value_file(path: Path) -> dict[str, int]:
    try:
        return {
            fields[0]: int(fields[1])
            for line in path.read_text(encoding="ascii").splitlines()
            if len(fields := line.split()) == 2
        }
    except (OSError, ValueError):
        return {}


def _process_io(pid: int) -> dict[str, int]:
    return _read_key_value_file(Path(f"/proc/{pid}/io"))


def _process_faults(pid: int) -> dict[str, int | None]:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
        return {"minor_faults": int(fields[9]), "major_faults": int(fields[11])}
    except (OSError, IndexError, ValueError):
        return {"minor_faults": None, "major_faults": None}


def _read_memory_numa_stat(path: Path) -> dict[str, dict[str, int]]:
    try:
        result: dict[str, dict[str, int]] = {}
        for line in path.read_text(encoding="ascii").splitlines():
            fields = line.split()
            if len(fields) < 2:
                continue
            result[fields[0]] = {
                key: int(value)
                for field in fields[1:]
                if "=" in field
                for key, value in (field.split("=", 1),)
            }
        return result
    except (OSError, ValueError):
        return {}


def _counter_delta(after: dict[str, int], before: dict[str, int], key: str) -> int | None:
    if key not in after or key not in before:
        return None
    return max(0, int(after[key]) - int(before[key]))


class LifecycleError(RuntimeError):
    pass


class SandboxLifecycle(Protocol):
    @property
    def resident(self) -> bool: ...
    def start(self) -> float: ...
    def checkpoint_and_evict(self) -> float: ...
    def restore(self) -> float: ...
    def close(self) -> None: ...


class SimulatedLifecycle:
    """Deterministic lifecycle for parser/engine tests and dry runs."""

    def __init__(self, *, start_s: float = 0.0, snapshot_s: float = 0.0,
                 restore_s: float = 0.0) -> None:
        self.start_delay_s = start_s
        self.snapshot_delay_s = snapshot_s
        self.restore_delay_s = restore_s
        self._resident = False

    @property
    def resident(self) -> bool:
        return self._resident

    def start(self) -> float:
        if self._resident:
            raise LifecycleError("simulated VM already started")
        time.sleep(self.start_delay_s)
        self._resident = True
        return self.start_delay_s

    def checkpoint_and_evict(self) -> float:
        if not self._resident:
            raise LifecycleError("simulated VM is not resident")
        time.sleep(self.snapshot_delay_s)
        self._resident = False
        return self.snapshot_delay_s

    def restore(self) -> float:
        if self._resident:
            raise LifecycleError("simulated VM is already resident")
        time.sleep(self.restore_delay_s)
        self._resident = True
        return self.restore_delay_s

    def close(self) -> None:
        self._resident = False


class CommandExecutor(Protocol):
    def execute(self, command: str, timeout_s: float) -> "CommandResult": ...


@dataclass(frozen=True, slots=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float


class LocalCommandExecutor:
    def __init__(self, *, cwd: Path | None = None,
                 workspace_alias: str | None = None) -> None:
        self.cwd = cwd
        self.workspace_alias = workspace_alias

    def execute(self, command: str, timeout_s: float) -> CommandResult:
        if self.workspace_alias and self.cwd is not None:
            command = command.replace(self.workspace_alias, str(self.cwd.resolve()))
        started = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=self.cwd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return CommandResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
            time.monotonic() - started,
        )

    def wait_ready(self, timeout_s: float) -> float:
        return 0.0


class SSHCommandExecutor:
    """Reconnect-per-command SSH transport suitable across VM process restore."""

    def __init__(self, *, host: str, port: int, user: str, identity_file: Path,
                 connect_timeout_s: int = 10) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.identity_file = identity_file
        self.connect_timeout_s = connect_timeout_s

    def execute(self, command: str, timeout_s: float) -> CommandResult:
        encoded = base64.b64encode(command.encode()).decode("ascii")
        remote = f"printf %s {encoded} | base64 -d | /bin/sh"
        argv = self._argv(remote)
        started = time.monotonic()
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s,
        )
        return CommandResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
            time.monotonic() - started,
        )

    def wait_ready(self, timeout_s: float) -> float:
        started = time.monotonic()
        deadline = started + timeout_s
        last_stderr = ""
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                completed = subprocess.run(
                    self._argv("true"), capture_output=True, text=True,
                    timeout=min(float(self.connect_timeout_s + 2), remaining),
                )
            except subprocess.TimeoutExpired as exc:
                last_stderr = str(exc)
            else:
                if completed.returncode == 0:
                    return time.monotonic() - started
                last_stderr = completed.stderr.strip()
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        raise LifecycleError(f"guest SSH did not become ready: {last_stderr}")

    def _argv(self, remote: str) -> list[str]:
        return [
            "ssh", "-T", "-p", str(self.port),
            "-i", str(self.identity_file),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ConnectTimeout={self.connect_timeout_s}",
            f"{self.user}@{self.host}", remote,
        ]


class KubectlCommandExecutor:
    """Execute replayed tools in the paired Kata/Firecracker Tool pod."""

    def __init__(self, *, namespace: str, pod: str, container: str | None = None,
                 kubectl: str = "kubectl") -> None:
        self.namespace = namespace
        self.pod = pod
        self.container = container
        self.kubectl = kubectl

    def execute(self, command: str, timeout_s: float) -> CommandResult:
        encoded = base64.b64encode(command.encode()).decode("ascii")
        remote = f"printf %s {encoded} | base64 -d | /bin/sh"
        started = time.monotonic()
        completed = subprocess.run(
            [*self._base_argv(), "exec", self.pod, *self._container_argv(), "--", "/bin/sh", "-c", remote],
            capture_output=True, text=True, timeout=timeout_s,
        )
        return CommandResult(
            completed.returncode, completed.stdout, completed.stderr,
            time.monotonic() - started,
        )

    def wait_ready(self, timeout_s: float) -> float:
        started = time.monotonic()
        completed = subprocess.run(
            [*self._base_argv(), "wait", f"pod/{self.pod}",
             "--for=condition=Ready", f"--timeout={max(1, int(timeout_s))}s"],
            capture_output=True, text=True, timeout=timeout_s + 5,
        )
        if completed.returncode != 0:
            raise LifecycleError(f"Tool pod did not become ready: {completed.stderr.strip()}")
        return time.monotonic() - started

    def _base_argv(self) -> list[str]:
        return [self.kubectl, "-n", self.namespace]

    def _container_argv(self) -> list[str]:
        return [] if self.container is None else ["-c", self.container]


class _UnixHttpClient:
    def __init__(self, socket_path: Path, timeout_s: float) -> None:
        self.socket_path = socket_path
        self.timeout_s = timeout_s

    def request(self, method: str, route: str, body: dict[str, Any] | None = None) -> Any:
        payload = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
        request = (
            f"{method} {route} HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Accept: application/json\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + payload
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout_s)
            deadline = time.monotonic() + self.timeout_s
            while True:
                try:
                    client.connect(str(self.socket_path))
                    break
                except OSError as exc:
                    if exc.errno not in {errno.ENOENT, errno.ECONNREFUSED}:
                        raise
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            client.sendall(request)
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = client.recv(65536)
                if not chunk:
                    break
                response += chunk
            head, separator, response_body = response.partition(b"\r\n\r\n")
            if not separator:
                raise LifecycleError(f"malformed Firecracker API response: {response[:200]!r}")
            content_length = 0
            for header_line in head.splitlines()[1:]:
                header_name, marker, header_value = header_line.partition(b":")
                if marker and header_name.strip().lower() == b"content-length":
                    try:
                        content_length = int(header_value.strip())
                    except ValueError as exc:
                        raise LifecycleError("invalid Firecracker Content-Length") from exc
                    break
            while len(response_body) < content_length:
                chunk = client.recv(min(65536, content_length - len(response_body)))
                if not chunk:
                    raise LifecycleError("truncated Firecracker API response body")
                response_body += chunk
            response_body = response_body[:content_length]
        first_line = head.splitlines()[0].decode("ascii", errors="replace")
        parts = first_line.split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise LifecycleError(f"malformed Firecracker API status: {first_line}")
        status = int(parts[1])
        parsed: Any = None
        if response_body:
            try:
                parsed = json.loads(response_body)
            except json.JSONDecodeError:
                parsed = response_body.decode(errors="replace")
        if not 200 <= status < 300:
            raise LifecycleError(f"Firecracker API {method} {route} returned {status}: {parsed!r}")
        return parsed


@dataclass(frozen=True, slots=True)
class FirecrackerConfig:
    binary: Path
    api_socket: Path
    kernel_image: Path
    rootfs: Path
    snapshot_state: Path
    snapshot_memory: Path
    vcpu_count: int = 1
    memory_mib: int = 1024
    boot_args: str = "console=ttyS0 reboot=k panic=1 pci=off"
    tap_device: str | None = None
    guest_mac: str = "06:00:ac:10:00:02"
    vsock_uds: Path | None = None
    guest_cid: int = 3
    guest_agent_port: int | None = None
    cpu_set: str | None = None
    numa_node: int | None = None
    log_path: Path | None = None
    api_timeout_s: float = 15.0
    # Full snapshot creation is synchronous and can legitimately take much
    # longer than ordinary control calls under concurrent multi-GiB I/O.
    snapshot_api_timeout_s: float = 300.0
    boot_timeout_s: float = 30.0
    balloon_enabled: bool = False
    balloon_deflate_on_oom: bool = True
    balloon_stats_polling_interval_s: int = 1
    cgroup_path: Path | None = None

    @classmethod
    def from_json(cls, path: Path) -> "FirecrackerConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        path_fields = {
            "binary", "api_socket", "kernel_image", "rootfs",
            "snapshot_state", "snapshot_memory", "log_path", "vsock_uds",
            "cgroup_path",
        }
        values = {
            key: (None if value is None else Path(value)) if key in path_fields else value
            for key, value in raw.items()
        }
        return cls(**values)


class FirecrackerLifecycle:
    """Direct Firecracker full-snapshot lifecycle with process eviction."""

    def __init__(self, config: FirecrackerConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._resident = False
        self._has_snapshot = False
        self._log_handle: Any = None
        self._snapshot_state_path: Path | None = None
        self._snapshot_memory_path: Path | None = None
        self._last_checkpoint_metrics: dict[str, Any] | None = None
        self._last_restore_metrics: dict[str, Any] | None = None

    @property
    def resident(self) -> bool:
        return self._resident

    def process_exit_code(self) -> int | None:
        """Return the Firecracker exit code, or None while it is running."""
        process = self._process
        return None if process is None or process.poll() is None else process.returncode

    def last_checkpoint_metrics(self) -> dict[str, Any] | None:
        return None if self._last_checkpoint_metrics is None else dict(
            self._last_checkpoint_metrics
        )

    def last_restore_metrics(self) -> dict[str, Any] | None:
        return None if self._last_restore_metrics is None else dict(
            self._last_restore_metrics
        )

    def start(self) -> float:
        if self._resident or self._process is not None:
            raise LifecycleError("Firecracker VM is already started")
        started = time.monotonic()
        self._spawn()
        api = _UnixHttpClient(self.config.api_socket, self.config.api_timeout_s)
        api.request("PUT", "/machine-config", {
            "vcpu_count": self.config.vcpu_count,
            "mem_size_mib": self.config.memory_mib,
            "smt": False,
            "track_dirty_pages": False,
        })
        api.request("PUT", "/boot-source", {
            "kernel_image_path": str(self.config.kernel_image),
            "boot_args": self.config.boot_args,
        })
        api.request("PUT", "/drives/rootfs", {
            "drive_id": "rootfs",
            "path_on_host": str(self.config.rootfs),
            "is_root_device": True,
            "is_read_only": False,
        })
        if self.config.tap_device:
            api.request("PUT", "/network-interfaces/eth0", {
                "iface_id": "eth0",
                "guest_mac": self.config.guest_mac,
                "host_dev_name": self.config.tap_device,
            })
        if self.config.vsock_uds:
            self.config.vsock_uds.parent.mkdir(parents=True, exist_ok=True)
            api.request("PUT", "/vsock", {
                "vsock_id": "runtime",
                "guest_cid": self.config.guest_cid,
                "uds_path": str(self.config.vsock_uds),
            })
        if self.config.balloon_enabled:
            api.request("PUT", "/balloon", {
                "amount_mib": 0,
                "deflate_on_oom": self.config.balloon_deflate_on_oom,
                "stats_polling_interval_s": (
                    self.config.balloon_stats_polling_interval_s
                ),
            })
        api.request("PUT", "/actions", {"action_type": "InstanceStart"})
        self._resident = True
        return time.monotonic() - started

    def checkpoint_and_evict(self) -> float:
        if not self._resident or self._process is None:
            raise LifecycleError("cannot checkpoint a non-resident VM")
        started = time.monotonic()
        process = self._process
        pid = int(getattr(process, "pid", -1))
        cgroup_before = self._cgroup_memory_snapshot()
        peak_reset = self._reset_cgroup_memory_peak()
        io_before = _process_io(pid)
        faults_before = _process_faults(pid)
        rss_before = self.rss_bytes()
        monitor_stop = Event()
        monitor = {
            "peak_cgroup_memory_current_bytes": cgroup_before.get("memory_current_bytes"),
            "peak_firecracker_rss_bytes": rss_before,
            "sample_count": 0,
        }

        def sample_transient() -> None:
            while not monitor_stop.wait(0.005):
                current = self._cgroup_memory_snapshot().get("memory_current_bytes")
                rss = self.rss_bytes()
                if current is not None:
                    prior = monitor["peak_cgroup_memory_current_bytes"]
                    monitor["peak_cgroup_memory_current_bytes"] = max(
                        int(prior or 0), int(current),
                    )
                monitor["peak_firecracker_rss_bytes"] = max(
                    int(monitor["peak_firecracker_rss_bytes"] or 0), rss,
                )
                monitor["sample_count"] = int(monitor["sample_count"]) + 1

        monitor_thread = Thread(target=sample_transient, daemon=True)
        monitor_thread.start()
        api = _UnixHttpClient(
            self.config.api_socket, self.config.snapshot_api_timeout_s,
        )
        paused = False
        next_state: Path | None = None
        next_memory: Path | None = None
        try:
            pause_started = time.monotonic()
            api.request("PATCH", "/vm", {"state": "Paused"})
            paused = True
            pause_s = time.monotonic() - pause_started
            next_state, next_memory = self._next_snapshot_paths()
            next_state.parent.mkdir(parents=True, exist_ok=True)
            next_memory.parent.mkdir(parents=True, exist_ok=True)
            create_started = time.monotonic()
            api.request("PUT", "/snapshot/create", {
                "snapshot_type": "Full",
                "snapshot_path": str(next_state),
                "mem_file_path": str(next_memory),
            })
            create_s = time.monotonic() - create_started
        except Exception:
            # Snapshot failure must not strand a healthy agent in Paused state.
            try:
                if paused:
                    api.request("PATCH", "/vm", {"state": "Resumed"})
            finally:
                if next_state is not None:
                    next_state.unlink(missing_ok=True)
                if next_memory is not None:
                    next_memory.unlink(missing_ok=True)
            raise
        finally:
            # Cover failures before CreateSnapshot too (pause, directory setup,
            # or API construction changes) so the 5 ms sampler never leaks.
            monitor_stop.set()
            monitor_thread.join(timeout=1)
        assert next_state is not None and next_memory is not None
        cgroup_after_create = self._cgroup_memory_snapshot()
        io_after = _process_io(pid)
        faults_after = _process_faults(pid)
        rss_after_create = self.rss_bytes()
        snapshot_files = self._snapshot_file_metrics(next_state, next_memory)
        previous_state = self._snapshot_state_path
        previous_memory = self._snapshot_memory_path
        stop_started = time.monotonic()
        self._stop_process()
        stop_s = time.monotonic() - stop_started
        self._snapshot_state_path = next_state
        self._snapshot_memory_path = next_memory
        cache_drop_started = time.monotonic()
        self._drop_snapshot_page_cache(next_state, next_memory)
        cache_drop_s = time.monotonic() - cache_drop_started
        # A restored VM MAP_PRIVATE-maps its memory snapshot. Reusing that path
        # as the output of the next snapshot can corrupt both the VM and file.
        # Alternate paths, stop the mapping process, then retire the old pair.
        for old_path in (previous_state, previous_memory):
            if old_path is not None and old_path not in {next_state, next_memory}:
                old_path.unlink(missing_ok=True)
        self._resident = False
        self._has_snapshot = True
        elapsed = time.monotonic() - started
        cgroup_after_evict = self._cgroup_memory_snapshot()
        before_current = cgroup_before.get("memory_current_bytes")
        peak_current = monitor.get("peak_cgroup_memory_current_bytes")
        kernel_operation_peak = (
            cgroup_after_create.get("memory_peak_bytes") if peak_reset else None
        )
        effective_peak = max(
            int(peak_current or 0), int(kernel_operation_peak or 0),
        ) or None
        after_current = cgroup_after_evict.get("memory_current_bytes")
        file_before = int(cgroup_before.get("stat_file_bytes") or 0)
        file_after_create = int(cgroup_after_create.get("stat_file_bytes") or 0)
        self._last_checkpoint_metrics = {
            "elapsed_s": elapsed,
            "pause_s": pause_s,
            # Firecracker 1.12.x synchronously flushes full-snapshot files as
            # part of CreateSnapshot and exposes no supported API switch for
            # separating write and fsync time.
            "snapshot_create_and_fsync_s": create_s,
            "snapshot_fsync_s": None,
            "process_stop_s": stop_s,
            "page_cache_drop_s": cache_drop_s,
            "firecracker_rss_before_bytes": rss_before,
            "firecracker_rss_after_snapshot_create_bytes": rss_after_create,
            "peak_firecracker_rss_bytes": monitor["peak_firecracker_rss_bytes"],
            "transient_sample_count": monitor["sample_count"],
            "cgroup_before": cgroup_before,
            "cgroup_operation_peak_reset": peak_reset,
            "cgroup_after_snapshot_create": cgroup_after_create,
            "cgroup_after_evict": cgroup_after_evict,
            "cgroup_memory_peak_during_checkpoint_bytes": effective_peak,
            "cgroup_kernel_memory_peak_during_checkpoint_bytes": kernel_operation_peak,
            "cgroup_memory_transient_growth_bytes": (
                None if before_current is None or effective_peak is None
                else max(0, int(effective_peak) - int(before_current))
            ),
            "cgroup_memory_released_bytes": (
                None if before_current is None or after_current is None
                else max(0, int(before_current) - int(after_current))
            ),
            "page_cache_growth_during_snapshot_bytes": max(
                0, file_after_create - file_before,
            ),
            "snapshot_files": snapshot_files,
            "snapshot_logical_bytes": sum(
                int(item["logical_bytes"]) for item in snapshot_files.values()
            ),
            "snapshot_allocated_bytes": sum(
                int(item["allocated_bytes"]) for item in snapshot_files.values()
            ),
            "process_io_before": io_before,
            "process_io_after_snapshot_create": io_after,
            "process_write_bytes": _counter_delta(io_after, io_before, "write_bytes"),
            "process_logical_write_bytes": _counter_delta(io_after, io_before, "wchar"),
            "minor_faults": (
                None
                if faults_before["minor_faults"] is None
                or faults_after["minor_faults"] is None
                else max(
                    0,
                    int(faults_after["minor_faults"])
                    - int(faults_before["minor_faults"]),
                )
            ),
            "major_faults": (
                None
                if faults_before["major_faults"] is None
                or faults_after["major_faults"] is None
                else max(
                    0,
                    int(faults_after["major_faults"])
                    - int(faults_before["major_faults"]),
                )
            ),
        }
        return elapsed

    def restore(self) -> float:
        if self._resident or self._process is not None:
            raise LifecycleError("cannot restore while VM is resident")
        if not self._has_snapshot:
            raise LifecycleError("cannot restore before a snapshot exists")
        if self._snapshot_state_path is None or self._snapshot_memory_path is None:
            raise LifecycleError("snapshot paths are unavailable")
        started = time.monotonic()
        cgroup_before = self._cgroup_memory_snapshot()
        spawn_started = time.monotonic()
        self._spawn()
        spawn_s = time.monotonic() - spawn_started
        assert self._process is not None
        pid = int(getattr(self._process, "pid", -1))
        faults_before = _process_faults(pid)
        io_before = _process_io(pid)
        try:
            api = _UnixHttpClient(
                self.config.api_socket, self.config.snapshot_api_timeout_s,
            )
            request: dict[str, Any] = {
                "snapshot_path": str(self._snapshot_state_path),
                "mem_backend": {
                    "backend_type": "File",
                    "backend_path": str(self._snapshot_memory_path),
                },
                "enable_diff_snapshots": False,
                "resume_vm": True,
            }
            if self.config.vsock_uds:
                request["vsock_override"] = {"uds_path": str(self.config.vsock_uds)}
            load_started = time.monotonic()
            try:
                api.request("PUT", "/snapshot/load", request)
            except LifecycleError as exc:
                # Firecracker 1.12 restores the snapshotted vsock path but does
                # not yet accept vsock_override. Retry only for that explicit
                # schema error; all other restore failures remain fatal.
                if "unknown field `vsock_override`" not in str(exc):
                    raise
                request.pop("vsock_override", None)
                api.request("PUT", "/snapshot/load", request)
            load_s = time.monotonic() - load_started
        except Exception:
            self._stop_process()
            raise
        self._resident = True
        elapsed = time.monotonic() - started
        faults_after = _process_faults(pid)
        io_after = _process_io(pid)
        self._last_restore_metrics = {
            "elapsed_s": elapsed,
            "process_spawn_s": spawn_s,
            "snapshot_load_s": load_s,
            "firecracker_rss_after_load_bytes": self.rss_bytes(),
            "cgroup_before": cgroup_before,
            "cgroup_after_load": self._cgroup_memory_snapshot(),
            "process_io_before": io_before,
            "process_io_after_load": io_after,
            "process_read_bytes": _counter_delta(io_after, io_before, "read_bytes"),
            "process_logical_read_bytes": _counter_delta(io_after, io_before, "rchar"),
            "minor_faults_during_load": (
                None
                if faults_before["minor_faults"] is None
                or faults_after["minor_faults"] is None
                else max(
                    0,
                    int(faults_after["minor_faults"])
                    - int(faults_before["minor_faults"]),
                )
            ),
            "major_faults_during_load": (
                None
                if faults_before["major_faults"] is None
                or faults_after["major_faults"] is None
                else max(
                    0,
                    int(faults_after["major_faults"])
                    - int(faults_before["major_faults"]),
                )
            ),
        }
        return elapsed

    def close(self) -> None:
        self._stop_process()
        self._resident = False

    def set_balloon_target_mib(self, amount_mib: int) -> dict[str, Any] | None:
        """Set the live virtio-balloon target without changing VM capacity."""
        if not self.config.balloon_enabled:
            raise LifecycleError("virtio-balloon is not configured")
        if not self._resident or self._process is None:
            raise LifecycleError("cannot update balloon on a non-resident VM")
        if amount_mib < 0 or amount_mib >= self.config.memory_mib:
            raise ValueError("balloon target must be non-negative and below VM capacity")
        api = _UnixHttpClient(self.config.api_socket, self.config.api_timeout_s)
        api.request("PATCH", "/balloon", {"amount_mib": int(amount_mib)})
        return self.balloon_statistics()

    def balloon_statistics(self) -> dict[str, Any] | None:
        if not self.config.balloon_enabled:
            return None
        if not self._resident or self._process is None:
            return None
        api = _UnixHttpClient(self.config.api_socket, self.config.api_timeout_s)
        result = api.request("GET", "/balloon/statistics")
        return result if isinstance(result, dict) else None

    def rss_bytes(self) -> int:
        process = self._process
        if process is None or process.poll() is not None:
            return 0
        pid = getattr(process, "pid", None)
        if pid is None:
            return 0
        try:
            fields = Path(f"/proc/{pid}/statm").read_text(encoding="ascii").split()
            return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, IndexError, ValueError):
            return 0

    def snapshot_allocated_bytes(self) -> int:
        """Allocated disk blocks for the currently retained snapshot pair."""
        total = 0
        for path in (self._snapshot_state_path, self._snapshot_memory_path):
            if path is None:
                continue
            try:
                total += path.stat().st_blocks * 512
            except OSError:
                continue
        return total

    def _spawn(self) -> None:
        socket_path = self.config.api_socket
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            if not socket_path.is_socket():
                raise LifecycleError(f"refusing to remove non-socket API path: {socket_path}")
            socket_path.unlink()
        self._remove_stale_vsock()
        argv: list[str] = []
        if (self.config.numa_node is not None or self.config.cpu_set
                or self.config.cgroup_path is not None):
            if self.config.numa_node is None or not self.config.cpu_set:
                raise LifecycleError("NUMA node and CPU set must be configured together")
            argv.extend([
                sys.executable, str(Path(__file__).with_name("_numa_exec.py")),
                "--numa-node", str(self.config.numa_node),
                "--cpu-set", self.config.cpu_set,
            ])
            if self.config.cgroup_path is not None:
                argv.extend(["--cgroup", str(self.config.cgroup_path)])
            argv.append("--")
        argv.extend([str(self.config.binary), "--api-sock", str(socket_path)])
        log_target: Any = subprocess.DEVNULL
        if self.config.log_path:
            self.config.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = self.config.log_path.open("ab")
            log_target = self._log_handle
        self._process = subprocess.Popen(argv, stdout=log_target, stderr=log_target)
        deadline = time.monotonic() + self.config.boot_timeout_s
        while not socket_path.exists():
            if self._process.poll() is not None:
                raise LifecycleError(f"Firecracker exited before API socket was ready: {self._process.returncode}")
            if time.monotonic() >= deadline:
                self._stop_process()
                raise LifecycleError("timed out waiting for Firecracker API socket")
            time.sleep(0.02)

    def _stop_process(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        if self.config.api_socket.exists() and self.config.api_socket.is_socket():
            self.config.api_socket.unlink()
        self._remove_stale_vsock()

    def _remove_stale_vsock(self) -> None:
        path = self.config.vsock_uds
        if path is None or not path.exists():
            return
        if not path.is_socket():
            raise LifecycleError(f"refusing to remove non-socket vsock path: {path}")
        path.unlink()

    def _cgroup_memory_snapshot(self) -> dict[str, Any]:
        path = self.config.cgroup_path
        if path is None:
            return {
                "memory_current_bytes": None,
                "memory_peak_bytes": None,
                "memory_stat": {},
                "memory_events_local": {},
                "memory_numa_stat": {},
            }
        stat = _read_key_value_file(path / "memory.stat")
        events_local = _read_key_value_file(path / "memory.events.local")
        if not events_local:
            events_local = _read_key_value_file(path / "memory.events")
        numa_stat = _read_memory_numa_stat(path / "memory.numa_stat")
        return {
            "memory_current_bytes": _read_int_file(path / "memory.current"),
            "memory_peak_bytes": _read_int_file(path / "memory.peak"),
            "memory_stat": stat,
            "memory_events_local": events_local,
            "memory_numa_stat": numa_stat,
            "stat_anon_bytes": stat.get("anon"),
            "stat_file_bytes": stat.get("file"),
            "stat_kernel_bytes": stat.get("kernel"),
            "stat_sock_bytes": stat.get("sock"),
        }

    def _reset_cgroup_memory_peak(self) -> bool:
        """Reset the per-session peak so the kernel captures short spikes.

        Parent pool peaks remain untouched and are the authoritative arm-level
        peak. This reset is only for operation-local checkpoint diagnostics.
        """
        path = self.config.cgroup_path
        if path is None:
            return False
        try:
            (path / "memory.peak").write_text("0", encoding="ascii")
        except OSError:
            return False
        return True

    @staticmethod
    def _snapshot_file_metrics(state_path: Path, memory_path: Path) -> dict[str, dict[str, int]]:
        metrics: dict[str, dict[str, int]] = {}
        for label, path in (("state", state_path), ("memory", memory_path)):
            try:
                stat = path.stat()
            except OSError:
                metrics[label] = {"logical_bytes": 0, "allocated_bytes": 0}
                continue
            metrics[label] = {
                "logical_bytes": int(stat.st_size),
                "allocated_bytes": int(stat.st_blocks * 512),
            }
        return metrics

    def _next_snapshot_paths(self) -> tuple[Path, Path]:
        if self._snapshot_memory_path == self.config.snapshot_memory:
            return (
                self.config.snapshot_state.with_name(self.config.snapshot_state.name + ".next"),
                self.config.snapshot_memory.with_name(self.config.snapshot_memory.name + ".next"),
            )
        return self.config.snapshot_state, self.config.snapshot_memory

    def _drop_snapshot_page_cache(self, state_path: Path, memory_path: Path) -> None:
        if not hasattr(os, "posix_fadvise") or not hasattr(os, "POSIX_FADV_DONTNEED"):
            return
        for path in (state_path, memory_path):
            try:
                with path.open("rb") as handle:
                    os.posix_fadvise(handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except OSError:
                # Eviction already succeeded; cache dropping is best effort.
                pass


class ResidentSlotLifecycle:
    """Apply the same explicit resident-memory budget to both experiment arms."""

    def __init__(
        self, inner: SandboxLifecycle, slots: BoundedSemaphore,
        acquire_timeout_s: Callable[[], float | None] | None = None,
    ) -> None:
        self.inner = inner
        self.slots = slots
        self.acquire_timeout_s = acquire_timeout_s
        self._owns_slot = False
        self._lock = Lock()
        self.admission_wait_s = 0.0
        self.admission_acquisitions = 0

    @property
    def resident(self) -> bool:
        return self.inner.resident

    def process_exit_code(self) -> int | None:
        checker = getattr(self.inner, "process_exit_code", None)
        return checker() if callable(checker) else None

    def start(self) -> float:
        self._acquire()
        try:
            return self.inner.start()
        except Exception:
            self._release()
            raise

    def checkpoint_and_evict(self) -> float:
        elapsed = self.inner.checkpoint_and_evict()
        self._release()
        return elapsed

    def restore(self) -> float:
        self._acquire()
        try:
            return self.inner.restore()
        except Exception:
            self._release()
            raise

    def close(self) -> None:
        try:
            self.inner.close()
        finally:
            self._release()

    def rss_bytes(self) -> int:
        sampler = getattr(self.inner, "rss_bytes", None)
        return int(sampler()) if callable(sampler) else 0

    def _acquire(self) -> None:
        with self._lock:
            if self._owns_slot:
                raise LifecycleError("resident slot is already held")
        started = time.monotonic()
        timeout = self.acquire_timeout_s() if self.acquire_timeout_s is not None else None
        acquired = self.slots.acquire(timeout=timeout) if timeout is not None else self.slots.acquire()
        self.admission_wait_s += time.monotonic() - started
        if acquired is False:
            raise TimeoutError("timed out waiting for resident-memory admission")
        with self._lock:
            self._owns_slot = True
            self.admission_acquisitions += 1

    def _release(self) -> None:
        with self._lock:
            if not self._owns_slot:
                return
            self._owns_slot = False
        self.slots.release()


class FairSemaphore:
    """FIFO counting semaphore for reproducible admission/restore ordering."""

    def __init__(self, value: int) -> None:
        if value < 1:
            raise ValueError("semaphore value must be positive")
        self._capacity = value
        self._available = value
        self._next_ticket = 0
        self._serving_ticket = 0
        self._cancelled: set[int] = set()
        self._condition = Condition()

    def acquire(self, timeout: float | None = None) -> bool:
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
            while ticket != self._serving_ticket or self._available <= 0:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    self._cancelled.add(ticket)
                    self._advance_cancelled()
                    self._condition.notify_all()
                    return False
                self._condition.wait(remaining)
            self._available -= 1
            self._serving_ticket += 1
            self._advance_cancelled()
            self._condition.notify_all()
            return True

    def release(self) -> None:
        with self._condition:
            if self._available >= self._capacity:
                raise ValueError("semaphore released too many times")
            self._available += 1
            self._condition.notify_all()

    def _advance_cancelled(self) -> None:
        while self._serving_ticket in self._cancelled:
            self._cancelled.remove(self._serving_ticket)
            self._serving_ticket += 1


class FairResourcePool:
    """FIFO admission that atomically leases one concrete resource.

    A counting semaphore is insufficient when admission also determines CPU
    placement: two admitted sessions could otherwise retain colliding static
    CPU assignments.  This pool couples the resident-memory slot and CPU pair
    into one lease, so at most one resident VM pair owns each CPU pair.
    """

    def __init__(self, resources: list[Any]) -> None:
        if not resources:
            raise ValueError("resource pool must not be empty")
        try:
            unique = set(resources)
        except TypeError as exc:
            raise ValueError("resource pool entries must be hashable") from exc
        if len(unique) != len(resources):
            raise ValueError("resource pool entries must be unique")
        self._resources = tuple(resources)
        self._available = list(resources)
        self._leased: set[Any] = set()
        self._next_ticket = 0
        self._serving_ticket = 0
        self._cancelled: set[int] = set()
        self._condition = Condition()

    def acquire(self, timeout: float | None = None) -> Any | None:
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
            while ticket != self._serving_ticket or not self._available:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    self._cancelled.add(ticket)
                    self._advance_cancelled()
                    self._condition.notify_all()
                    return None
                self._condition.wait(remaining)
            resource = self._available.pop(0)
            self._leased.add(resource)
            self._serving_ticket += 1
            self._advance_cancelled()
            self._condition.notify_all()
            return resource

    def release(self, resource: Any) -> None:
        with self._condition:
            if resource not in self._leased:
                raise ValueError("resource was not leased from this pool")
            self._leased.remove(resource)
            # Stable order prevents the resource selection itself from adding
            # run-to-run randomness after FIFO waiter ordering is established.
            self._available.append(resource)
            order = {item: index for index, item in enumerate(self._resources)}
            self._available.sort(key=order.__getitem__)
            self._condition.notify_all()

    def _advance_cancelled(self) -> None:
        while self._serving_ticket in self._cancelled:
            self._cancelled.remove(self._serving_ticket)
            self._serving_ticket += 1


class FairWeightedResourcePool:
    """FIFO admission for heterogeneous integer accounting reservations."""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("weighted resource capacity must be positive")
        self.capacity = capacity
        self._available = capacity
        self._leased: dict[int, int] = {}
        self._next_lease = 0
        self._next_ticket = 0
        self._serving_ticket = 0
        self._cancelled: set[int] = set()
        self._condition = Condition()

    def acquire(self, amount: int, timeout: float | None = None) -> int | None:
        if amount <= 0 or amount > self.capacity:
            raise ValueError("reservation must be positive and no larger than capacity")
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
            while ticket != self._serving_ticket or amount > self._available:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    self._cancelled.add(ticket)
                    self._advance_cancelled()
                    self._condition.notify_all()
                    return None
                self._condition.wait(remaining)
            lease = self._next_lease
            self._next_lease += 1
            self._leased[lease] = amount
            self._available -= amount
            self._serving_ticket += 1
            self._advance_cancelled()
            self._condition.notify_all()
            return lease

    def release(self, lease: int) -> None:
        with self._condition:
            try:
                amount = self._leased.pop(lease)
            except KeyError as exc:
                raise ValueError("weighted resource lease is unknown") from exc
            self._available += amount
            self._condition.notify_all()

    def _advance_cancelled(self) -> None:
        while self._serving_ticket in self._cancelled:
            self._cancelled.remove(self._serving_ticket)
            self._serving_ticket += 1


class FeedbackMemoryAdmission:
    """FIFO memory admission against live resident RSS plus future growth.

    A prediction is an incremental commitment, not reclaimed memory.  While a
    tool is running, its commitment remains in addition to measured resident
    RSS so concurrent admissions cannot race ahead of RSS materialization.  At
    tool completion we remeasure RSS before removing only the no-longer-needed
    future-growth commitment; persistent pages remain charged by live RSS.
    """

    def __init__(self, budget_bytes: int, safety_headroom_bytes: int,
                 measure_resident_bytes: Callable[[], int], *, poll_s: float = 0.1) -> None:
        if budget_bytes <= 0:
            raise ValueError("memory admission budget must be positive")
        if safety_headroom_bytes < 0 or safety_headroom_bytes >= budget_bytes:
            raise ValueError("memory safety headroom must be non-negative and below budget")
        if poll_s <= 0:
            raise ValueError("memory admission poll interval must be positive")
        self.budget_bytes = int(budget_bytes)
        self.safety_headroom_bytes = int(safety_headroom_bytes)
        self._measure = measure_resident_bytes
        self._poll_s = float(poll_s)
        self._leased: dict[int, dict[str, Any]] = {}
        self._next_lease = 0
        self._next_ticket = 0
        self._serving_ticket = 0
        self._cancelled: set[int] = set()
        self._last_resident_bytes = 0
        self._peak_resident_bytes = 0
        self._peak_charge_bytes = 0
        self._over_budget_observations = 0
        self._prediction_exceeded_leases = 0
        self._condition = Condition()

    def _read_locked(self, measured_bytes: int | None = None) -> int:
        resident = int(self._measure() if measured_bytes is None else measured_bytes)
        resident = max(0, resident)
        self._last_resident_bytes = resident
        self._peak_resident_bytes = max(self._peak_resident_bytes, resident)
        for item in self._leased.values():
            lease_measure = item["measure_resident_bytes"]
            lease_resident = max(0, int(lease_measure()))
            realized = max(0, lease_resident - int(item["resident_at_admit_bytes"]))
            prediction = int(item["predicted_incremental_bytes"])
            item["realized_growth_bytes"] = realized
            item["unrealized_growth_bytes"] = max(0, prediction - realized)
            if realized > prediction and not bool(item["exceeded"]):
                item["exceeded"] = True
                self._prediction_exceeded_leases += 1
        outstanding = sum(
            int(item["unrealized_growth_bytes"]) for item in self._leased.values()
        )
        charge = resident + outstanding + self.safety_headroom_bytes
        self._peak_charge_bytes = max(self._peak_charge_bytes, charge)
        if charge > self.budget_bytes:
            self._over_budget_observations += 1
        return resident

    def _status_locked(self, resident: int) -> dict[str, int]:
        outstanding = sum(
            int(item["unrealized_growth_bytes"]) for item in self._leased.values()
        )
        charge = resident + outstanding + self.safety_headroom_bytes
        return {
            "resident_bytes": resident,
            "outstanding_unrealized_growth_bytes": outstanding,
            # Compatibility alias for old result readers. The value now has the
            # corrected meaning: only prediction that has not materialized yet.
            "outstanding_incremental_bytes": outstanding,
            "safety_headroom_bytes": self.safety_headroom_bytes,
            "admission_charge_bytes": charge,
            "remaining_headroom_bytes": max(0, self.budget_bytes - charge),
            "budget_bytes": self.budget_bytes,
        }

    def acquire(self, incremental_bytes: int, timeout: float | None = None,
                *, measure_resident_bytes: Callable[[], int] | None = None,
                 ) -> tuple[int, dict[str, int]] | None:
        if incremental_bytes <= 0:
            raise ValueError("predicted incremental memory must be positive")
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
            while True:
                resident = self._read_locked()
                status = self._status_locked(resident)
                projected = status["admission_charge_bytes"] + int(incremental_bytes)
                if ticket == self._serving_ticket and projected <= self.budget_bytes:
                    lease = self._next_lease
                    self._next_lease += 1
                    lease_measure = measure_resident_bytes or self._measure
                    lease_resident = max(0, int(lease_measure()))
                    self._leased[lease] = {
                        "predicted_incremental_bytes": int(incremental_bytes),
                        "resident_at_admit_bytes": lease_resident,
                        "realized_growth_bytes": 0,
                        "unrealized_growth_bytes": int(incremental_bytes),
                        "measure_resident_bytes": lease_measure,
                        "exceeded": False,
                    }
                    self._serving_ticket += 1
                    self._advance_cancelled()
                    admitted = self._status_locked(resident)
                    self._condition.notify_all()
                    return lease, admitted
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    self._cancelled.add(ticket)
                    self._advance_cancelled()
                    self._condition.notify_all()
                    return None
                self._condition.wait(
                    self._poll_s if remaining is None else min(self._poll_s, remaining)
                )

    def release(self, lease: int) -> dict[str, int]:
        with self._condition:
            # Remeasure first.  Removing the speculative growth commitment can
            # never erase resident pages that survived tool completion.
            resident = self._read_locked()
            if lease not in self._leased:
                raise ValueError("memory admission lease is unknown")
            self._leased.pop(lease)
            status = self._status_locked(resident)
            self._condition.notify_all()
            return status

    def observe(self, resident_bytes: int | None = None) -> dict[str, int]:
        """Refresh live RSS so prediction overruns immediately reduce headroom."""
        with self._condition:
            resident = self._read_locked(resident_bytes)
            status = self._status_locked(resident)
            self._condition.notify_all()
            return status

    def metrics(self) -> dict[str, int]:
        with self._condition:
            resident = self._read_locked()
            return {
                **self._status_locked(resident),
                "peak_resident_bytes": self._peak_resident_bytes,
                "peak_admission_charge_bytes": self._peak_charge_bytes,
                "over_budget_observations": self._over_budget_observations,
                "prediction_exceeded_leases": self._prediction_exceeded_leases,
                "realized_predicted_growth_bytes": sum(
                    min(
                        int(item["realized_growth_bytes"]),
                        int(item["predicted_incremental_bytes"]),
                    )
                    for item in self._leased.values()
                ),
                "active_leases": len(self._leased),
            }

    def _advance_cancelled(self) -> None:
        while self._serving_ticket in self._cancelled:
            self._cancelled.remove(self._serving_ticket)
            self._serving_ticket += 1


class AtomicMemoryAdmission:
    """Priority-aware all-or-nothing admission across parent and Tool pools.

    Formal paper runs must never hold capacity in one pool while waiting for
    the other. Normal work is bounded by application admission watermarks;
    calibrated checkpoint/restore transients may use the emergency interval
    below the kernel-enforced hard limits.
    """

    _PRIORITY = {
        "restore": 0,
        "checkpoint": 1,
        "continuation": 2,
        "lifetime": 3,
        "boot": 4,
    }
    _EMERGENCY = {"restore", "checkpoint"}

    def __init__(
        self,
        *,
        parent_high_bytes: int,
        parent_hard_bytes: int,
        parent_headroom_bytes: int,
        tool_high_bytes: int,
        tool_hard_bytes: int,
        tool_headroom_bytes: int,
        measure_parent_bytes: Callable[[], int],
        measure_tool_bytes: Callable[[], int],
        emergency_parent_headroom_bytes: int = 0,
        emergency_tool_headroom_bytes: int = 0,
        poll_s: float = 0.05,
    ) -> None:
        for label, high, hard, headroom, emergency in (
            (
                "parent", parent_high_bytes, parent_hard_bytes,
                parent_headroom_bytes, emergency_parent_headroom_bytes,
            ),
            (
                "tool", tool_high_bytes, tool_hard_bytes,
                tool_headroom_bytes, emergency_tool_headroom_bytes,
            ),
        ):
            if not 0 < int(high) < int(hard):
                raise ValueError(f"{label} memory limits must satisfy 0 < high < hard")
            if not 0 <= int(headroom) < int(high):
                raise ValueError(f"{label} normal headroom is invalid")
            if not 0 <= int(emergency) < int(hard):
                raise ValueError(f"{label} emergency headroom is invalid")
        if poll_s <= 0:
            raise ValueError("memory admission poll interval must be positive")
        self.parent_high_bytes = int(parent_high_bytes)
        self.parent_hard_bytes = int(parent_hard_bytes)
        self.parent_headroom_bytes = int(parent_headroom_bytes)
        self.tool_high_bytes = int(tool_high_bytes)
        self.tool_hard_bytes = int(tool_hard_bytes)
        self.tool_headroom_bytes = int(tool_headroom_bytes)
        self.emergency_parent_headroom_bytes = int(
            emergency_parent_headroom_bytes
        )
        self.emergency_tool_headroom_bytes = int(emergency_tool_headroom_bytes)
        self._measure_parent = measure_parent_bytes
        self._measure_tool = measure_tool_bytes
        self._poll_s = float(poll_s)
        self._condition = Condition()
        self._next_ticket = 0
        self._next_lease = 0
        self._waiters: dict[int, dict[str, Any]] = {}
        self._leased: dict[int, dict[str, Any]] = {}
        self._completed: list[dict[str, Any]] = []
        self._timed_out: list[dict[str, Any]] = []
        self._peak_parent_charge = 0
        self._peak_tool_charge = 0
        self._peak_parent_resident = 0
        self._peak_tool_resident = 0
        self._parent_over_high_observations = 0
        self._tool_over_high_observations = 0
        self._parent_over_hard_observations = 0
        self._tool_over_hard_observations = 0

    def acquire(
        self,
        *,
        parent_increment_bytes: int,
        tool_increment_bytes: int,
        request_class: str,
        timeout: float | None = None,
        measure_parent_bytes: Callable[[], int] | None = None,
        measure_tool_bytes: Callable[[], int] | None = None,
    ) -> tuple[int, dict[str, Any]] | None:
        if request_class not in self._PRIORITY:
            raise ValueError(f"unknown memory request class: {request_class}")
        parent_increment = int(parent_increment_bytes)
        tool_increment = int(tool_increment_bytes)
        if parent_increment < 0 or tool_increment < 0 or not (
            parent_increment or tool_increment
        ):
            raise ValueError("at least one non-negative memory increment is required")
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            enqueued = time.monotonic()
            waiter = {
                "ticket": ticket,
                "request_class": request_class,
                "priority": self._PRIORITY[request_class],
                "parent_increment_bytes": parent_increment,
                "tool_increment_bytes": tool_increment,
            }
            self._waiters[ticket] = waiter
            deadline = None if timeout is None else enqueued + max(0.0, timeout)
            try:
                while True:
                    status = self._status_locked()
                    selected = self._selected_waiter_locked(status)
                    if selected is waiter:
                        lease_id = self._next_lease
                        self._next_lease += 1
                        parent_measure = measure_parent_bytes or self._measure_parent
                        tool_measure = measure_tool_bytes or self._measure_tool
                        lease = {
                            **waiter,
                            "lease": lease_id,
                            "admitted_monotonic": time.monotonic(),
                            "wait_s": time.monotonic() - enqueued,
                            "parent_at_admit_bytes": max(0, int(parent_measure())),
                            "tool_at_admit_bytes": max(0, int(tool_measure())),
                            "parent_realized_growth_bytes": 0,
                            "tool_realized_growth_bytes": 0,
                            "parent_unrealized_growth_bytes": parent_increment,
                            "tool_unrealized_growth_bytes": tool_increment,
                            "parent_peak_growth_bytes": 0,
                            "tool_peak_growth_bytes": 0,
                            "measure_parent_bytes": parent_measure,
                            "measure_tool_bytes": tool_measure,
                        }
                        self._leased[lease_id] = lease
                        self._waiters.pop(ticket, None)
                        admitted = self._status_locked()
                        self._condition.notify_all()
                        return lease_id, {
                            **admitted,
                            "request_class": request_class,
                            "wait_s": lease["wait_s"],
                        }
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        self._waiters.pop(ticket, None)
                        self._timed_out.append({
                            **waiter,
                            "wait_s": time.monotonic() - enqueued,
                        })
                        self._condition.notify_all()
                        return None
                    self._condition.wait(
                        self._poll_s
                        if remaining is None else min(self._poll_s, remaining)
                    )
            except BaseException:
                self._waiters.pop(ticket, None)
                self._condition.notify_all()
                raise

    def release(self, lease_id: int) -> dict[str, Any]:
        with self._condition:
            self._refresh_locked()
            lease = self._leased.pop(lease_id, None)
            if lease is None:
                raise ValueError("memory admission lease is unknown")
            completed = {
                key: value
                for key, value in lease.items()
                if not key.startswith("measure_")
            }
            completed["released_monotonic"] = time.monotonic()
            completed["held_s"] = (
                completed["released_monotonic"]
                - float(completed["admitted_monotonic"])
            )
            completed["parent_prediction_error_bytes"] = (
                int(completed["parent_peak_growth_bytes"])
                - int(completed["parent_increment_bytes"])
            )
            completed["tool_prediction_error_bytes"] = (
                int(completed["tool_peak_growth_bytes"])
                - int(completed["tool_increment_bytes"])
            )
            completed["parent_underprediction_bytes"] = max(
                0, int(completed["parent_prediction_error_bytes"])
            )
            completed["tool_underprediction_bytes"] = max(
                0, int(completed["tool_prediction_error_bytes"])
            )
            self._completed.append(completed)
            status = self._status_locked()
            self._condition.notify_all()
            return {**status, "completed_lease": completed}

    def observe(self) -> dict[str, Any]:
        with self._condition:
            status = self._status_locked()
            self._condition.notify_all()
            return status

    def record_lease_growth(
        self, lease_id: int, *, parent_growth_bytes: int, tool_growth_bytes: int,
    ) -> None:
        """Merge an operation-local kernel peak into one admission lease."""
        with self._condition:
            lease = self._leased.get(lease_id)
            if lease is None:
                raise ValueError("memory admission lease is unknown")
            parent_growth = max(0, int(parent_growth_bytes))
            tool_growth = max(0, int(tool_growth_bytes))
            lease["parent_peak_growth_bytes"] = max(
                int(lease["parent_peak_growth_bytes"]), parent_growth,
            )
            lease["tool_peak_growth_bytes"] = max(
                int(lease["tool_peak_growth_bytes"]), tool_growth,
            )
            self._condition.notify_all()

    def metrics(self) -> dict[str, Any]:
        with self._condition:
            status = self._status_locked()
            return {
                **status,
                "active_leases": len(self._leased),
                "queued_requests": len(self._waiters),
                "peak_parent_admission_charge_bytes": self._peak_parent_charge,
                "peak_tool_admission_charge_bytes": self._peak_tool_charge,
                "peak_parent_resident_bytes": self._peak_parent_resident,
                "peak_tool_resident_bytes": self._peak_tool_resident,
                "parent_over_high_observations": self._parent_over_high_observations,
                "tool_over_high_observations": self._tool_over_high_observations,
                "parent_over_hard_observations": self._parent_over_hard_observations,
                "tool_over_hard_observations": self._tool_over_hard_observations,
                "parent_prediction_exceeded_leases": sum(
                    int(item["parent_underprediction_bytes"]) > 0
                    for item in self._completed
                ),
                "tool_prediction_exceeded_leases": sum(
                    int(item["tool_underprediction_bytes"]) > 0
                    for item in self._completed
                ),
                "timed_out_requests": [dict(item) for item in self._timed_out],
                "queued_by_class": {
                    request_class: sum(
                        item["request_class"] == request_class
                        for item in self._waiters.values()
                    )
                    for request_class in self._PRIORITY
                },
                "completed_leases": [dict(item) for item in self._completed],
            }

    def _refresh_locked(self) -> tuple[int, int]:
        parent_resident = max(0, int(self._measure_parent()))
        tool_resident = max(0, int(self._measure_tool()))
        for lease in self._leased.values():
            parent_growth = max(
                0,
                int(lease["measure_parent_bytes"]())
                - int(lease["parent_at_admit_bytes"]),
            )
            tool_growth = max(
                0,
                int(lease["measure_tool_bytes"]())
                - int(lease["tool_at_admit_bytes"]),
            )
            lease["parent_realized_growth_bytes"] = parent_growth
            lease["tool_realized_growth_bytes"] = tool_growth
            lease["parent_peak_growth_bytes"] = max(
                int(lease["parent_peak_growth_bytes"]), parent_growth,
            )
            lease["tool_peak_growth_bytes"] = max(
                int(lease["tool_peak_growth_bytes"]), tool_growth,
            )
            lease["parent_unrealized_growth_bytes"] = max(
                0, int(lease["parent_increment_bytes"]) - parent_growth,
            )
            lease["tool_unrealized_growth_bytes"] = max(
                0, int(lease["tool_increment_bytes"]) - tool_growth,
            )
        return parent_resident, tool_resident

    def _status_locked(self) -> dict[str, Any]:
        parent_resident, tool_resident = self._refresh_locked()
        parent_unrealized = sum(
            int(item["parent_unrealized_growth_bytes"])
            for item in self._leased.values()
        )
        tool_unrealized = sum(
            int(item["tool_unrealized_growth_bytes"])
            for item in self._leased.values()
        )
        parent_charge = parent_resident + parent_unrealized
        tool_charge = tool_resident + tool_unrealized
        self._peak_parent_resident = max(
            self._peak_parent_resident, parent_resident,
        )
        self._peak_tool_resident = max(self._peak_tool_resident, tool_resident)
        self._peak_parent_charge = max(self._peak_parent_charge, parent_charge)
        self._peak_tool_charge = max(self._peak_tool_charge, tool_charge)
        self._parent_over_high_observations += parent_resident > self.parent_high_bytes
        self._tool_over_high_observations += tool_resident > self.tool_high_bytes
        self._parent_over_hard_observations += parent_resident > self.parent_hard_bytes
        self._tool_over_hard_observations += tool_resident > self.tool_hard_bytes
        return {
            "parent_resident_bytes": parent_resident,
            "tool_resident_bytes": tool_resident,
            "parent_outstanding_unrealized_bytes": parent_unrealized,
            "tool_outstanding_unrealized_bytes": tool_unrealized,
            "parent_admission_charge_bytes": parent_charge,
            "tool_admission_charge_bytes": tool_charge,
            "parent_high_bytes": self.parent_high_bytes,
            "tool_high_bytes": self.tool_high_bytes,
            "parent_hard_bytes": self.parent_hard_bytes,
            "tool_hard_bytes": self.tool_hard_bytes,
        }

    def _fits_locked(self, waiter: dict[str, Any], status: dict[str, Any]) -> bool:
        emergency = waiter["request_class"] in self._EMERGENCY
        parent_limit = (
            self.parent_hard_bytes - self.emergency_parent_headroom_bytes
            if emergency else self.parent_high_bytes - self.parent_headroom_bytes
        )
        tool_limit = (
            self.tool_hard_bytes - self.emergency_tool_headroom_bytes
            if emergency else self.tool_high_bytes - self.tool_headroom_bytes
        )
        return (
            int(status["parent_admission_charge_bytes"])
            + int(waiter["parent_increment_bytes"])
            <= parent_limit
            and int(status["tool_admission_charge_bytes"])
            + int(waiter["tool_increment_bytes"])
            <= tool_limit
        )

    def _selected_waiter_locked(
        self, status: dict[str, Any],
    ) -> dict[str, Any] | None:
        ordered = sorted(
            self._waiters.values(),
            key=lambda item: (int(item["priority"]), int(item["ticket"])),
        )
        if not ordered:
            return None
        first = ordered[0]
        if self._fits_locked(first, status):
            return first
        # Only a reclaiming checkpoint may bypass an infeasible higher-priority
        # request. Letting ordinary work bypass it could consume the very memory
        # needed by a response-critical restore.
        for waiter in ordered[1:]:
            if (
                waiter["request_class"] == "checkpoint"
                and self._fits_locked(waiter, status)
            ):
                return waiter
        return None
