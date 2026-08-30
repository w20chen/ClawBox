from __future__ import annotations

import ipaddress
import os
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable

from clawbox.cell.capacity import (
    AtomicAdmission,
    CellSize,
    ClawTunePredictionSizer,
    FixedProfileSizer,
    KubernetesNodeCapacityProvider,
    PlacementPolicy,
    ResourceVector,
    SingleNodePlacementPolicy,
)
from clawbox.cell.p90 import (
    FIXED_BASELINE,
    HTTPAdmissionPredictionClient,
    PredictionUnavailable,
    SUPPORTED_CELL_BASELINES,
    resolve_p90_decision,
)
from clawbox.common.config import settings
from clawbox.cell.manifests import (
    credential_secrets,
    generate_ssh_credentials,
    network_policies,
    prompt_configmap,
    runtime_job,
    tool_pod,
    tool_service,
)


GROUP = "clawbox.openai.com"
VERSION = "v1alpha1"
PLURAL = "sandboxtasks"
FINALIZER = "clawbox.openai.com/cell-cleanup"


class CellPhase(StrEnum):
    QUEUED = "Queued"
    ADMITTED = "Admitted"
    TOOL_STARTING = "ToolStarting"
    TOOL_READY = "ToolReady"
    RUNTIME_RUNNING = "RuntimeRunning"
    COLLECTING = "Collecting"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    TIMED_OUT = "TimedOut"
    CANCELLED = "Cancelled"
    CLEANED = "Cleaned"


# Result phases still need one cleanup pass. CLEANED is deliberately separate:
# it is the stable lifecycle endpoint and must never be driven back through a
# desired-state transition. Future suspend/resume phases should likewise be
# classified explicitly instead of being inferred from "not terminal".
OUTCOME_TERMINAL = {
    CellPhase.SUCCEEDED,
    CellPhase.FAILED,
    CellPhase.TIMED_OUT,
    CellPhase.CANCELLED,
}
FINAL = {CellPhase.CLEANED}
TERMINAL = OUTCOME_TERMINAL | FINAL

# Capacity ownership is an explicit lifecycle contract. A future Suspended
# phase can be added here if it retains its Cell reservation, or omitted after
# a checkpoint has released the underlying VMs.
CAPACITY_HELD = {
    CellPhase.ADMITTED, CellPhase.TOOL_STARTING, CellPhase.TOOL_READY,
    CellPhase.RUNTIME_RUNNING, CellPhase.COLLECTING,
}


def capacity_reservation_for_phase(
    phase: CellPhase, reservation: ResourceVector,
) -> ResourceVector | None:
    """Return the physical reservation charged by a lifecycle phase.

    Suspend support can return a reduced vector here after checkpointing
    releases CPU/memory while retaining storage.  Admission remains unaware of
    the suspend mechanism itself.
    """
    return reservation if phase in CAPACITY_HELD else None


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def resource_from_status(value: dict[str, Any]) -> ResourceVector:
    return ResourceVector(
        int(value["cpuMillis"]), int(value["memoryBytes"]),
        int(value["storageBytes"]), int(value["pods"]),
    )


def validate_task(task: dict[str, Any]) -> None:
    spec = task.get("spec") or {}
    if len(str(task.get("metadata", {}).get("name", ""))) > 48:
        raise ValueError("SandboxTask name must be at most 48 characters so owned child names remain valid")
    image = str(spec.get("toolImage", ""))
    if not re.fullmatch(r".+@sha256:[a-f0-9]{64}", image):
        raise ValueError("spec.toolImage must be an immutable linux/arm64 digest")
    if spec.get("profile", "small") not in FixedProfileSizer.PROFILES:
        raise ValueError("spec.profile must be small, medium, or large")
    baseline = str(spec.get("baseline", FIXED_BASELINE))
    if baseline not in SUPPORTED_CELL_BASELINES:
        raise ValueError(
            "spec.baseline must be fixed-resident, p90-static, or p90-elastic"
        )
    generation = spec.get("kbGeneration")
    if baseline == "p90-static":
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ValueError("p90-static requires a positive integer spec.kbGeneration")
    elif generation is not None:
        raise ValueError("spec.kbGeneration is only valid with p90-static")
    if not spec.get("problemStatement"):
        raise ValueError("spec.problemStatement is required")
    if not spec.get("llmSecretName"):
        raise ValueError("spec.llmSecretName is required")
    if not spec.get("llmEgressCIDR"):
        raise ValueError("spec.llmEgressCIDR is required for fail-closed network policy")
    try:
        llm_cidrs = spec.get("llmEgressCIDRs") or [spec["llmEgressCIDR"]]
        if not isinstance(llm_cidrs, list) or not llm_cidrs or len(llm_cidrs) > 32:
            raise ValueError("spec.llmEgressCIDRs must contain between 1 and 32 entries")
        if str(spec["llmEgressCIDR"]) != str(llm_cidrs[0]):
            raise ValueError("spec.llmEgressCIDR must equal the first spec.llmEgressCIDRs entry")
        for cidr in llm_cidrs:
            ipaddress.ip_network(str(cidr), strict=True)
        for cidr in spec.get("toolEgressCIDRs", []):
            ipaddress.ip_network(str(cidr), strict=True)
    except ValueError as exc:
        raise ValueError(f"egress CIDRs must be canonical networks: {exc}") from exc


class CellReconciler:
    def __init__(
        self, *, core_api, batch_api, networking_api, custom_api,
        sizer: FixedProfileSizer | None = None,
        prediction_sizer: ClawTunePredictionSizer | None = None,
        prediction_provider=None,
        p90_min_evidence: int | None = None,
        capacity_provider: KubernetesNodeCapacityProvider | None = None,
        placement_policy: PlacementPolicy | None = None,
    ):
        self.core = core_api
        self.batch = batch_api
        self.networking = networking_api
        self.custom = custom_api
        self.sizer = sizer or FixedProfileSizer()
        self.prediction_sizer = prediction_sizer or ClawTunePredictionSizer(self.sizer)
        self.prediction_provider = prediction_provider
        if self.prediction_provider is None and settings.kb_endpoint and settings.kb_token:
            self.prediction_provider = HTTPAdmissionPredictionClient(
                settings.kb_endpoint, settings.kb_token,
            )
        self.p90_min_evidence = (
            int(os.getenv("CLAWBOX_P90_MIN_EVIDENCE", "5"))
            if p90_min_evidence is None else p90_min_evidence
        )
        if self.p90_min_evidence < 1:
            raise ValueError("p90_min_evidence must be positive")
        self.capacity_provider = capacity_provider or KubernetesNodeCapacityProvider(
            core_api, devmapper_available_bytes=int(os.getenv("CLAWBOX_DEVMAPPER_AVAILABLE_BYTES", "0")),
        )
        self.placement = placement_policy or SingleNodePlacementPolicy()

    @staticmethod
    def _not_found(exc: Exception) -> bool:
        return getattr(exc, "status", None) == 404

    def _patch_status(self, task: dict[str, Any], phase: CellPhase, **values: Any) -> None:
        current = dict(task.get("status") or {})
        current.update(values)
        current.update({
            "phase": phase.value,
            "observedGeneration": task["metadata"].get("generation", 1),
            "lastTransitionTime": now(),
        })
        self.custom.patch_namespaced_custom_object_status(
            GROUP, VERSION, task["metadata"]["namespace"], PLURAL,
            task["metadata"]["name"], {"status": current},
        )

    def _patch_metadata(self, task: dict[str, Any], finalizers: list[str]) -> None:
        self.custom.patch_namespaced_custom_object(
            GROUP, VERSION, task["metadata"]["namespace"], PLURAL,
            task["metadata"]["name"], {"metadata": {"finalizers": finalizers}},
        )

    def _get(self, reader: Callable[..., Any], name: str, namespace: str) -> Any | None:
        try:
            return reader(name, namespace)
        except Exception as exc:
            if self._not_found(exc):
                return None
            raise

    @staticmethod
    def _assert_owner(existing: Any, expected_uid: str, name: str) -> None:
        metadata_value = existing.get("metadata", {}) if isinstance(existing, dict) else getattr(existing, "metadata", None)
        owners = (
            metadata_value.get("ownerReferences", [])
            if isinstance(metadata_value, dict)
            else (getattr(metadata_value, "owner_references", None) or [])
        )
        for owner in owners:
            uid = owner.get("uid") if isinstance(owner, dict) else getattr(owner, "uid", None)
            controller = owner.get("controller") if isinstance(owner, dict) else getattr(owner, "controller", None)
            if uid == expected_uid and controller is True:
                return
        raise RuntimeError(f"refusing to adopt pre-existing child without the SandboxTask owner: {name}")

    def _ensure(self, reader: Callable[..., Any], creator: Callable[..., Any], manifest: dict[str, Any]) -> Any:
        name = manifest["metadata"]["name"]
        namespace = manifest["metadata"]["namespace"]
        existing = self._get(reader, name, namespace)
        if existing is not None:
            expected_uid = manifest["metadata"]["ownerReferences"][0]["uid"]
            self._assert_owner(existing, expected_uid, name)
            return existing
        try:
            return creator(namespace, manifest)
        except Exception as exc:
            if getattr(exc, "status", None) == 409:
                existing = reader(name, namespace)
                expected_uid = manifest["metadata"]["ownerReferences"][0]["uid"]
                self._assert_owner(existing, expected_uid, name)
                return existing
            raise

    def _reservation_set(self, namespace: str, exclude: str) -> AtomicAdmission:
        admission = AtomicAdmission(self.capacity_provider.capacity())
        response = self.custom.list_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL)
        for item in response.get("items", []):
            if item.get("metadata", {}).get("name") == exclude:
                continue
            status = item.get("status") or {}
            try:
                phase = CellPhase(status.get("phase", "Queued"))
                reservation = resource_from_status(status["reservation"])
            except (KeyError, TypeError, ValueError):
                continue
            held = capacity_reservation_for_phase(phase, reservation)
            if held is not None:
                admission.reserve(item["metadata"]["name"], held)
        return admission

    def _size_for_phase(
        self, task: dict[str, Any], phase: CellPhase, status: dict[str, Any],
    ) -> tuple[CellSize, dict[str, Any]] | None:
        """Resolve once while queued, then replay the persisted exact decision."""
        baseline = str(task.get("spec", {}).get("baseline", FIXED_BASELINE))
        persisted = status.get("sizingDecision")
        if isinstance(persisted, dict):
            if persisted.get("baseline") != baseline:
                raise ValueError("persisted sizing baseline does not match immutable task spec")
            raw_size = persisted.get("cellSize")
            if not isinstance(raw_size, dict):
                raise ValueError("persisted sizing decision has no Cell size")
            return CellSize.from_status(raw_size), persisted
        if baseline == FIXED_BASELINE:
            size = self.sizer.size(task["spec"].get("profile", "small"))
            return size, {
                "baseline": FIXED_BASELINE,
                "source": "fixed-profile",
                "cellSize": size.as_status(),
            }
        if phase is not CellPhase.QUEUED:
            raise ValueError("p90 Cell reached a materialized phase without a frozen sizing decision")
        if self.prediction_provider is None:
            raise PredictionUnavailable("ClawTune prediction service is not configured")
        decision = resolve_p90_decision(
            task,
            provider=self.prediction_provider,
            sizer=self.prediction_sizer,
            min_evidence=self.p90_min_evidence,
        )
        return decision.cell_size, decision.as_status()

    def _ensure_secrets(self, task: dict[str, Any], timeout: int) -> None:
        namespace = task["metadata"]["namespace"]
        name = task["metadata"]["name"]
        secret_name = f"{name}-auth"
        existing = self._get(self.core.read_namespaced_secret, secret_name, namespace)
        if existing is not None:
            self._assert_owner(existing, task["metadata"]["uid"], secret_name)
            return
        credentials = generate_ssh_credentials(name)
        for secret in credential_secrets(task, credentials, timeout):
            self._ensure(self.core.read_namespaced_secret, self.core.create_namespaced_secret, secret)

    def _ensure_prerequisites(self, task: dict[str, Any], timeout: int) -> None:
        self._ensure_secrets(task, timeout)
        self._ensure(
            self.core.read_namespaced_config_map, self.core.create_namespaced_config_map,
            prompt_configmap(task),
        )
        self._ensure(
            self.core.read_namespaced_service, self.core.create_namespaced_service,
            tool_service(task),
        )
        for policy in network_policies(task):
            self._ensure(
                self.networking.read_namespaced_network_policy,
                self.networking.create_namespaced_network_policy,
                policy,
            )

    @staticmethod
    def _pod_state(pod: Any | None) -> str:
        if pod is None:
            return "Missing"
        status = getattr(pod, "status", None)
        phase = getattr(status, "phase", "")
        statuses = getattr(status, "container_statuses", None) or []
        if phase == "Running" and statuses and all(bool(getattr(item, "ready", False)) for item in statuses):
            return "Ready"
        return phase or "Unknown"

    @staticmethod
    def _job_state(job: Any | None) -> str:
        if job is None:
            return "Missing"
        status = getattr(job, "status", None)
        if getattr(status, "succeeded", 0):
            return "Succeeded"
        if getattr(status, "failed", 0):
            return "Failed"
        if getattr(status, "active", 0):
            return "Running"
        return "Pending"

    def _timed_out(self, task: dict[str, Any]) -> bool:
        # Cell deadline = agent budget + pipeline margin. Keep in sync with
        # runtime_job()'s activeDeadlineSeconds (clawbox/cell/manifests.py):
        # 600s headroom for patch collection (two bounded ssh) + final upload.
        timeout = int(task["spec"].get("timeoutSeconds", 1800)) + 600
        started = (task.get("status") or {}).get("admittedAt")
        if not started:
            return False
        parsed = datetime.fromisoformat(started.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - parsed).total_seconds() > timeout

    def _delete(self, call: Callable[..., Any], name: str, namespace: str, **kwargs: Any) -> None:
        try:
            call(name, namespace, **kwargs)
        except Exception as exc:
            if not self._not_found(exc):
                raise

    def _cleanup(self, task: dict[str, Any]) -> bool:
        namespace = task["metadata"]["namespace"]
        name = task["metadata"]["name"]
        self._delete(self.batch.delete_namespaced_job, f"{name}-runtime", namespace,
                     propagation_policy="Foreground")
        self._delete(self.core.delete_namespaced_pod, f"{name}-tool", namespace,
                     grace_period_seconds=0)
        # Foreground Job deletion is asynchronous.  Keep auth/config/network
        # objects until both workload Pods are actually gone so a terminating
        # sidecar can still complete its final upload.
        workloads_remaining = (
            self._get(self.batch.read_namespaced_job, f"{name}-runtime", namespace)
            or self._get(self.core.read_namespaced_pod, f"{name}-tool", namespace)
        )
        if workloads_remaining is not None:
            return False
        self._delete(self.core.delete_namespaced_service, f"{name}-tool", namespace)
        self._delete(self.core.delete_namespaced_config_map, f"{name}-prompt", namespace)
        self._delete(self.core.delete_namespaced_secret, f"{name}-auth", namespace)
        for suffix in ("default-deny", "tool-ingress", "runtime-egress", "tool-egress"):
            self._delete(self.networking.delete_namespaced_network_policy, f"{name}-{suffix}", namespace)
        remaining = (
            self._get(self.core.read_namespaced_secret, f"{name}-auth", namespace)
            or self._get(self.core.read_namespaced_service, f"{name}-tool", namespace)
        )
        return remaining is None

    def reconcile(self, task: dict[str, Any], *, allow_workload_start: bool = True) -> bool:
        """Reconcile one Cell and report whether a Pod or Job start was issued.

        ``allow_workload_start`` is the materialization boundary. It rate-limits
        both today's initial VM creation and a future resume path without
        delaying status, cancellation, timeout, or cleanup work.
        """
        metadata_value = task["metadata"]
        finalizers = list(metadata_value.get("finalizers") or [])
        if metadata_value.get("deletionTimestamp"):
            if self._cleanup(task) and FINALIZER in finalizers:
                self._patch_metadata(task, [item for item in finalizers if item != FINALIZER])
            return False
        if FINALIZER not in finalizers:
            self._patch_metadata(task, finalizers + [FINALIZER])
            return False

        status = task.get("status") or {}
        try:
            phase = CellPhase(status.get("phase", CellPhase.QUEUED.value))
        except ValueError:
            self._patch_status(task, CellPhase.FAILED, outcome="Failed", reason="InvalidPhase")
            return False

        if task.get("spec", {}).get("desiredState") == "Cancelled" and phase not in TERMINAL:
            self._patch_status(
                task,
                CellPhase.CANCELLED,
                outcome="Cancelled",
                reason="CancellationRequested",
            )
            return False

        if not status.get("phase"):
            try:
                validate_task(task)
                self.sizer.size(task["spec"].get("profile", "small"))
            except ValueError as exc:
                self._patch_status(task, CellPhase.FAILED, outcome="Failed", reason="InvalidSpec", message=str(exc))
                return False
            self._patch_status(task, CellPhase.QUEUED, queuedAt=now(), reason="PendingAdmission")
            return False

        if phase in CAPACITY_HELD and self._timed_out(task):
            self._patch_status(task, CellPhase.TIMED_OUT, outcome="TimedOut", reason="CellDeadlineExceeded")
            return False

        namespace = metadata_value["namespace"]
        name = metadata_value["name"]
        timeout = int(task["spec"].get("timeoutSeconds", 1800))

        try:
            resolved_sizing = self._size_for_phase(task, phase, status)
        except PredictionUnavailable as exc:
            self._patch_status(
                task, CellPhase.QUEUED, reason="PredictionUnavailable", message=str(exc),
            )
            return False
        except (KeyError, TypeError, ValueError) as exc:
            self._patch_status(
                task, CellPhase.FAILED, outcome="Failed", reason="UnsafeSizingDecision",
                message=str(exc),
            )
            return False
        if resolved_sizing is None:  # pragma: no cover - defensive type boundary
            return False
        size, sizing_decision = resolved_sizing
        if status.get("reservation"):
            try:
                persisted_reservation = resource_from_status(status["reservation"])
            except (KeyError, TypeError, ValueError) as exc:
                self._patch_status(
                    task, CellPhase.FAILED, outcome="Failed", reason="UnsafeSizingDecision",
                    message=f"invalid persisted reservation: {exc}",
                )
                return False
            if persisted_reservation != size.reservation:
                self._patch_status(
                    task, CellPhase.FAILED, outcome="Failed", reason="UnsafeSizingDecision",
                    message="persisted sizing decision does not match the Cell reservation",
                )
                return False

        if phase == CellPhase.QUEUED:
            admission = self._reservation_set(namespace, name)
            if not admission.reserve(name, size.reservation):
                self._patch_status(task, CellPhase.QUEUED, reason="InsufficientCellCapacity",
                                   message="the complete two-VM Cell budget is unavailable")
                return False
            node_name = self.placement.select_node(size)
            self._patch_status(
                task, CellPhase.ADMITTED, reason="CellBudgetReserved", admittedAt=now(),
                reservation=size.reservation.as_status(), nodeName=node_name or "",
                sizingDecision=sizing_decision,
            )
            return False

        node_name = status.get("nodeName") or None
        if phase == CellPhase.ADMITTED:
            if not allow_workload_start:
                return False
            self._ensure_prerequisites(task, timeout)
            self._ensure(
                self.core.read_namespaced_pod, self.core.create_namespaced_pod,
                tool_pod(task, size, node_name=node_name),
            )
            self._patch_status(task, CellPhase.TOOL_STARTING, reason="ToolPodCreated")
            return True

        tool = self._get(self.core.read_namespaced_pod, f"{name}-tool", namespace)
        tool_state = self._pod_state(tool)
        if phase == CellPhase.TOOL_STARTING:
            if tool_state in {"Failed", "Succeeded", "Missing"}:
                self._patch_status(task, CellPhase.FAILED, outcome="Failed", reason=f"Tool{tool_state}")
            elif tool_state == "Ready":
                self._patch_status(task, CellPhase.TOOL_READY, reason="ToolBridgeReady", toolReadyAt=now())
            return False

        if phase == CellPhase.TOOL_READY:
            if tool_state != "Ready":
                self._patch_status(task, CellPhase.FAILED, outcome="Failed", reason=f"Tool{tool_state}")
                return False
            if not allow_workload_start:
                return False
            self._ensure(
                self.batch.read_namespaced_job, self.batch.create_namespaced_job,
                runtime_job(task, size, node_name=node_name),
            )
            self._patch_status(task, CellPhase.RUNTIME_RUNNING, reason="RuntimeJobCreated", runtimeStartedAt=now())
            return True

        job = self._get(self.batch.read_namespaced_job, f"{name}-runtime", namespace)
        job_state = self._job_state(job)
        if phase == CellPhase.RUNTIME_RUNNING:
            if tool_state != "Ready":
                self._patch_status(task, CellPhase.FAILED, outcome="Failed", reason=f"Tool{tool_state}")
            elif job_state == "Succeeded":
                self._patch_status(task, CellPhase.COLLECTING, reason="UploadsConfirmedByRuntimeExit")
            elif job_state in {"Failed", "Missing"}:
                self._patch_status(task, CellPhase.FAILED, outcome="Failed", reason=f"Runtime{job_state}")
            return False

        if phase == CellPhase.COLLECTING:
            # runtime-entrypoint exits zero only after the ingester receipt says
            # result + final trace marker are durable.
            if job_state == "Succeeded":
                self._patch_status(task, CellPhase.SUCCEEDED, outcome="Succeeded", reason="ArtifactsDurable")
            else:
                self._patch_status(task, CellPhase.FAILED, outcome="Failed", reason="ReceiptLost")
            return False

        if phase in OUTCOME_TERMINAL:
            if self._cleanup(task):
                # Cleaning is a lifecycle transition, not a new outcome. Keep
                # the terminal reason (for example RuntimeFailed or
                # CellDeadlineExceeded) so observers and a future resume
                # coordinator do not lose the cause when children disappear.
                self._patch_status(task, CellPhase.CLEANED, cleanedAt=now())
            return False

        return False
