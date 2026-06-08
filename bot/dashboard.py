"""
Convenience launcher: start the FastAPI backend, the Vite dev server,
the Discord listener, and the trade executor together.

Run:
    python -m bot.dashboard                 # everything
    python -m bot.dashboard --no-listener   # dashboard only (api + vite)
    python -m bot.dashboard --no-executor   # listener + dashboard, no executor

Then open http://localhost:5173 in your browser.

All processes share the parent terminal. Ctrl-C stops all of them.

If the listener or executor crashes (e.g. bad token, expired session, parser
exception), the dashboard keeps running so you can still inspect historical
data — you'll see a warning in this terminal.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
PY = sys.executable


def _have_node_modules() -> bool:
    return (WEB_DIR / "node_modules").exists()


def _have_listener_config() -> bool:
    """Listener needs a .env with the token. Skip with a warning if missing."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return False
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return "DISCORD_USER_TOKEN" in text and "DISCORD_CHANNEL_IDS" in text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-listener",
        action="store_true",
        help="Do not auto-start the Discord listener (just run the dashboard).",
    )
    parser.add_argument(
        "--no-executor",
        action="store_true",
        help="Do not auto-start the trade executor (DRY_RUN mode).",
    )
    args = parser.parse_args()

    if not WEB_DIR.exists():
        sys.exit(f"web/ directory not found at {WEB_DIR}")

    if not _have_node_modules():
        print("[dashboard] web/node_modules missing -- running `npm install` first...")
        npm_cmd = "npm.cmd" if os.name == "nt" else "npm"
        rc = subprocess.call([npm_cmd, "install"], cwd=WEB_DIR)
        if rc != 0:
            sys.exit(f"npm install failed with code {rc}")

    start_listener = not args.no_listener
    if start_listener and not _have_listener_config():
        print("[dashboard] WARNING: .env is missing or lacks DISCORD_USER_TOKEN/DISCORD_CHANNEL_IDS.")
        print("[dashboard] Skipping the Discord listener; dashboard will only show historical data.")
        print("[dashboard] Re-run with `python -m bot.dashboard` after creating .env.")
        start_listener = False

    start_executor = not args.no_executor

    pieces = ["FastAPI on :8787", "Vite on :5173"]
    if start_listener:
        pieces.append("Discord listener")
    if start_executor:
        pieces.append("Executor (DRY_RUN)")
    print(f"[dashboard] starting {', '.join(pieces)} ...")
    print("[dashboard] open http://localhost:5173 in your browser.")
    print("[dashboard] Ctrl-C to stop everything.")
    print()

    backend = subprocess.Popen([PY, "-m", "server.api"], cwd=ROOT)

    npm_cmd = "npm.cmd" if os.name == "nt" else "npm"
    frontend = subprocess.Popen([npm_cmd, "run", "dev"], cwd=WEB_DIR)

    listener: subprocess.Popen | None = None
    if start_listener:
        listener = subprocess.Popen(
            [PY, "-u", "-m", "bot.listener"],
            cwd=ROOT,
        )

    executor: subprocess.Popen | None = None
    if start_executor:
        executor = subprocess.Popen(
            [PY, "-u", "-m", "bot.executor"],
            cwd=ROOT,
        )

    # Critical: a listener or executor crash should NOT take down the dashboard.
    critical = [("backend", backend), ("frontend", frontend)]
    optional: list[tuple[str, subprocess.Popen]] = []
    if listener is not None:
        optional.append(("listener", listener))
    if executor is not None:
        optional.append(("executor", executor))

    def _shutdown(*_a) -> None:
        for _, p in critical + optional:
            if p is not None and p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    try:
        warned: set[str] = set()
        while True:
            for name, p in critical:
                rc = p.poll()
                if rc is not None:
                    print(f"[dashboard] {name} exited with code {rc}; shutting down peers.")
                    _shutdown()
                    return

            for name, p in optional:
                if p is None:
                    continue
                rc = p.poll()
                if rc is not None and name not in warned:
                    print(
                        f"[dashboard] WARNING: {name} exited with code {rc}. "
                        "Dashboard will keep running."
                    )
                    if name == "listener":
                        print("[dashboard] Listener common causes: invalid token, network blip, Discord rate limit.")
                        print("[dashboard] New signals will NOT be captured. Run `python -m bot.listener` to retry.")
                    elif name == "executor":
                        print("[dashboard] Executor stopped: no new trade decisions will be made.")
                        print("[dashboard] Run `python -m bot.executor` to retry, after fixing the cause.")
                    warned.add(name)

            time.sleep(0.5)
    finally:
        _shutdown()
        for _, p in critical + optional:
            if p is None:
                continue
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
