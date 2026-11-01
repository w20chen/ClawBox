"""Optional Linux forkserver seed preload; never fork the running worker."""
import os
from pathlib import Path

SEED = None
if path := os.environ.get('CLAWBOX_KB_PRELOAD_SEED'):
    from clawbox.clawtune_integration import use_clawtune
    use_clawtune()
    from .host import SharedSeed
    SEED = SharedSeed(Path(path))
