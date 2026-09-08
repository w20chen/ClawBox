from clawbox.cube.client import CubeSandboxClient, Ownership

client = CubeSandboxClient()
owner = 'post-reboot-create-20260908'
try:
    sandbox = client.create_sandbox(
        template='tpl-f623b38f249f485cafc91478', node_name='193.124.7.2',
        ownership=Ownership(owner, owner, owner, owner, owner, 'probe'),
        allow_internet_access=False)
    print('CREATE_OK', client.sandbox_id(sandbox), flush=True)
finally:
    client.kill_owned_sandboxes(owner)
    print('CLEANUP_OK', flush=True)
