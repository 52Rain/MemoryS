"""Compatibility entry point for the shared MemoryS library."""
from pathlib import Path
import sys

if (Path(__file__).parent / "service_library.py").is_file():
    from service_library import *
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from runtime.service_library import *
