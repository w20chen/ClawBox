"""Retain completed prediction evidence without a growing GC container graph."""
import json
import zlib
from threading import Lock


class PredictionRecordBuffer:
    def __init__(self):
        self._lock = Lock()
        self._rows = []
        self._active = {}

    def append(self, record):
        with self._lock:
            execution = record['execution_id']
            self._active[execution] = len(self._rows)
            self._rows.append(record)

    def complete(self, execution, observation):
        with self._lock:
            index = self._active.pop(execution, None)
            if index is None:
                return
            record = self._rows[index]
            record.update(observation)
            increment = observation.get('host_cgroup_execution_increment_bytes')
            record['actual_host_execution_increment_mib'] = (
                float(increment) / (1024.0 * 1024.0)
                if isinstance(increment, int) else None)
            self._rows[index] = zlib.compress(json.dumps(record, separators=(',', ':')).encode(), level=1)

    def records(self):
        with self._lock:
            return [json.loads(zlib.decompress(row)) if isinstance(row, bytes) else dict(row)
                    for row in self._rows]
