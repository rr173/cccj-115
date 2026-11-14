"""Subprocess entry point for the deliberate os._exit crash-hook test.

Runs under ``python3 -I`` (isolated mode, so no environment/site-package
influence): it only bootstraps the repository path explicitly, then hands
control to the real CLI.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session_ledger.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
