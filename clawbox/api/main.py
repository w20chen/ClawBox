"""Managed API entry point (clawbox-managed-api).

Builds the app from the environment (service token + template registry) so a
deployed container honors CLAWBOX_SERVICE_TOKEN / CLAWBOX_TEMPLATES.
"""

from __future__ import annotations

import json
import os

import uvicorn

from clawbox.api.app import create_app
from clawbox.api.templates import TemplateRegistry, default_registry


def _load_registry() -> TemplateRegistry:
    raw = os.getenv("CLAWBOX_TEMPLATES")
    if raw:
        return TemplateRegistry.from_dict(json.loads(raw))
    return default_registry()


app = create_app(registry=_load_registry())


def main() -> None:
    uvicorn.run(
        app,
        host=os.getenv("MANAGED_API_HOST", "0.0.0.0"),
        port=int(os.getenv("MANAGED_API_PORT", "8085")),
    )


if __name__ == "__main__":
    main()
