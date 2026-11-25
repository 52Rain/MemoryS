"""Compatibility entry point for the shared MemoryS backend."""
from pathlib import Path
import sys

if (Path(__file__).parent / "service_backend.py").is_file():
    from service_backend import *
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from runtime.service_backend import *

if __name__ == "__main__":
    run_stdio()
