#!/usr/bin/env python3
"""Send an optional wakeup after the authoritative Runtime exit file is written."""
import json
import os
import sys
import urllib.request


def main():
    phase = sys.argv[1]
    if phase not in {'agent', 'subagents'}:
        raise ValueError('invalid Runtime completion phase')
    request = urllib.request.Request(
        os.environ['CLAWBOX_POLICY_CONTROL_URL'].rstrip('/') + '/v1/runtime/completion',
        data=json.dumps({'session_id': os.environ['CLAWBOX_POLICY_SESSION_ID'], 'phase': phase}).encode(),
        headers={'Authorization': 'Bearer ' + os.environ['CLAWBOX_POLICY_CONTROL_AUTH'],
                 'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            result = json.load(response)
        if result.get('status') != 'NOTIFIED' or result.get('phase') != phase:
            raise ValueError('invalid Runtime completion acknowledgement')
    except Exception as error:
        # This notification does not authorize policy work or determine task
        # success. The host still checks the exit file on its bounded interval.
        print('Runtime completion wakeup not delivered: ' + type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
