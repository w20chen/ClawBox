from pathlib import Path

from cubesandbox import NEVER_TIMEOUT, Sandbox


template = "tpl-2cd6abbb96b6467bbe43b0b6"
node = "193.124.7.2"
target = Path("/home/weitianc/clawbox-tiered-study-20260907/overrides/telemetry-from-tool-image.py")
sandbox = Sandbox.create(
    template=template,
    timeout=NEVER_TIMEOUT,
    lifecycle={"on_timeout": "kill", "auto_resume": False},
    metadata={"clawbox.owner": "diagnostic-source-dump"},
    distribution_scope=[node],
)
try:
    source = sandbox.files.read(
        "/opt/clawtune-guest/services/sidecar/src/tool_resource/telemetry.py"
    )
    target.write_text(source)
    print(target)
finally:
    sandbox.kill()
