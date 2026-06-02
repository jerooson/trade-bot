"""
Convenience launcher: start the FastAPI backend, the Vite dev server,
and the Discord listener together.

Run:
    python -m bot.dashboard                # all three
    python -m bot.dashboard --no-listener  # just dashboard (api + vite)

Then open http://localhost:5173 in your browser.

All processes share the parent terminal. Ctrl-C stops all of them.

If the listener crashes (e.g. bad token, expired session), the dashboard
keeps running so you can still inspect historical data.
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

    pieces = ["FastAPI on :8787", "Vite on :5173"]
    if start_listener:
        pieces.append("Discord listener")
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

    # Critical: a listener crash should NOT take down the dashboard.
    critical = [("backend", backend), ("frontend", frontend)]
    optional = [("listener", listener)] if listener is not None else []

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
        listener_warned = False
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
                if rc is not None and not listener_warned:
                    print(
                        f"[dashboard] WARNING: {name} exited with code {rc}. "
                        "Dashboard will keep running, but new signals will NOT be captured."
                    )
                    print("[dashboard] Common causes: invalid token, network blip, Discord rate limit.")
                    print("[dashboard] Run `python -m bot.listener` in a fresh terminal to retry.")
                    listener_warned = True

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
