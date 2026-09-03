"""Managed Dispatcher entry point (clawbox-managed-dispatcher)."""

from __future__ import annotations

import logging
import os

from clawbox.api.dispatcher import Dispatcher, KubernetesCRBackend
from clawbox.managed.db import managed_session_factory


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    dispatcher = Dispatcher(
        session_factory=managed_session_factory(),
        cr_backend=KubernetesCRBackend(
            namespace=os.getenv("CLAWBOX_CELL_NAMESPACE", "clawbox-benchmarks"),
            version="v1alpha2",
        ),
    )
    dispatcher.run_forever(interval_seconds=float(os.getenv("DISPATCHER_INTERVAL_SECONDS", "2")))


if __name__ == "__main__":
    main()
