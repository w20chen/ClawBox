import pytest

from clawbox.experiments import numa_policy


def test_policy_configuration_and_live_process_are_both_required(monkeypatch):
    observed = {"configured": "local", "actual": "default", "pid": 123}
    monkeypatch.setattr(numa_policy, "state", lambda: observed.copy())
    # A stale live Cubelet still needs restart after daemon-reload.
    assert numa_policy.configure("local") is True
    with pytest.raises(RuntimeError, match="idle VM pool"):
        numa_policy.verify("local")
    observed["actual"] = "local"
    assert numa_policy.configure("local") is False
    assert numa_policy.verify("local") == observed


def test_null_policy_never_touches_services(monkeypatch):
    def unexpected():
        raise AssertionError("must not inspect or change services")
    monkeypatch.setattr(numa_policy, "state", unexpected)
    assert numa_policy.configure(None) is False


def test_policy_writes_only_cubelet_dropin_and_detects_conflicts(monkeypatch):
    monkeypatch.setattr(numa_policy, "state", lambda: {
        "configured": "default", "actual": "default", "pid": 123})
    calls = []
    def command(*args):
        calls.append(args)
        return "local" if args[0] == "systemctl" else ""
    monkeypatch.setattr(numa_policy, "command", command)
    assert numa_policy.configure("local") is True
    assert calls[0][-2:] == (numa_policy.DROP_IN, "local")
    assert "shutil.copy2" in calls[0][4]
    assert not any("restart" in c or "sysctl" in c for c in calls)
    monkeypatch.setattr(numa_policy, "command", lambda *a: "default")
    with pytest.raises(RuntimeError, match="conflicting drop-ins"):
        numa_policy.configure("local")


def test_live_policy_reading_rejects_stopped_service(monkeypatch):
    def command(*args):
        if "NUMAPolicy" in args: return "n/a"
        if "MainPID" in args: return "123"
        return "00400000 default file=/usr/bin/cubelet mapped=1 N0=1\n"
    monkeypatch.setattr(numa_policy, "command", command)
    assert numa_policy.state() == {"configured": "default", "actual": "default", "pid": 123}
    monkeypatch.setattr(numa_policy, "command", lambda *a: "0" if "MainPID" in a else "local")
    with pytest.raises(RuntimeError, match="no running MainPID"):
        numa_policy.state()
