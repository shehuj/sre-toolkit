"""Test package.

The source tree is put on sys.path here rather than in a helper module, so no
test file depends on its own import order. `make test` and CI also set
PYTHONPATH; this makes a bare `python -m unittest discover -s tests` work too.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
