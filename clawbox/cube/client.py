from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Any

from requests import ConnectionError as RequestsConnectionError, Timeout as RequestsTimeout

from .api_retry import read_with_backoff, retry_with_backoff


@dataclass(frozen=True, slots=True)
class Ownership:
    run_id: str
    attempt_id: str
    task_uid: str
    experiment_id: str
    session_id: str
    policy_name: str

    def metadata(self) -> dict[str, str]:
        return {f"clawbox.{key.removesuffix('_id').replace('_', '-')}": value for key, value in asdict(self).items()}


@dataclass(frozen=True, slots=True)
class CubeSandboxTcpEndpoint:
    """Semantic raw TCP endpoint returned by CubeSandbox for one port."""

    sandbox_id: str
    container_port: int
    address: str


class OwnedSandboxJournal:
    """Durable primary cleanup index for one worker attempt."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()

    def record(self, sandbox_id: str, ownership: Ownership) -> None:
        record = {"sandbox_id": sandbox_id, "ownership": asdict(ownership)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())

    def sandbox_ids(self, *, task_uid: str | None = None) -> list[str]:
        if not self.path.exists():
            return []
        found: list[str] = []
        seen: set[str] = set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                sandbox_id = str(record["sandbox_id"])
                owner = record.get("ownership") or {}
            except (ValueError, KeyError, TypeError):
                continue
            if task_uid is not None and owner.get("task_uid") != task_uid:
                continue
            if sandbox_id not in seen:
                seen.add(sandbox_id)
                found.append(sandbox_id)
        return found


class CubeSandboxClient:
    """Small synchronous wrapper around the official CubeSandbox v0.7 SDK."""

    def __init__(self, *, journal: OwnedSandboxJournal | None = None,
                 sandbox_class: type | None = None, template_class: type | None = None,
                 config: Any = None,
                 request_timeout_s: float = 130.0,
                 command_stream_grace_s: float = 15.0,
                 tcp_endpoint_attempts: int = 10,
                 tcp_endpoint_initial_delay_s: float = 0.25,
                 tcp_endpoint_max_delay_s: float = 2.0,
                 cleanup_workers: int = 8) -> None:
        if request_timeout_s <= 0:
            raise ValueError("Cube API request timeout must be positive")
        if sandbox_class is None:
            from cubesandbox import Config, Sandbox
            sandbox_class = Sandbox
            if config is None:
                config = Config(request_timeout=request_timeout_s)
        self._sandbox_class = sandbox_class
        self._template_class = template_class
        self._config = config
        self.journal = journal
        self._handles: dict[str, Any] = {}
        if command_stream_grace_s < 0:
            raise ValueError("command_stream_grace_s must be non-negative")
        if tcp_endpoint_attempts < 1:
            raise ValueError("tcp_endpoint_attempts must be positive")
        if tcp_endpoint_initial_delay_s < 0 or tcp_endpoint_max_delay_s < 0:
            raise ValueError("TCP endpoint retry delays must be non-negative")
        if cleanup_workers < 1:
            raise ValueError("cleanup_workers must be positive")
        self.command_stream_grace_s = float(command_stream_grace_s)
        self.tcp_endpoint_attempts = int(tcp_endpoint_attempts)
        self.tcp_endpoint_initial_delay_s = float(tcp_endpoint_initial_delay_s)
        self.tcp_endpoint_max_delay_s = float(tcp_endpoint_max_delay_s)
        self.cleanup_workers = int(cleanup_workers)
        self._delete_slots = threading.BoundedSemaphore(self.cleanup_workers)

    def _sdk_kwargs(self) -> dict[str, Any]:
        return {} if self._config is None else {"config": self._config}

    @staticmethod
    def sandbox_id(sandbox: Any) -> str:
        return str(sandbox.sandbox_id)

    def create_sandbox(self, *, template: str, node_name: str,
                       ownership: Ownership, env_vars: dict[str, str] | None = None,
                       allow_internet_access: bool = True,
                       network_allow_out: list[str] | None = None,
                       network_deny_out: list[str] | None = None) -> Any:
        network = {}
        if network_allow_out:
            network["allow_out"] = network_allow_out
        if network_deny_out:
            network["deny_out"] = network_deny_out
        ownership_metadata = ownership.metadata()
        try:
            sandbox = self._sandbox_class.create(
                template=template,
                timeout=-1,  # cubesandbox.NEVER_TIMEOUT in SDK v0.7.0
                lifecycle={"on_timeout": "kill", "auto_resume": False},
                metadata=ownership_metadata,
                distribution_scope=[node_name],
                env_vars=env_vars,
                allow_internet_access=allow_internet_access,
                network=network or None,
                **self._sdk_kwargs(),
            )
        except Exception as create_error:
            # The SDK raises ApiError (not requests.Timeout) for an HTTP
            # timeout/gateway failure. A dropped connection is ambiguous too.
            # Definite rejections and programming errors must still propagate.
            if not (
                isinstance(create_error, (RequestsTimeout, RequestsConnectionError))
                or getattr(create_error, "status_code", None) in {408, 502, 503, 504}
            ):
                raise
            # A read timeout does not prove that CubeAPI rejected the create.
            # Never repeat this non-idempotent POST. Resolve its unique
            # ownership identity until the bounded reconciliation window ends.
            def resolve_timed_out_create() -> dict[str, Any]:
                matches = [
                    item for item in self._sandbox_class.list_v2(**self._sdk_kwargs())
                    if all(
                        (item.get("metadata") or {}).get(key) == value
                        for key, value in ownership_metadata.items()
                    )
                ]
                if len(matches) != 1:
                    raise RuntimeError(
                        "expected exactly one sandbox after timed-out create, "
                        f"found {len(matches)}"
                    )
                return matches[0]

            try:
                recovered = read_with_backoff(
                    resolve_timed_out_create,
                    label=f"ownership reconciliation for {ownership.session_id}",
                    attempts=10, initial_delay_s=0.25, max_delay_s=2.0,
                )
            except Exception as reconciliation_error:
                raise RuntimeError(
                    "CubeSandbox create timed out and ownership reconciliation "
                    f"failed for {ownership.session_id}"
                ) from reconciliation_error
            sandbox = self._sandbox_class(recovered, self._config)
            # Listing proves ownership, not readiness: CubeAPI can list a
            # sandbox while its VM is still being restored. Journal it before
            # probing so failed recovery remains eligible for owned cleanup.
            if self.journal is not None:
                self.journal.record(self.sandbox_id(sandbox), ownership)

            def verify_recovered_guest() -> None:
                result = sandbox.commands.run("/bin/true", timeout=5)
                if result.exit_code != 0:
                    raise RuntimeError("recovered sandbox guest is not ready")

            read_with_backoff(
                verify_recovered_guest,
                label=f"guest readiness after timed-out create for {ownership.session_id}",
                attempts=10, initial_delay_s=0.25, max_delay_s=2.0,
            )
        sandbox_id = self.sandbox_id(sandbox)
        self._handles[sandbox_id] = sandbox
        if self.journal is not None:
            self.journal.record(sandbox_id, ownership)
        return sandbox

    def connect_sandbox(self, sandbox_id: str, *,
                        snapshot_mechanism: str = "incremental-cow") -> Any:
        # Resuming the same immutable sandbox ID is idempotent. CubeMaster can
        # transiently reject or drop HTTP requests during a large restore
        # wave, so retry the control-plane transport without creating a new VM.
        sandbox = retry_with_backoff(
            lambda: self._sandbox_class.connect(
                sandbox_id, **self._sdk_kwargs(),
                **({"snapshot_mechanism": snapshot_mechanism}
                   if snapshot_mechanism != "full-copy" else {}),
            ),
            label=f"resume sandbox {sandbox_id}",
            attempts=10,
            initial_delay_s=0.25,
            max_delay_s=2.0,
        )
        self._handles[sandbox_id] = sandbox
        return sandbox

    def get_sandbox_state(self, sandbox_or_id: Any) -> str | None:
        if not isinstance(sandbox_or_id, str):
            getter = getattr(sandbox_or_id, "get_info", None)
            if callable(getter):
                info = getter()
                return str(info.get("state", "")).lower() or None
            sandbox_id = self.sandbox_id(sandbox_or_id)
        else:
            sandbox_id = sandbox_or_id
        for info in self._sandbox_class.list_v2(**self._sdk_kwargs()):
            if self._info_id(info) == sandbox_id:
                return str(info.get("state", "")).lower() or None
        return None

    def get_tcp_endpoint(self, sandbox: Any, container_port: int = 2222) -> CubeSandboxTcpEndpoint:
        """Ask CubeSandbox for the raw TCP endpoint of a sandbox port.

        The ClawBox layer consumes only this semantic contract. It does not
        inspect CubeProxy Redis fields, guest IPs, or host-port metadata.
        """
        if isinstance(container_port, bool) or not isinstance(container_port, int):
            raise ValueError("container_port must be an integer between 1 and 65535")
        if not 1 <= container_port <= 65535:
            raise ValueError("container_port must be between 1 and 65535")
        getter = getattr(sandbox, "get_tcp_endpoint", None)
        if not callable(getter):
            raise RuntimeError(
                "CubeSandbox SDK must expose get_tcp_endpoint(container_port)"
            )
        # CubeAPI acknowledges sandbox creation/resume before CubeMaster has
        # published the corresponding L4 route record.  Retry only this
        # read-after-lifecycle boundary; the endpoint remains wholly owned by
        # CubeSandbox and no cached or alternate route is introduced.
        try:
            raw = read_with_backoff(
                lambda: getter(container_port),
                label=(f"TCP endpoint for {self.sandbox_id(sandbox)}:{container_port}"),
                attempts=self.tcp_endpoint_attempts,
                initial_delay_s=self.tcp_endpoint_initial_delay_s,
                max_delay_s=self.tcp_endpoint_max_delay_s,
            )
        finally:
            # The SDK keeps one requests connection pool on every live sandbox
            # handle. Endpoint discovery is a discrete control-plane operation;
            # retaining hundreds of its idle sockets exhausts the worker's
            # default RLIMIT_NOFILE before the offered concurrency is reached.
            # Public Sandbox.close() only closes cached clients; it does not
            # destroy the VM, and later operations rebuild them lazily.
            close = getattr(sandbox, "close", None)
            if callable(close):
                close()
        if isinstance(raw, Mapping):
            sandbox_id = raw.get("sandboxID") or raw.get("sandbox_id")
            returned_port = raw.get("containerPort") or raw.get("container_port")
            address = raw.get("address")
        else:
            sandbox_id = getattr(raw, "sandbox_id", None)
            returned_port = getattr(raw, "container_port", None)
            address = getattr(raw, "address", None)
        endpoint = CubeSandboxTcpEndpoint(
            sandbox_id=str(sandbox_id or ""),
            container_port=int(returned_port or 0),
            address=str(address or "").strip(),
        )
        expected_id = self.sandbox_id(sandbox)
        if endpoint.sandbox_id != expected_id:
            raise RuntimeError(
                f"CubeSandbox endpoint identity mismatch: expected {expected_id!r}, "
                f"got {endpoint.sandbox_id!r}"
            )
        if endpoint.container_port != container_port:
            raise RuntimeError(
                f"CubeSandbox endpoint port mismatch: expected {container_port}, "
                f"got {endpoint.container_port}"
            )
        if not endpoint.address:
            raise RuntimeError("CubeSandbox returned an empty raw TCP endpoint")
        return endpoint

    def update_network(self, sandbox: Any, *, allow_internet_access: bool,
                       network_allow_out: list[str] | None = None,
                       network_deny_out: list[str] | None = None) -> None:
        """Replace a running sandbox's egress policy through CubeSandbox.

        This is the official SDK network operation, kept behind the same small
        client boundary as create/pause/restore.  It is used only when a
        freshly resolved semantic TCP endpoint adds a new endpoint host to a
        Runtime's closed-network allowlist; it does not inspect or construct
        CubeProxy routing metadata.
        """
        updater = getattr(sandbox, "update_network", None)
        if not callable(updater):
            raise RuntimeError(
                "CubeSandbox SDK must expose update_network(network) for "
                "endpoint-host refresh"
            )
        network: dict[str, Any] = {
            "allow_internet_access": bool(allow_internet_access),
            "allow_out": list(network_allow_out or []),
            "deny_out": list(network_deny_out or []),
        }
        updater(network)

    def validate_template_image(self, template: str, expected_digest: str) -> dict[str, str]:
        """Fail closed when a pinned experiment template is not its image.

        Template provenance is checked through CubeSandbox's official Template
        API. This is a creation-time artifact gate, separate from the semantic
        per-sandbox TCP endpoint API and from any CubeProxy routing metadata.
        """
        reference = str(template or "").strip()
        digest = str(expected_digest or "").strip()
        if not reference or not digest:
            raise ValueError("template and expected image digest are required")
        template_class = self._template_class
        if template_class is None:
            from cubesandbox import Template
            template_class = Template
        info = template_class.get(reference)
        status = str(getattr(info, "status", "")).strip().lower()
        if status not in {"ready", "succeeded", "success", "completed"}:
            raise RuntimeError(f"CubeSandbox template {reference!r} is not ready: {status}")
        image_info = str(getattr(info, "image_info", "") or "").strip()
        if not image_info:
            # CubeAPI v0.7.0's detail response can omit imageInfo even though
            # the official list response carries CubeMaster's persisted
            # source-image provenance. Require one exact template-ID match;
            # absence or ambiguity still fails closed below.
            lister = getattr(template_class, "list", None)
            if callable(lister):
                matches = [
                    item for item in lister()
                    if str(getattr(item, "template_id", "")).strip() == reference
                ]
                if len(matches) == 1:
                    image_info = str(
                        getattr(matches[0], "image_info", "") or ""
                    ).strip()
        actual_digest = image_info.rsplit("@", 1)[-1] if "@" in image_info else ""
        if actual_digest != digest:
            raise RuntimeError(
                f"CubeSandbox template {reference!r} image digest mismatch: "
                f"expected {digest!r}, got {actual_digest or image_info!r}"
            )
        return {"template": reference, "status": status, "image_info": image_info,
                "image_digest": actual_digest}

    def validate_template_images(
        self, expected_digests: Mapping[str, str],
    ) -> dict[str, dict[str, str]]:
        """Validate many pinned templates from one official inventory read."""
        expected = {
            str(template or "").strip(): str(digest or "").strip()
            for template, digest in expected_digests.items()
        }
        if not expected or any(not template or not digest
                               for template, digest in expected.items()):
            raise ValueError("template and expected image digest are required")
        template_class = self._template_class
        if template_class is None:
            from cubesandbox import Template
            template_class = Template
        inventory: dict[str, Any] = {}
        duplicates: set[str] = set()
        for item in template_class.list():
            reference = str(getattr(item, "template_id", "") or "").strip()
            if reference in expected:
                if reference in inventory:
                    duplicates.add(reference)
                inventory[reference] = item
        if duplicates:
            raise RuntimeError(
                "CubeSandbox template inventory contains duplicate IDs: "
                f"{sorted(duplicates)}"
            )
        missing = sorted(set(expected) - set(inventory))
        if missing:
            raise RuntimeError(
                f"CubeSandbox template inventory is missing: {missing}"
            )
        validated = {}
        for reference, digest in expected.items():
            info = inventory[reference]
            status = str(getattr(info, "status", "")).strip().lower()
            if status not in {"ready", "succeeded", "success", "completed"}:
                raise RuntimeError(
                    f"CubeSandbox template {reference!r} is not ready: {status}"
                )
            image_info = str(getattr(info, "image_info", "") or "").strip()
            actual_digest = image_info.rsplit("@", 1)[-1] if "@" in image_info else ""
            if actual_digest != digest:
                raise RuntimeError(
                    f"CubeSandbox template {reference!r} image digest mismatch: "
                    f"expected {digest!r}, got {actual_digest or image_info!r}"
                )
            validated[reference] = {
                "template": reference,
                "status": status,
                "image_info": image_info,
                "image_digest": actual_digest,
            }
        return validated

    def run_command(self, sandbox: Any, command: str, *, timeout_s: float,
                    cwd: str = "/workspace") -> Any:
        """Run a command without allowing a broken stream to block forever.

        CubeSandbox's e2b-connect transport intentionally leaves stream reads
        unbounded and relies on the server-side command deadline.  A proxy or
        a partially closed stream can therefore outlive that deadline.  Keep
        the SDK call in a daemon thread and enforce the same deadline locally,
        with a short grace period for normal deadline propagation.  The owning
        session fails and its normal cleanup path kills the sandbox; the
        daemon thread is only retained for the broken transport edge case and
        cannot keep the worker process alive.
        """
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                result_queue.put(("result", sandbox.commands.run(
                    command, timeout=timeout_s, cwd=cwd,
                )))
            except BaseException as exc:  # re-raise in the caller's context
                result_queue.put(("error", exc))

        thread = threading.Thread(target=invoke, name="cube-command", daemon=True)
        thread.start()
        thread.join(timeout_s + self.command_stream_grace_s)
        if thread.is_alive():
            raise TimeoutError(
                "CubeSandbox command stream exceeded deadline "
                f"({timeout_s:g}s + {self.command_stream_grace_s:g}s grace)"
            )
        kind, value = result_queue.get_nowait()
        if kind == "error":
            raise value
        return value

    @staticmethod
    def file_nonempty(sandbox: Any, path: str, *, timeout_s: float) -> bool:
        """Probe completion through Cube's bounded native filesystem RPC."""
        from cubesandbox import FilesystemNotFoundError

        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        def probe() -> bool:
            try:
                entry = sandbox.files.stat(path, timeout=timeout_s)
            except FilesystemNotFoundError:
                return False
            # Stat's size is an int64 encoded as a string by Connect JSON. An
            # existing but empty exit file is still being written by the Agent.
            return int(entry.get("size", 0)) > 0

        # Match the former read-only command poll's bounded transport retries.
        return read_with_backoff(
            probe, label=f"stat {path}", attempts=8,
            initial_delay_s=0.1, max_delay_s=1.0,
        )

    @staticmethod
    def wait_file_nonempty(sandbox: Any, path: str, *, timeout_s: float) -> bool:
        """Wait on Cube's filesystem notifications, including subscribe races."""
        import httpx
        from cubesandbox import FilesystemNotFoundError

        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        deadline = time.monotonic() + timeout_s
        stat_timed_out = False

        def ready() -> bool:
            nonlocal stat_timed_out
            try:
                entry = sandbox.files.stat(
                    path, timeout=max(0.001, deadline - time.monotonic()),
                )
            except FilesystemNotFoundError:
                return False
            except (httpx.ReadTimeout, httpx.ConnectError):
                # Under Full load, a bounded Stat can consume the remaining
                # watch window before response headers arrive. That is the
                # normal "not ready within this window" outcome. Cube's
                # endpoint-name lookup can also fail transiently while its DNS
                # registry changes under high churn. The caller rechecks
                # cancellation/residency and retries within the session's
                # existing command deadline.
                stat_timed_out = True
                return False
            return int(entry.get("size", 0)) > 0

        if ready():
            return True
        if stat_timed_out:
            return False
        # Subscribe, then stat again: the file may have been written between
        # the first stat and establishment of the notification stream.
        try:
            with sandbox.files.watch_dir(
                str(PurePosixPath(path).parent),
                timeout=max(0.001, deadline - time.monotonic()),
            ) as watcher:
                if ready():
                    return True
                for event in watcher:
                    if PurePosixPath(event.name).name == PurePosixPath(path).name and ready():
                        return True
                    if stat_timed_out:
                        break
                    if time.monotonic() >= deadline:
                        break
        except (httpx.ReadTimeout, httpx.ConnectError):
            # An idle notification stream is expected to reach this bounded
            # wait. A transient endpoint lookup failure is equivalent here:
            # the caller checks cancellation and residency before retry.
            return False
        return False

    @staticmethod
    def read_file(sandbox: Any, path: str) -> str:
        return sandbox.files.read(path)

    @staticmethod
    def write_file(sandbox: Any, path: str, data: str | bytes) -> None:
        sandbox.files.write(path, data)

    @staticmethod
    def pause_sandbox(sandbox: Any, *, tier: str | None = None,
                      memory_snapshot_path: str | None = None,
                      generation: int | None = None,
                      snapshot_mechanism: str = "incremental-cow") -> Mapping[str, Any] | None:
        if tier is None:
            if snapshot_mechanism != "full-copy":
                raise ValueError("incremental-cow requires an explicit snapshot tier and path")
            sandbox.pause(wait=True)
            return None
        if not memory_snapshot_path or generation is None:
            raise ValueError("tiered pause requires memory_snapshot_path and generation")
        try:
            result = sandbox.pause(
                wait=True, snapshot_tier=tier,
                memory_snapshot_path=memory_snapshot_path,
                snapshot_generation=generation,
                **({"snapshot_mechanism": snapshot_mechanism}
                   if snapshot_mechanism != "full-copy" else {}),
            )
        except TypeError as exc:
            raise RuntimeError(
                "installed CubeSandbox SDK lacks the required direct tiered pause API"
            ) from exc
        if not isinstance(result, Mapping):
            raise RuntimeError("tiered pause did not return a snapshot manifest")
        required = {"memory_snapshot_path", "generation", "tier"}
        if missing := sorted(required.difference(result)):
            raise RuntimeError("tiered pause manifest missing: " + ", ".join(missing))
        if str(result["tier"]).lower() != tier or int(result["generation"]) != generation:
            raise RuntimeError("tiered pause manifest identity mismatch")
        if str(result["memory_snapshot_path"]) != memory_snapshot_path:
            raise RuntimeError("tiered pause used an unexpected memory snapshot path")
        from .snapshot import read_snapshot_metadata
        if snapshot_mechanism == "incremental-cow" or "logical_bytes" not in result or "allocated_bytes" not in result:
            try:
                metadata = read_snapshot_metadata(
                    memory_snapshot_path, require_lineage=snapshot_mechanism == "incremental-cow",
                )
            except OSError as exc:
                raise RuntimeError("tiered pause completed without an accessible direct memory snapshot") from exc
        else:
            metadata = {}
        enriched = {**result, **metadata}
        enriched.setdefault("transferred_bytes", None)
        enriched["transferred_bytes_unavailable_reason"] = (
            "CubeSandbox pause API does not expose shim bytes written"
            if enriched["transferred_bytes"] is None else None
        )
        return enriched

    @staticmethod
    def relocate_snapshot(sandbox: Any, *, tier: str,
                          memory_snapshot_path: str, generation: int) -> Mapping[str, Any]:
        method = getattr(sandbox, "relocate_snapshot", None)
        if method is None:
            raise RuntimeError("installed CubeSandbox SDK lacks snapshot relocation")
        result = method(
            snapshot_tier=tier,
            memory_snapshot_path=memory_snapshot_path,
            snapshot_generation=generation,
        )
        if not isinstance(result, Mapping):
            raise RuntimeError("snapshot relocation did not return an identity")
        if (str(result.get("tier", "")).lower() != tier or
                int(result.get("generation", 0)) != generation or
                str(result.get("memory_snapshot_path", "")) != memory_snapshot_path):
            raise RuntimeError("snapshot relocation identity mismatch")
        return result

    def kill_sandbox(self, sandbox_or_id: Any) -> None:
        # Session finalization shares the same budget as bulk cleanup. Keep
        # this independent of snapshot transfer slots: pause itself needs a
        # native keep-tombstone destroy and must retain room to make progress.
        with self._delete_slots:
            self._kill_sandbox(sandbox_or_id)

    def _kill_sandbox(self, sandbox_or_id: Any) -> None:
        if not isinstance(sandbox_or_id, str):
            try:
                sandbox_or_id.kill()
            finally:
                close = getattr(sandbox_or_id, "close", None)
                if callable(close):
                    close()
                self._handles.pop(self.sandbox_id(sandbox_or_id), None)
            return
        sandbox_id = sandbox_or_id
        handle = self._handles.pop(sandbox_id, None)

        def kill_once() -> None:
            nonlocal handle
            if handle is None:
                info = next((
                    item for item in self._sandbox_class.list_v2(**self._sdk_kwargs())
                    if self._info_id(item) == sandbox_id
                ), None)
                if info is None:
                    return
                # Constructing an SDK handle from list data lets kill use the
                # official API without unnecessarily resuming a paused VM.
                handle = self._sandbox_class(info, self._config)
            attempt_handle = handle
            try:
                attempt_handle.kill()
            except Exception:
                # A lost delete response is successful if a fresh semantic
                # list proves the exact sandbox identity is already absent.
                still_present = any(
                    self._info_id(item) == sandbox_id
                    for item in self._sandbox_class.list_v2(**self._sdk_kwargs())
                )
                if not still_present:
                    return
                handle = None
                raise
            finally:
                # The retry path clears handle. Close the actual attempted
                # handle regardless, or each failed DELETE leaks its client.
                close = getattr(attempt_handle, "close", None)
                if callable(close):
                    close()

        retry_with_backoff(
            kill_once, label=f"kill sandbox {sandbox_id}", attempts=10,
            initial_delay_s=0.25, max_delay_s=2.0,
        )

    def get_sandbox_metrics(self, sandbox: Any) -> dict[str, Any] | None:
        getter = getattr(sandbox, "get_metrics", None)
        return None if getter is None else getter()

    def list_owned_sandboxes(self, owner_id: str) -> list[dict[str, Any]]:
        return [item for item in self._sandbox_class.list_v2(**self._sdk_kwargs())
                if (item.get("metadata") or {}).get("clawbox.task-uid") == owner_id]

    def kill_owned_sandboxes(self, owner_id: str) -> None:
        journal_ids = set(
            self.journal.sandbox_ids(task_uid=owner_id) if self.journal else []
        )
        # Resolve the journal against one current inventory.  Looking up every
        # historical ID separately makes interrupted high-density cleanup take
        # O(journal entries * full inventory) and can exceed the supervisor's
        # cleanup deadline even after most sandboxes are already gone.
        listed = self._sandbox_class.list_v2(**self._sdk_kwargs())
        ids: list[str] = []
        for item in listed:
            sandbox_id = self._info_id(item)
            metadata = item.get("metadata") or {}
            if sandbox_id in journal_ids or metadata.get("clawbox.task-uid") == owner_id:
                ids.append(sandbox_id)
                if sandbox_id not in self._handles:
                    self._handles[sandbox_id] = self._sandbox_class(item, self._config)
        owned_ids = list(dict.fromkeys(filter(None, ids)))
        errors: list[str] = []
        workers = min(self.cleanup_workers, len(owned_ids))
        if workers:
            # Cube destroy requests can each occupy the SDK lifecycle timeout.
            # Bound concurrency so dense-run cleanup is not serialized without
            # flooding the control plane with every owned VM at once.
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="cube-cleanup",
            ) as pool:
                futures = {
                    pool.submit(self.kill_sandbox, sandbox_id): sandbox_id
                    for sandbox_id in owned_ids
                }
                for future in as_completed(futures):
                    sandbox_id = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        # A single stale/broken handle must not prevent cleanup
                        # of the other sandboxes. Fail closed after a fresh list.
                        errors.append(
                            f"{sandbox_id}: {type(exc).__name__}: {exc}"
                        )
        remaining = self.list_owned_sandboxes(owner_id)
        if errors or remaining:
            details = []
            if errors:
                details.append("kill errors=" + repr(sorted(errors)))
            if remaining:
                details.append(
                    "remaining=" + repr([self._info_id(item) for item in remaining])
                )
            raise RuntimeError(
                f"CubeSandbox cleanup incomplete for task {owner_id}: "
                + "; ".join(details)
            )

    @staticmethod
    def _info_id(info: dict[str, Any]) -> str:
        return str(info.get("sandboxID") or info.get("sandbox_id") or "")
