# Scripts

The supported interface is `clawbox experiment`. Start with the
[self-service workflow](../docs/self-service.md); use the
[command reference](../docs/guide.md) for options and the
[installation guide](../docs/installation.md) for provisioning.

`scripts/clawbox` forwards arguments to the CLI. The other scripts support
installation, image preparation, integration checks, or analysis; they are not
alternate experiment launch interfaces. The old `scripts/lab` and
`snapshot-host.sh` flows are older single-node helpers; configure new hosts with
`clawbox experiment host inspect/init/check/apply`.
