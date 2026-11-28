import builtins
import sys

import pytest

from clawbox.experiments.policy_http import policy_event_loop


def test_auto_requires_uvloop_on_linux_with_actionable_failure(monkeypatch):
    original = builtins.__import__
    def missing(name, *args, **kwargs):
        if name == 'uvloop':
            raise ImportError('test missing dependency')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(builtins, '__import__', missing)
    with pytest.raises(RuntimeError, match='pip install -e'):
        policy_event_loop('auto')
    assert policy_event_loop('asyncio')[0] == 'asyncio'


def test_auto_uses_asyncio_outside_linux(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')
    name, factory = policy_event_loop('auto')
    loop = factory()
    try:
        assert name == 'asyncio'
        assert loop.run_until_complete(__import__('asyncio').sleep(0, result=7)) == 7
    finally:
        loop.close()


def test_unknown_loop_is_rejected():
    with pytest.raises(ValueError, match='policy_http_loop'):
        policy_event_loop('unknown')
