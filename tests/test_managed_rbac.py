"""M1 RBAC separation contract tests (roadmap §6.5, ADR-006).

Locks least-privilege: the Managed API has no Kubernetes permissions; the
Dispatcher can only touch SandboxTask CRs (no Pods/Secrets/Jobs); the Cell
Controller keeps the Pod/Secret/Job verbs it needs to realize Cells.
"""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
MANAGED_RBAC = REPO / "deploy" / "managed-rbac.yaml"
CONTROL_RBAC = REPO / "deploy" / "control-plane-rbac.yaml"
CELL_DEPLOYMENT = REPO / "deploy" / "cell-controller.yaml"


def _load(path: Path) -> list[dict]:
    return list(yaml.safe_load_all(path.read_text(encoding="utf-8")))


def _rules_for(role_name: str, docs: list[dict]) -> list[dict]:
    role = next(
        (d for d in docs if d.get("kind") == "Role" and d.get("metadata", {}).get("name") == role_name),
        None,
    )
    assert role is not None, f"Role {role_name} not found"
    return role["rules"]


def _verbs_for(role_name: str, group: str, resource: str, docs: list[dict]) -> set[str]:
    rules = _rules_for(role_name, docs)
    out: set[str] = set()
    for rule in rules:
        if group in rule.get("apiGroups", []) and resource in rule.get("resources", []):
            out.update(rule["verbs"])
    return out


def test_managed_api_has_no_kubernetes_permissions():
    docs = _load(MANAGED_RBAC)
    rules = _rules_for("clawbox-managed-api-no-kubernetes-access", docs)
    assert rules == []
    # No other role in the managed file grants the API anything.
    bound = next(
        (d for d in docs if d.get("kind") == "RoleBinding" and d.get("metadata", {}).get("name")
         == "clawbox-managed-api-no-kubernetes-access"),
        None,
    )
    assert bound is not None
    assert bound["roleRef"]["name"] == "clawbox-managed-api-no-kubernetes-access"


def test_dispatcher_can_manage_sandboxtasks_but_not_pods_or_secrets():
    docs = _load(MANAGED_RBAC)
    role_name = "clawbox-managed-dispatcher"
    assert _verbs_for(role_name, "clawbox.openai.com", "sandboxtasks", docs) >= {
        "get", "list", "watch", "create", "patch",
    }
    assert _verbs_for(role_name, "", "pods", docs) == set()
    assert _verbs_for(role_name, "", "secrets", docs) == set()
    assert _verbs_for(role_name, "batch", "jobs", docs) == set()
    assert _verbs_for(role_name, "networking.k8s.io", "networkpolicies", docs) == set()


def test_cell_controller_keeps_cell_realization_verbs():
    docs = _load(CONTROL_RBAC)
    role_name = "clawbox-cell-controller"
    # The cell controller (not the API/dispatcher) is the only component with
    # pod/secret creation.
    assert _verbs_for(role_name, "", "pods", docs) >= {"create", "delete"}
    assert _verbs_for(role_name, "", "secrets", docs) >= {"create", "delete"}
    assert _verbs_for(role_name, "", "services", docs) >= {"create", "delete"}
    assert _verbs_for(role_name, "batch", "jobs", docs) >= {"create", "delete"}


def test_single_cell_controller_never_overlaps_without_leader_election():
    deployment = _load(CELL_DEPLOYMENT)[0]
    assert deployment["kind"] == "Deployment"
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}


def test_benchmark_launcher_is_direct_cr_path_being_retired():
    # The legacy launcher still has direct CR create for the dev/benchmark
    # path; production admission must come through the Dispatcher instead.
    docs = _load(CONTROL_RBAC)
    assert _verbs_for("clawbox-benchmark-launcher", "clawbox.openai.com", "sandboxtasks", docs) >= {
        "create", "delete",
    }
