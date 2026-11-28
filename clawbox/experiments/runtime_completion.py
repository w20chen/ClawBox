"""Wait locally for a Runtime wakeup, reconciling against its exit file."""
import time


def wait_runtime_completion(phase, path, *, wait, probe, interval_seconds,
                            deadline, cancelled=None, event=None):
    started = time.monotonic()
    next_probe = started + interval_seconds
    checks = 0
    notified = False
    while True:
        if cancelled is not None and cancelled():
            raise InterruptedError('OpenClaw completion wait cancelled after arm failure')
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            raise TimeoutError('OpenClaw completion wait timed out')
        duration = min(1., max(0., next_probe-now))
        if deadline is not None:
            duration = min(duration, deadline-now)
        notification = wait(phase, duration)
        notified = notified or notification
        if cancelled is not None and cancelled():
            raise InterruptedError('OpenClaw completion wait cancelled after arm failure')
        if not notification and time.monotonic() < next_probe:
            continue
        remaining = 10. if deadline is None else max(.001, min(10., deadline-time.monotonic()))
        ready = probe(path, remaining)
        checks += 1
        if ready:
            if event is not None:
                event({'event': 'runtime_completion_wait', 'phase': phase,
                       'notification_observed': notified, 'file_checks': checks,
                       'wait_seconds': time.monotonic()-started})
            return
        # A wakeup is only a hint: a paused/replaced Runtime or a lost message
        # cannot prove completion. Retry the file check on the same bound.
        next_probe = time.monotonic() + interval_seconds
