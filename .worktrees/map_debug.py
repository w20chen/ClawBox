import base64
import pathlib
import subprocess
import tempfile
import time
from cubesandbox import Sandbox

key_dir = pathlib.Path(tempfile.mkdtemp(prefix="clawbox-map-debug-"))
client_key = key_dir / "client"
host_key = key_dir / "host"
subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(client_key)], check=True)
subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)], check=True)

sandbox = Sandbox.create(
    template="tpl-72f8a42d8fe746279d0bb80a",
    timeout=600,
    lifecycle={"on_timeout": "kill", "auto_resume": False},
    metadata={"clawbox.owner": "map-debug"},
    distribution_scope=["hostname-txyuq.foreman.pxe"],
    env_vars={
        "CLAWBOX_VM_ROLE": "tool",
        "CLAWBOX_TOOL_HOST_KEY_B64": base64.b64encode(host_key.read_bytes()).decode(),
        "CLAWBOX_TOOL_AUTHORIZED_KEY_B64": base64.b64encode(
            client_key.with_suffix(".pub").read_bytes()
        ).decode(),
        "TASK_ID": "map-debug",
    },
)
print(sandbox.sandbox_id, sandbox.get_tcp_endpoint(2222), flush=True)
print(sandbox.commands.run("cat /proc/net/tcp; ps -ef", timeout=30).stdout, flush=True)
time.sleep(600)
