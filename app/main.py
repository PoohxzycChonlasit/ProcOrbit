from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from instance import SingleInstanceGuard


if __name__ == "__main__":
    show_now = "--show" in sys.argv
    if show_now:
        sys.argv.remove("--show")
    guard = SingleInstanceGuard()
    try:
        if not guard.acquire():
            raise SystemExit(0)
        # Take the mutex before loading Qt so secondary launches can signal the
        # existing window without importing the full GUI stack.
        from ui import PythonWatchApp
        raise SystemExit(PythonWatchApp(guard, show_initially=show_now).run())
    finally:
        guard.close()
