import atexit
import os
import tempfile
from pathlib import Path


_test_database = Path(tempfile.gettempdir()) / f"clawbox-tests-{os.getpid()}.db"
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_test_database.as_posix()}")
os.environ.setdefault("CONTROLLER_BACKEND", "subprocess")
os.environ.setdefault("NUMA_CAPACITY", "0:64")


@atexit.register
def _remove_test_database() -> None:
    try:
        from clawbox.common.db import engine
        engine.dispose()
    except ImportError:
        pass
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(f"{_test_database}{suffix}").unlink(missing_ok=True)
        except PermissionError:
            # Windows may keep a TestClient worker handle alive until after
            # Python's atexit callbacks.  The unique file stays in TEMP only.
            pass
