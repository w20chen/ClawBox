"""Reuse ClawTune's existing model configuration and credential resolver."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def resolve_llm_configuration(configuration: dict, *, live: bool) -> tuple[dict, str]:
    resolved = dict(configuration)
    credential = ""
    if source := configuration.get("clawtune_config"):
        path = Path(source).expanduser().resolve()
        root = path.parent.parent
        sys.path.insert(0, str(root))
        try:
            from swe_rebench.config import RunnerConfig
            llm = RunnerConfig.from_yaml(path, repo_root=root).llm
        finally:
            sys.path.remove(str(root))
        resolved["base_url"] = llm.upstream_base_url
        resolved["model"] = llm.model
        credential = llm.api_key
    else:
        credential = os.environ.get(configuration.get("api_key_env", "OPENCLAW_API_KEY"), "")
    if live and not credential:
        raise ValueError("No LLM credential found in the selected ClawTune configuration or API key environment")
    return resolved, credential
