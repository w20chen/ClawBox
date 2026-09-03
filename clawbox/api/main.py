"""Managed API entry point (clawbox-managed-api)."""

from __future__ import annotations

import os

import uvicorn

from clawbox.api.app import create_app
app = create_app()


def main() -> None:
    uvicorn.run(
        app,
        host=os.getenv("MANAGED_API_HOST", "0.0.0.0"),
        port=int(os.getenv("MANAGED_API_PORT", "8085")),
    )


if __name__ == "__main__":
    main()
