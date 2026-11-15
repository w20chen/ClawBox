"""Small, payload-free diagnostics shared by the simulator adapter and client."""
from __future__ import annotations

import asyncio
import json
import time
import traceback
from pathlib import Path


def exception_record(exc: BaseException) -> dict:
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(dict(type=f'{type(current).__module__}.{type(current).__name__}',
                          message=str(current), repr=repr(current),
                          errno=getattr(current, 'errno', None)))
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return dict(chain=chain, traceback=traceback.format_exception(type(exc), exc, exc.__traceback__))


class Diagnostics:
    """One writer per event loop; no request bodies or per-token records."""

    def __init__(self, path: Path):
        self.stream = path.open('x', encoding='utf-8', buffering=1)

    def write(self, event: str, **fields):
        self.stream.write(json.dumps(dict(event=event, unix_s=time.time(), **fields),
                                     separators=(',', ':')) + '\n')

    async def sample_loop(self, interval_s: float):
        import psutil
        process = psutil.Process()
        while True:
            deadline = time.monotonic() + interval_s
            await asyncio.sleep(interval_s)
            lag = max(0., time.monotonic() - deadline)
            with process.oneshot():
                fields = dict(loop_lag_seconds=lag, rss_bytes=process.memory_info().rss,
                              cpu_seconds=sum(process.cpu_times()[:2]))
                if hasattr(process, 'num_fds'):
                    fields['open_fds'] = process.num_fds()
            self.write('process_sample', **fields)

    def close(self):
        self.stream.close()
