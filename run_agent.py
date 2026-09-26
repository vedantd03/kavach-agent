"""Entry point for the packaged agent (PyInstaller needs a script, not ``-m``).

Running from source, prefer ``python -m agent.main``; this file exists so the
frozen executable has a single well-defined start.
"""

from __future__ import annotations

import multiprocessing
import sys

from agent.main import main

if __name__ == "__main__":
    multiprocessing.freeze_support()     # harmless here, required if a child is ever spawned
    sys.exit(main())
