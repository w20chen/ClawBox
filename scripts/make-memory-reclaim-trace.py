#!/usr/bin/env python3
"""Generate a phased diagnostic workload for the normal experiment CLI."""
import argparse
import json
from pathlib import Path
import shlex


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--allocation-mib", type=int, default=64)
    parser.add_argument("--hold-seconds", type=float, default=3)
    parser.add_argument("--settle-seconds", type=float, default=5)
    args = parser.parse_args()
    import math
    if args.allocation_mib <= 0 or any(not math.isfinite(v) or v <= 0
        for v in (args.hold_seconds, args.settle_seconds)):
        parser.error("allocation, hold and settle must be finite and positive")
    code = f'''import json,mmap,time
from pathlib import Path
phases=[]
artifact=Path('/var/lib/clawtune/artifacts/tool-resource/memory-reclaim-phases.json')
if not artifact.parent.is_dir():
    raise RuntimeError('Tool telemetry artifact directory missing; rebuild the supported Tool image')
def phase(name):
    row=dict(phase=name, wall_time_ns=time.time_ns(),
        guest_meminfo=Path('/proc/meminfo').read_text(),
        guest_process_status=Path('/proc/self/status').read_text())
    phases.append(row)
    temporary=artifact.with_suffix('.next')
    temporary.write_text(json.dumps(dict(schema_version=1, allocation_mib={args.allocation_mib}, phases=phases)))
    temporary.replace(artifact)
    print(json.dumps(dict(phase=name, wall_time_ns=row['wall_time_ns'])), flush=True)
phase('before_touch')
time.sleep({args.settle_seconds!r})
data=mmap.mmap(-1, {args.allocation_mib}*1024*1024)
for offset in range(0, len(data), mmap.PAGESIZE):
    data[offset]=1
phase('after_touch')
time.sleep({args.hold_seconds!r})
data.close()
phase('after_guest_unmap')
time.sleep({args.settle_seconds!r})
phase('after_guest_settle')
Path('/workspace/result.txt').write_text('complete')
'''
    command = "python3 -u -c " + shlex.quote(code)
    call = {"id": "memory-phases", "type": "function", "function": {
        "name": "exec", "arguments": json.dumps({"command": command})}}
    prompt = f"Touch {args.allocation_mib} MiB, unmap it, observe settling, and write complete to /workspace/result.txt."
    user = {"role": "user", "content": prompt}
    answer = {"role": "assistant", "content": None, "tool_calls": [call]}
    rows = []
    for seq, messages, content in (
        (0, [user], answer),
        (1, [user, answer, {"role": "tool", "tool_call_id": "memory-phases", "content": ""}],
         {"role": "assistant", "content": "Created /workspace/result.txt containing complete."}),
    ):
        base = dict(schema_version=6, trace_id="memory-reclaim-diagnostic", span_id=f"model-{seq}",
                    kind="llm", name="example-model", sequence_no=seq)
        rows.append(dict(base, record_type="span_start", wall_time_ns=str(seq*1000000000),
                         input=dict(messages=messages, requested_args=None)))
        rows.append(dict(base, record_type="span_end", duration_ns="10000000",
                         status={"code": "ok"}, output={"content": content}))
    # Never replace a trace already referenced by a run or calibration artifact.
    try:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write("".join(json.dumps(row) + "\n" for row in rows))
    except OSError as exc:
        parser.error(f"cannot create {args.output}: {exc}; reuse the existing trace or choose a new writable path")


if __name__ == "__main__":
    main()
