# Project scope

- The supported product is the standalone `clawbox experiment` workflow,
  CubeSandbox execution, and its ClawTune prediction/measurement integration.
- Legacy control-plane modules listed in `docs/deprecated.md` are deprecated.
  Keep their source, but do not extend them or preserve their compatibility in
  future changes. Historical APIs, schemas, aliases, and stored-state formats do
  not constrain the supported workflow; do not add compatibility fallbacks.
- Some legacy directories contain shared code still called by the supported
  workflow. Preserve that functionality or move it to an active module before
  changing it. A directory's age alone does not make its contents unused.
- Prioritize experiment correctness: propagate policy failures, honor configured
  snapshot mechanisms, and keep measured behavior consistent with result metadata.
