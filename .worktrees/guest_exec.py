import sys

sys.path.insert(0, "/home/weitianc/ClawBox-tiered-2906a91")
from cubesandbox import Sandbox
from clawbox.experiments.openclaw_driver import native_tool_bridge_setup_command

sandbox = Sandbox.connect("9d6b377e90d24c98821a04df80bddc80")
setup = sandbox.commands.run(native_tool_bridge_setup_command(), timeout=45)
print("setup", setup.exit_code, setup.stdout, setup.stderr)
result = sandbox.commands.run(
    "uname -r; hostname -I; cat /proc/net/fib_trie; cat /proc/net/route; "
    "cat /proc/net/tcp; ps -ef",
    timeout=30,
)
print("exit", result.exit_code)
print(result.stdout)
print(result.stderr)
