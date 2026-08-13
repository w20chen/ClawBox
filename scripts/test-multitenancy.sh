#!/usr/bin/env bash
set -euo pipefail
exec python3 -m pytest -q tests/test_phase2_kb.py tests/test_phase3_chain.py
