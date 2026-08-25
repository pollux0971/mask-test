"""Make the bridge tests runnable from the repo root and from this directory.

They import both `host.*` (needs the repo root on sys.path) and their own
sibling helper module (needs this directory). pytest adds one or the other
depending on where it was invoked from, so add both explicitly.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

for path in (str(ROOT), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)
