"""Test path setup for Project utility modules."""

import sys
from pathlib import Path

_UTILS_DIR = Path(__file__).parent / "utils"
if str(_UTILS_DIR) not in sys.path:
    sys.path.insert(0, str(_UTILS_DIR))
