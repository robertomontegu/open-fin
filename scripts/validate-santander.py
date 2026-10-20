#!/usr/bin/env python3
"""Run the owner-local Santander wizard using the system's standard library."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

if __name__ == "__main__":
    from open_fin.local_validation import main

    raise SystemExit(main(project_root=PROJECT_ROOT))
