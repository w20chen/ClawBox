from __future__ import annotations

import base64
import errno
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Any, Protocol


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
    boot_timeout_s: float = 30.0

    @classmethod
    def from_json(cls, path: Path) -> "FirecrackerConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        path_fields = {
            "binary", "api_socket", "kernel_image", "rootfs",
            "snapshot_state", "snapshot_memory", "log_path", "vsock_uds",
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

    @property
    def resident(self) -> bool:
        return self._resident

    def process_exit_code(self) -> int | None:
        """Return the Firecracker exit code, or None while it is running."""
        process = self._process
        return None if process is None or process.poll() is None else process.returncode

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
        api.request("PUT", "/actions", {"action_type": "InstanceStart"})
        self._resident = True
        return time.monotonic() - started

    def checkpoint_and_evict(self) -> float:
        if not self._resident or self._process is None:
            raise LifecycleError("cannot checkpoint a non-resident VM")
        started = time.monotonic()
        api = _UnixHttpClient(self.config.api_socket, self.config.api_timeout_s)
        api.request("PATCH", "/vm", {"state": "Paused"})
        next_state, next_memory = self._next_snapshot_paths()
        next_state.parent.mkdir(parents=True, exist_ok=True)
        next_memory.parent.mkdir(parents=True, exist_ok=True)
        try:
            api.request("PUT", "/snapshot/create", {
                "snapshot_type": "Full",
                "snapshot_path": str(next_state),
                "mem_file_path": str(next_memory),
            })
        except Exception:
            # Snapshot failure must not strand a healthy agent in Paused state.
            try:
                api.request("PATCH", "/vm", {"state": "Resumed"})
            finally:
                next_state.unlink(missing_ok=True)
                next_memory.unlink(missing_ok=True)
            raise
        previous_state = self._snapshot_state_path
        previous_memory = self._snapshot_memory_path
        self._stop_process()
        self._snapshot_state_path = next_state
        self._snapshot_memory_path = next_memory
        self._drop_snapshot_page_cache(next_state, next_memory)
        # A restored VM MAP_PRIVATE-maps its memory snapshot. Reusing that path
        # as the output of the next snapshot can corrupt both the VM and file.
        # Alternate paths, stop the mapping process, then retire the old pair.
        for old_path in (previous_state, previous_memory):
            if old_path is not None and old_path not in {next_state, next_memory}:
                old_path.unlink(missing_ok=True)
        self._resident = False
        self._has_snapshot = True
        return time.monotonic() - started

    def restore(self) -> float:
        if self._resident or self._process is not None:
            raise LifecycleError("cannot restore while VM is resident")
        if not self._has_snapshot:
            raise LifecycleError("cannot restore before a snapshot exists")
        if self._snapshot_state_path is None or self._snapshot_memory_path is None:
            raise LifecycleError("snapshot paths are unavailable")
        started = time.monotonic()
        self._spawn()
        try:
            api = _UnixHttpClient(self.config.api_socket, self.config.api_timeout_s)
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
        except Exception:
            self._stop_process()
            raise
        self._resident = True
        return time.monotonic() - started

    def close(self) -> None:
        self._stop_process()
        self._resident = False

    def rss_bytes(self) -> int:
        process = self._process
        if process is None or process.poll() is not None:
            return 0
        try:
            fields = Path(f"/proc/{process.pid}/statm").read_text(encoding="ascii").split()
            return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, IndexError, ValueError):
            return 0

    def _spawn(self) -> None:
        socket_path = self.config.api_socket
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            if not socket_path.is_socket():
                raise LifecycleError(f"refusing to remove non-socket API path: {socket_path}")
            socket_path.unlink()
        self._remove_stale_vsock()
        argv: list[str] = []
        if self.config.numa_node is not None or self.config.cpu_set:
            if self.config.numa_node is None or not self.config.cpu_set:
                raise LifecycleError("NUMA node and CPU set must be configured together")
            argv.extend([
                sys.executable, str(Path(__file__).with_name("_numa_exec.py")),
                "--numa-node", str(self.config.numa_node),
                "--cpu-set", self.config.cpu_set, "--",
            ])
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

    def __init__(self, inner: SandboxLifecycle, slots: BoundedSemaphore) -> None:
        self.inner = inner
        self.slots = slots
        self._owns_slot = False
        self._lock = Lock()

    @property
    def resident(self) -> bool:
        return self.inner.resident

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
        self.slots.acquire()
        with self._lock:
            self._owns_slot = True

    def _release(self) -> None:
        with self._lock:
            if not self._owns_slot:
                return
            self._owns_slot = False
        self.slots.release()
