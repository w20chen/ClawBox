"""Managed Dispatcher entry point (clawbox-managed-dispatcher)."""

from __future__ import annotations

import json
import logging
import os

from clawbox.api.dispatcher import Dispatcher, KubernetesCRBackend
from clawbox.api.templates import TemplateRegistry, default_registry
from clawbox.managed.db import managed_session_factory


def _load_registry() -> TemplateRegistry:
    raw = os.getenv("CLAWBOX_TEMPLATES")
    if raw:
        return TemplateRegistry.from_dict(json.loads(raw))
    return default_registry()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    dispatcher = Dispatcher(
        session_factory=managed_session_factory(),
        registry=_load_registry(),
        cr_backend=KubernetesCRBackend(
            namespace=os.getenv("CLAWBOX_CELL_NAMESPACE", "clawbox-benchmarks"),
            version=os.getenv("CLAWBOX_CR_VERSION", "v1alpha1"),
        ),
    )
    dispatcher.run_forever(interval_seconds=float(os.getenv("DISPATCHER_INTERVAL_SECONDS", "2")))


if __name__ == "__main__":
    main()
