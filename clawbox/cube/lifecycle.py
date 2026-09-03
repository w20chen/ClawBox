from __future__ import annotations

import time

from clawbox.replay.lifecycle import LifecycleError

from .client import CubeSandboxClient, Ownership


class CubeSandboxLifecycle:
    def __init__(self, client: CubeSandboxClient, *, template: str,
                 node_name: str, ownership: Ownership,
                 allow_internet_access: bool = True) -> None:
        self.client = client
        self.template = template
        self.node_name = node_name
        self.ownership = ownership
        self.allow_internet_access = allow_internet_access
        self.sandbox = None
        self.sandbox_id: str | None = None
        self._resident = False

    @property
    def resident(self) -> bool:
        return self._resident

    def start(self) -> float:
        if self.sandbox_id is not None:
            raise LifecycleError("CubeSandbox lifecycle already started")
        started = time.monotonic()
        self.sandbox = self.client.create_sandbox(
            template=self.template,
            node_name=self.node_name,
            ownership=self.ownership,
            allow_internet_access=self.allow_internet_access,
        )
        self.sandbox_id = self.client.sandbox_id(self.sandbox)
        self._resident = True
        return time.monotonic() - started

    def checkpoint_and_evict(self) -> float:
        if self.sandbox is None or not self._resident:
            raise LifecycleError("CubeSandbox is not resident")
        started = time.monotonic()
        self.client.pause_sandbox(self.sandbox)
        self._resident = False
        return time.monotonic() - started

    pause_and_evict = checkpoint_and_evict

    def restore(self) -> float:
        if self.sandbox_id is None or self._resident:
            raise LifecycleError("CubeSandbox is not paused")
        started = time.monotonic()
        self.sandbox = self.client.connect_sandbox(self.sandbox_id)
        self._resident = True
        return time.monotonic() - started

    def close(self) -> None:
        if self.sandbox_id is not None:
            self.client.kill_sandbox(self.sandbox_id)
        self.sandbox = None
        self.sandbox_id = None
        self._resident = False
