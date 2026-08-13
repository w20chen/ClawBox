$ErrorActionPreference = 'Stop'
python -m pytest -q tests/test_phase2_kb.py tests/test_phase3_chain.py
exit $LASTEXITCODE
