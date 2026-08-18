"""Managed API template registry (M1 acceptance: API cannot reference
arbitrary Secrets).

A client references a `template_ref` + `template_revision`; the Secret name,
task image and egress policy come from this registry, never from user input.
The registry is configured server-side (env/JSON), so a caller can only get
the images/secrets/limits a deployment explicitly publishes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TemplatePolicy:
    template_ref: str
    template_revision: int
    # Published immutable task image (digest-pinned), re-exposed in the CR.
    tool_image: str
    # Fixed Secret reference; callers can never name a Secret.
    secret_name: str
    runtime_image: str
    llm_egress_cidr: str
    profile: str = "small"
    max_deadline_seconds: int = 3600
    min_deadline_seconds: int = 60
    # Allowed reference types/egress buckets if any (empty = task-image only).
    allowed_input_prefixes: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)


class TemplateError(ValueError):
    """Unknown template/version or disallowed revision."""


class TemplateRegistry:
    def __init__(self, policies: dict[tuple[str, int], TemplatePolicy]):
        self._policies = dict(policies)

    @staticmethod
    def from_dict(data: dict) -> "TemplateRegistry":
        policies: dict[tuple[str, int], TemplatePolicy] = {}
        for ref, versions in data.items():
            for rev, cfg in versions.items():
                policies[(ref, int(rev))] = TemplatePolicy(
                    template_ref=ref,
                    template_revision=int(rev),
                    tool_image=cfg["toolImage"],
                    secret_name=cfg["secretName"],
                    runtime_image=cfg["runtimeImage"],
                    llm_egress_cidr=cfg.get("llmEgressCIDR", "0.0.0.0/0"),
                    profile=cfg.get("profile", "small"),
                    max_deadline_seconds=int(cfg.get("maxDeadlineSeconds", 3600)),
                    min_deadline_seconds=int(cfg.get("minDeadlineSeconds", 60)),
                    allowed_input_prefixes=tuple(cfg.get("allowedInputPrefixes", [])),
                )
        return TemplateRegistry(policies)

    def resolve(self, template_ref: str, template_revision: int) -> TemplatePolicy:
        policy = self._policies.get((template_ref, template_revision))
        if policy is None:
            known = sorted({f"{ref}@{rev}" for ref, rev in self._policies})
            raise TemplateError(
                f"unknown template {template_ref!r} revision {template_revision}; known: {known or 'none'}"
            )
        return policy

    def validate_deadline(self, policy: TemplatePolicy, deadline_seconds: int) -> None:
        if not (policy.min_deadline_seconds <= deadline_seconds <= policy.max_deadline_seconds):
            raise TemplateError(
                f"deadlineSeconds must be in [{policy.min_deadline_seconds}, "
                f"{policy.max_deadline_seconds}] for {policy.template_ref}@{policy.template_revision}"
            )


DEFAULT_TEMPLATE_JSON = json.dumps(
    {
        "swe-rebench-arm64": {
            "1": {
                "toolImage": "127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:0000000000000000000000000000000000000000000000000000000000000000",
                "secretName": "swe-rebench-secret",
                "runtimeImage": "127.0.0.1:5000/clawbox/runtime-arm64:dev",
                "llmEgressCIDR": "0.0.0.0/0",
                "profile": "small",
                "maxDeadlineSeconds": 3600,
                "minDeadlineSeconds": 60,
                "allowedInputPrefixes": [],
            }
        }
    }
)


def default_registry() -> TemplateRegistry:
    return TemplateRegistry.from_dict(json.loads(DEFAULT_TEMPLATE_JSON))
