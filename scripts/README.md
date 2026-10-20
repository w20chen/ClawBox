# Scripts

The supported interface is `clawbox experiment`. Start with the
[experiment guide](../docs/guide.md) for operation and options, and the
[installation guide](../docs/installation.md) for provisioning.

`bash scripts/clawbox` loads `~/.config/clawbox/machine.env`, selects the project's
Python environment, and forwards arguments to the CLI. Wrapper flags
`--machine-env FILE` and `--sudo` precede the normal CLI arguments. Use this entry
point in new terminals to avoid repeating environment and privilege setup.

| Task | Entry point |
| --- | --- |
| Create or update Python, the patched SDK and matching ClawTune; check their integration | [bootstrap-python.sh](bootstrap-python.sh) |
| Export guest images, kernel and matching source for a new host | [export-machine-assets.sh](export-machine-assets.sh), with `--with-clawtune` |
| Build the patched CubeSandbox release using the exported kernel | [build-cubesandbox-release.sh](build-cubesandbox-release.sh) |
| Register a Runtime/Tool pair and save IDs after both become READY | [register-machine-templates.sh](register-machine-templates.sh) |
| Inspect/configure the host or preview/apply crash-dump cleanup | [clawbox](clawbox), `experiment host` |
| Configure, validate, launch, train, report or recover an experiment | [clawbox](clawbox), `experiment` |
| Reproduce the frozen full-pool versus one-cache-cluster CPU-placement experiment | [run-llc-shared-reproduction.sh](run-llc-shared-reproduction.sh) |

The installation helpers support `--help`. Complete command sequences are in
the installation guide. Keep generated bundles, registration records and logs
outside this directory; store machine settings under `~/.config/clawbox/`.

Local verification, result, and collected-trace directories are ignored by Git;
checked-in examples under `examples/` remain versioned. Preserve any ignored
measurements needed to audit a result, and do not use `git clean -fdx`. For host
crash dumps, use the documented `experiment host clean` preview/apply commands.

The other scripts support
installation, image preparation, integration checks, or analysis; they are not
alternate experiment launch interfaces. The old `scripts/lab` and
`snapshot-host.sh` flows are older single-node helpers; configure new hosts with
`clawbox experiment host inspect/init/check/apply`.
