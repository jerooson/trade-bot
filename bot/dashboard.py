"""
Convenience launcher: start the FastAPI backend AND the Vite dev server together.

Run:
    python -m bot.dashboard

Then open http://localhost:5173 in your browser.

Both processes share the parent terminal. Ctrl-C stops both.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
PY = sys.executable  # the venv's python


def _have_node_modules() -> bool:
    return (WEB_DIR / "node_modules").exists()


def main() -> None:
    if not WEB_DIR.exists():
        sys.exit(f"web/ directory not found at {WEB_DIR}")

    if not _have_node_modules():
        print("[dashboard] web/node_modules missing -- running `npm install` first...")
        npm_cmd = "npm.cmd" if os.name == "nt" else "npm"
        rc = subprocess.call([npm_cmd, "install"], cwd=WEB_DIR)
        if rc != 0:
            sys.exit(f"npm install failed with code {rc}")

    print("[dashboard] starting FastAPI on :8787 and Vite on :5173 ...")
    print("[dashboard] open http://localhost:5173 in your browser.")
    print("[dashboard] Ctrl-C to stop both.")
    print()

    backend = subprocess.Popen(
        [PY, "-m", "server.api"],
        cwd=ROOT,
    )

    npm_cmd = "npm.cmd" if os.name == "nt" else "npm"
    frontend = subprocess.Popen(
        [npm_cmd, "run", "dev"],
        cwd=WEB_DIR,
    )

    procs = [backend, frontend]

    def _shutdown(*_a) -> None:
        for p in procs:
            if p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    try:
        # Wait for either process to exit; if one dies, kill the other.
        while True:
            for p in procs:
                rc = p.poll()
                if rc is not None:
                    print(f"[dashboard] process exited with code {rc}; shutting down peer.")
                    _shutdown()
                    return
            import time
            time.sleep(0.5)
    finally:
        _shutdown()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
