import pytest
from clawbox.experiments.recovery import SessionRecovery


def test_resume_keeps_completed_and_never_reexecutes_interrupted_trace(tmp_path):
    progress = SessionRecovery(tmp_path)
    calls = []
    assert progress.run('a', lambda: calls.append('a') or {'valid': True})['valid']
    progress.write('b', status='started')
    resumed = SessionRecovery(tmp_path)
    assert resumed.run('a', lambda: calls.append('duplicate'))['recovered_from_previous_invocation']
    with pytest.raises(RuntimeError, match='incomplete trace retained'):
        resumed.run('b', lambda: calls.append('unsafe replay'))
    resumed.run('c', lambda: calls.append('c') or {'valid': True})
    assert calls == ['a', 'c']
    cold = SessionRecovery(tmp_path/'new-attempt')
    cold.run('a', lambda: calls.append('a') or {'valid': True})
    assert calls == ['a', 'c', 'a']
