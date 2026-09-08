#!/usr/bin/env bash
set -eu
validation_dir=$(mktemp -d /home/weitianc/clawbox-capacity-validation-20260907-XXXXXX)
git -C /home/weitianc/ClawBox-tiered-2906a91 archive HEAD | tar -xf - -C "$validation_dir"
mkdir -p "$validation_dir/evidence"
cp /home/weitianc/test_snapshot_pool.py "$validation_dir/tests/test_snapshot_pool.py"
cd "$validation_dir"
set +e
PYTHONPATH=. /home/weitianc/ClawBox-tiered-2906a91/.venv/bin/python -m pytest tests/test_snapshot_pool.py -q > evidence/before.log 2>&1
before_status=$?
set -e
cp /home/weitianc/snapshot_pool.py "$validation_dir/clawbox/experiments/snapshot_pool.py"
PYTHONPATH=. /home/weitianc/ClawBox-tiered-2906a91/.venv/bin/python -m pytest tests/test_snapshot_pool.py tests/test_policy_v2.py -q > evidence/after.log 2>&1
printf 'validation_dir=%s\nbefore_exit=%s\nafter_exit=0\n' "$validation_dir" "$before_status"
tail -4 evidence/before.log
cat evidence/after.log
test "$before_status" = 1
