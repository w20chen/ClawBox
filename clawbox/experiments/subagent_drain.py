"""Wait for native OpenClaw child cleanup and logical Session completion.

Invoked by the existing experiment driver inside its private Runtime VM.
Reads the installed OpenClaw registry; never modifies agent/session storage.
"""
import argparse
import json
import os
from pathlib import Path
import time
import urllib.request


def drained(snapshots, runs, root_session_id):
    if not any(row['identity']['session_id'] == root_session_id for row in snapshots):
        return False
    if any(row['payload']['phase'] != 'IDLE' for row in snapshots):
        return False
    # Cleanup is after native announcement/delivery handling, not just process
    # exit. Parent return alone must never destroy a still-running child.
    return all(isinstance(run.get('endedAt'), (int, float))
               and isinstance(run.get('cleanupCompletedAt'), (int, float))
               and run['cleanupCompletedAt'] >= run['endedAt'] for run in runs.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--root-session-id', required=True)
    parser.add_argument('--timeout', type=float, required=True)
    args = parser.parse_args()
    deadline = time.monotonic() + args.timeout
    stable_since = None
    previous = None
    headers = {'Authorization': 'Bearer ' + os.environ['CLAWTUNE_TOKEN']} if os.environ.get('CLAWTUNE_TOKEN') else {}
    while time.monotonic() < deadline:
        request = urllib.request.Request('http://127.0.0.1:8765/v1/sessions', headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            snapshots = json.load(response)['sessions']
        path = args.state_dir / 'subagents' / 'runs.json'
        runs = json.loads(path.read_text(encoding='utf-8'))['runs'] if path.exists() else {}
        if not isinstance(runs, dict):
            raise RuntimeError('OpenClaw subagent registry must contain an object named runs')
        signature = [(row['identity'], row['payload']['state_revision']) for row in snapshots]
        if drained(snapshots, runs, args.root_session_id):
            if stable_since is None or signature != previous:
                stable_since = time.monotonic()
            if stable_since is not None and time.monotonic() - stable_since >= 5:
                print(json.dumps({'sessions': snapshots, 'subagent_runs': len(runs),
                                  'all_sessions_idle': True, 'native_cleanup_complete': True}))
                return
        else:
            stable_since = None
        previous = signature
        time.sleep(.5)
    raise TimeoutError('OpenClaw child sessions or parent continuation did not finish within the remaining arm timeout')


if __name__ == '__main__':
    main()
