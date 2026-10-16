You've already spent a lot of time fixing issues. But this is just a paper project to test the effectiveness of our ideas, not an industry product. So you don't need to make it overly robust. As long as the bugs/errors/issues don't affect the results for our ideas, it's fine to keep the results or even ignore some issues. You don't have to rerun or rebuild again and again and waste my time.

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

# Self-service usability requirements

- A user must be able to install/configure a new host, reuse an existing host,
  recover after reboot or interruption, and run experiments by entering the
  documented commands in order, without Codex or undocumented machine state.
- Every supported feature must have an editable configuration field or public
  command option. Explain units, defaults, prerequisites, and which changes
  require host apply, image rebuild, retraining, or a new run ID.
- Encapsulate repeated or error-prone environment, privilege, installation,
  registration, cleanup, and experiment steps in maintained scripts or the
  existing CLI. Do not introduce a second experiment execution path.
- Detect verifiable host facts and reuse explicit saved settings. Show proposed
  configuration for review; never guess template identities, artifact provenance,
  measured values, credentials, or available capacity from physical capacity alone.
- Commands must fail with actionable remedies and nonzero status. Do not hide a
  failed step, silently weaken a policy, or claim success from service startup.
- Preserve existing configuration, results, VM/template storage and unrelated
  workloads. Environment maintenance includes crash-dump cleanup with a preview,
  explicit apply, narrow file selection, and protection against recent/growing
  files; do not delete arbitrary large files or prune Docker volumes automatically.
- Update the scenario walkthrough and option reference with each public change.
  Validate documented commands on `ssh kunpeng`, including fresh-shell use,
  repeat execution, failures, and cleanup. Exercise fresh installation in an
  isolated environment; do not reinstall a shared host just to test a command.
- Keep auditable verification artifacts separate from user documentation. Distinguish
  real VM end-to-end validation, isolated installation, unit tests and syntax/help
  checks. State untested environments or blocked commands explicitly; an existing
  host test alone must never be described as proof of clean-machine installation.
