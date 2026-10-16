# Verification, 2026-10-16

Source audit: [integration points and base commit](../pressure-controller-audit.md).
User configuration and policy design: [pressure control](../../docs/pressure-control.md).

Implemented: six selectable modes/presets; neutral preallocation per-VM cgroups;
guest execution start/end callbacks with endpoint validation; bounded CPU weights
from measured transient-release history; guarded memory.low; existing lifecycle
candidate ranking with LOCAL release measurement; rollback and cleanup. The
pre-existing endpoint retry change in worker.py was preserved.

## Executed checks

| Check | Result | Scope |
|---|---|---|
| Local Python selection | 199 passed, 2 skipped, 35.49 s | Policy, admission, state/events, bridge wrapper, driver, configuration, reporting, NUMA, legacy congestion |
| Final cooldown change | 11 passed, 0.46 s | Targeted pressure-controller regression tests after the larger selection |
| Kunpeng Python/kernel selection | 21 passed, 1 skipped, 5.31 s | Real cgroup defaults, writes/restoration, journal cleanup; Python policy/control tests |
| Kunpeng Toolbridge | `go test -count=1 ./...` passed, 15.032 s | Real local subprocess execution and start/end HTTP callbacks; not a VM run |
| Fresh-shell configure/describe/input validation | Passed | Existing recorded sqlglot trace; 6 planned arms, 2 repeats, unchanged `time_scale=1` |
| Launch, including repeat attempt | Exit 1 in preflight | No replay VM started; same five host/image checks failed |
| Cleanup inspection | `populated 0`, `frozen 0`; no `clawbox-test-*` children | After kernel tests and rejected launch |

Raw host outputs: [Linux tests](final-linux-tests.log),
[Go tests](final-go-tests.log), [input validation](final-input-validation.log),
[launch failure](final-launch.log). The local skips are platform-specific SSH
process tests; the host skip is a PSI-dependent legacy kernel test. Kunpeng's
live memory cgroup has no `memory.pressure` file. Consequently the new CPU/PSI
and memory-protection actions abstain on this kernel. Lifecycle coordination
does not depend on PSI.

The local Python selection was:

```text
python -m pytest -p no:cacheprovider -o addopts='' -q
  tests/test_pressure_control.py tests/test_policy_control.py
  tests/test_demand_admission.py tests/test_tool_completion_progress.py
  tests/test_policy_ssh.py tests/test_openclaw_driver.py
  tests/test_experiment_suites.py tests/test_experiments_v2.py
  tests/test_experiment_inputs.py tests/test_experiment_memory.py
  tests/test_reporting.py tests/test_numa_memory.py tests/test_numa_joint.py
  tests/test_congestion.py
```

Kunpeng validation used an isolated source copy at
`/home/weitianc/pressure-validation.hhAB2l`, the existing Python environment,
and a separate Go build cache. The shared repository had extensive existing
changes and was not overwritten. Kernel tests created their own bounded test
cgroups under the empty Cube subtree and removed them. No host reinstall,
template replacement, result deletion, volume pruning or memory-budget apply
was performed.

## Remaining validation and prerequisites

Real-VM end-to-end behavior and repeated throughput comparisons remain untested.
The attempted low-pressure comparison used the existing SWE-ReBench sqlglot
recording with recorded waits (24.416455745 seconds in this trace), original
Tool commands, c1, two repetitions, and no_control/phase_memcg/release_cpu.
Its saved 64-GiB LOCAL/CPU0–63 configuration does not match the current live
host. Preflight failed combined memory.max, LOCAL/shared NUMA nodes, compute
CPU set, and both current-ClawTune image checks. These checks were not bypassed.

Reproduction of the failed gate, after selecting the same source copy:

```sh
python -m clawbox.cli experiment configure \
  /home/weitianc/clawbox-c16-20261013/runtime.yaml low-pressure.yaml \
  --profile /home/weitianc/pressure-validation.hhAB2l/base-only.json \
  --experiment-id pressure-low --concurrency 1 --repetitions 2 \
  --baseline no_control --baseline phase_memcg --baseline release_cpu \
  --time-scale 1 --arm-timeout-seconds 600
python -m clawbox.cli experiment describe low-pressure.yaml
python -m clawbox.cli experiment validate low-pressure.yaml --inputs
sudo -n /home/weitianc/ClawBox/.venv/bin/python -m clawbox.cli \
  --output-root /home/weitianc/pressure-validation.hhAB2l/results \
  experiment launch low-pressure.yaml --run-id pressure-low-01
```

`base-only.json` is deliberately absent: this validation preserved the base
recipe's recorded identities instead of importing unrelated default profile
templates. Repeating configure without `--force` preserves the generated YAML.

To finish performance validation: reconcile and review the intended saved host
settings with live configuration; supply current workload-specific images with
progress-enabled Toolbridge; provide independently measured checkpoint/restore
costs and historical model-wait estimates for lifecycle arms. Evaluate CPU and
memory-control arms on a PSI-capable kernel. Run matched low-pressure,
capacity-saturation and combined-contention comparisons with repeated trials.
No Agent throughput, latency improvement or controller overhead conclusion can
be drawn from these unit/kernel checks. No performance regime was completed.

Clean-machine installation and the new Tool image build were not exercised.
The existing-environment tests are not evidence of fresh installation. Optional
memory.high throttling remains disabled because no safe avoidable-growth
measurement is available; no anonymous guest-capacity release is attributed to
memory.high, memory.reclaim or freezing.
