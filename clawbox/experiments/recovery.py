"""Durable trace boundaries; never replay side effects into retained learning."""
from pathlib import Path
import json
import os


class SessionRecovery:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def previous(self, session_id):
        path = self.root / (session_id + '.json')
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def write(self, session_id, **record):
        from .host_kb import _sync_directory
        path = self.root / (session_id + '.json')
        temp = path.with_suffix('.tmp')
        with temp.open('w', encoding='utf-8') as stream:
            json.dump(dict(session_id=session_id, **record), stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
        _sync_directory(self.root)

    def run(self, session_id, operation):
        previous = self.previous(session_id)
        if previous:
            if previous['status'] == 'completed':
                return {**previous['result'], 'recovered_from_previous_invocation': True}
            raise RuntimeError(
                f'{session_id}: incomplete trace retained; Tool filesystem and Agent progress '
                'cannot be recovered. No calls were replayed. Use an explicit new --attempt-id to restart cold.')
        self.write(session_id, status='started')
        try:
            result = operation()
        except BaseException as exc:
            self.write(session_id, status='incomplete', error=f'{type(exc).__name__}: {exc}')
            raise
        self.write(session_id, status='completed', result=result)
        return result
