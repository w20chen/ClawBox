from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fcntl")
spec = importlib.util.spec_from_file_location(
    "collector_refresh", Path(__file__).parents[1] / "scripts/refresh-swe-collector-images.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Missing(Exception):
    pass


class Templates:
    def __init__(self):
        self.items = {
            "old": SimpleNamespace(status="READY", image_info="old-image"),
            "new": SimpleNamespace(status="READY", image_info="new-image"),
        }
        self.deleted = []

    def get(self, key):
        if key not in self.items:
            raise Missing(key)
        return self.items[key]

    def delete(self, key):
        self.deleted.append(key)
        del self.items[key]


def record():
    return dict(status="template-ready", old_template_id="old", new_template_id="new",
                old_image="old-image", new_image="new-image")


def test_retirement_and_resume_keep_replacement():
    api, state = Templates(), record()
    module.retire_replaced_template(state, api, Missing)
    assert api.deleted == ["old"] and "new" in api.items
    assert state["old_template_retired_at"] > 0
    module.retire_replaced_template(state, api, Missing)
    module.retire_replaced_template(record(), api, Missing)
    assert api.deleted == ["old"]


@pytest.mark.parametrize("change", ["same-id", "unfinished", "not-ready", "new-image", "old-image"])
def test_retirement_rejects_unverified_replacement(change):
    api, state = Templates(), record()
    if change == "same-id":
        state["new_template_id"] = "old"
    elif change == "unfinished":
        state["status"] = "template-building"
    elif change == "not-ready":
        api.items["new"].status = "BUILDING"
    elif change == "new-image":
        api.items["new"].image_info = "unexpected-image"
    else:
        api.items["old"].image_info = "unexpected-image"
    with pytest.raises(RuntimeError):
        module.retire_replaced_template(state, api, Missing)
    assert api.deleted == []


def test_api_refusal_does_not_claim_retirement():
    api, state = Templates(), record()

    def refuse(key):
        raise RuntimeError("template is still in use")

    api.delete = refuse
    with pytest.raises(RuntimeError, match="still in use"):
        module.retire_replaced_template(state, api, Missing)
    assert "old_template_retired_at" not in state
    assert "old" in api.items and "new" in api.items
