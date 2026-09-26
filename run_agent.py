"""Entry point for the packaged agent (PyInstaller needs a script, not ``-m``).

Running from source, prefer ``python -m agent.main``; this file exists so the
frozen executable has a single well-defined start, and so double-clicking it
does something useful instead of flashing a console window and vanishing.

Double-click behaviour:
  * no arguments        -> start the daemon, and hold the window open on exit
  * a folder dropped on -> scan that folder
Anything else is passed straight through to the normal CLI.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import traceback

from agent.main import main

COMMANDS = {"run", "scan", "status", "health", "-h", "--help"}

BANNER = r"""
  Kavach agent - starting the daemon.
  It polls the server for scan commands and stays running.
  Press Ctrl+C to stop.
"""


def _frozen() -> bool:
    return getattr(sys, "frozen", False)


def _launched_from_explorer() -> bool:
    """True when someone double-clicked the exe or dropped a folder on it.

    A console launch always names a command; Explorer passes either nothing or
    the dropped path.  Only then do we hold the window open, so piping and
    scripting keep working normally.
    """
    if not _frozen():
        return False
    if len(sys.argv) == 1:
        return True
    return len(sys.argv) == 2 and sys.argv[1] not in COMMANDS and os.path.exists(sys.argv[1])


def _argv_for_explorer() -> list[str]:
    if len(sys.argv) == 2:                      # a file or folder was dropped on the exe
        return ["scan", sys.argv[1]]
    print(BANNER)
    return ["run"]


if __name__ == "__main__":
    multiprocessing.freeze_support()     # harmless here, required if a child is ever spawned

    explorer = _launched_from_explorer()
    argv = _argv_for_explorer() if explorer else None

    try:
        code = main(argv)
    except KeyboardInterrupt:
        print("\nstopped.")
        code = 0
    except Exception:                    # noqa: BLE001 - last resort, so the window shows why
        traceback.print_exc()
        code = 1

    if explorer:
        # Without this the console closes instantly and nobody sees the error.
        try:
            input("\nPress Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass

    sys.exit(code)
