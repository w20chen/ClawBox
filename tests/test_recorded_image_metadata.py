import pytest

from clawbox.images.recorded import installed_metadata_path
from clawbox.images.swerebench import _registry_session


@pytest.mark.parametrize("path,expected", [
    ("opt/conda/envs/testbed/lib/python3.9/site-packages/pytest-8.3.5.dist-info/METADATA", True),
    ("opt/conda/envs/testbed/lib/python3.9/site-packages/astor.egg-info/PKG-INFO", True),
    ("opt/conda/envs/testbed/lib/python3.9/site-packages/setuptools/_vendor/packaging-24.2.dist-info/METADATA", False),
    ("opt/conda/lib/python3.12/site-packages/pip-25.0.dist-info/METADATA", False),
])
def test_only_installed_task_distributions_are_inventory_entries(path, expected):
    assert installed_metadata_path(path) is expected


def test_explicit_registry_proxy_overrides_ambient_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "socks5://unrelated-proxy:1080")
    with _registry_session("http://127.0.0.1:17897") as session:
        settings = session.merge_environment_settings("https://registry-1.docker.io/v2/", {}, False, None, None)
        assert settings["proxies"]["https"] == "http://127.0.0.1:17897"
