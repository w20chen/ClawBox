# Use an installed machine

This guide assumes standalone CubeSandbox is installed and the Runtime and Tool templates have passed the checks in the [installation guide](cubesandbox-setup.md).

## 1. Open the correct checkout

Use a ClawBox checkout on the Linux experiment host. On kunpeng, check [current environment](current-environment.md) first: the active experiment uses its own frozen checkout. Updating another checkout does not update the running experiment.

The wrapper reads `~/.config/clawbox/machine.env`. This file contains addresses, template IDs, image references, ClawTune location, and output paths. Start with `examples/clawbox-machine.env.example`. Never copy another machine's template IDs.

## 2. Create a local experiment file

```bash
bash scripts/clawbox study init \
  --trace /data/clawbox-traces/rec-a-healthy-reference.jsonl \
  --output /data/clawbox-specs/rec-a.yaml
```

The command refuses to overwrite a file. Its default example is rec-a: 40 agents, 13 policies, 2 GiB Runtime VMs, 4 GiB Tool VMs, 160 GiB LOCAL, and 64 GiB WARM for the two tiered policies. All policies use the same LOCAL budget.

Review the YAML. Template sizes must match its VM sizes. For another benchmark, provide a suitable `--base` file and check the repository, base revision, prompt, validation command, trace, and prediction files. Changing only a filename does not change the benchmark correctly.

Keep the original recording separate from any approved request reference. The supported input contains action-format `llm_call` records with requests, responses, and latency. A matching file format alone does not guarantee that commands and outputs match the selected Tool image.

## 3. Check prerequisites

```bash
bash scripts/clawbox study check --spec /data/clawbox-specs/rec-a.yaml
```

Success confirms the YAML, required files, and LOCAL/WARM settings. It does not run a VM or certify telemetry.

After reboot, restore the temporary memory settings while no experiment VMs remain:

```bash
bash scripts/clawbox study setup-memory --spec /data/clawbox-specs/rec-a.yaml
bash scripts/clawbox study check --spec /data/clawbox-specs/rec-a.yaml
```

Setup uses sudo and refuses a populated VM pool. Do not run it during an experiment. Do not restart Cubelet merely to start another study.

## 4. Start a run

Short correctness check:

```bash
bash scripts/clawbox study start \
  --spec /data/clawbox-specs/rec-a.yaml --name check-01 --steps 5
```

Full recording:

```bash
bash scripts/clawbox study start \
  --spec /data/clawbox-specs/rec-a.yaml --name full-01 --full-trace
```

The command prints a process ID, result directory, log path, and status command. It starts a detached process: SSH can be closed without stopping it. Do not add another `nohup`. Use a new name for each attempt. Do not run two studies on the same VM pool.

The default safety deadline is 20 minutes per baseline for a prefix and 60 minutes for a full trace. Override it with `--arm-seconds`. These are failure limits, not expected durations. The runner stops a timed-out worker, cleans up its owned VMs, records the incomplete result, and proceeds to the next policy.

Five rounds preserve five original responses and add one labeled stop response. They do not include the later recorded edits and pytest phase. Separate post-run pytest validation remains enabled.

## 5. Read progress and results

```bash
bash scripts/clawbox study status /data/clawbox-results/check-01
bash scripts/clawbox study report /data/clawbox-results/check-01
tail -n 20 /data/clawbox-results/check-01.log
```

Use the exact output paths printed at launch. Status separates completed baseline results from partial sessions. A long gap in non-memory events needs investigation even if memory samples continue.

A passing result requires all 40 sessions, successful validation, matching replay requests, exact execution-ID joins, and no unexplained telemetry loss. See [results](results-guide.md).

## 6. Resume work after interruption

If only SSH or the assistant disconnected, inspect the existing run; do not start a duplicate.

If the worker died or the host rebooted, preserve its directory. Inspect owned VMs with `scripts/audit-cube-sandboxes.py --json`, finish cleanup for that run, restore memory settings when needed, and launch a new named attempt. There is no automatic resume from the middle of an agent's trace. Never combine partial attempts into a claimed 40/40 success.
