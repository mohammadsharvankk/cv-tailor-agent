"""
Desktop launcher: what CV-Tailor.exe runs.

It keeps everything personal next to the program, so the folder is portable (copy it to another PC or a
USB stick and your settings and master CV come with it):

    CV-Tailor/
      CV-Tailor.exe
      .env                  <- your provider and API key (written by the Settings panel)
      my_cv/master_cv.md    <- your master CV

It picks a free port on this computer only (127.0.0.1), opens your browser, and keeps running until you
close the window.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path


def app_folder() -> Path:
    """The folder that holds the .exe (or the project folder when run from source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def free_port(start: int = 8000, tries: int = 50) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port between 8000 and 8049")


def main() -> None:
    folder = app_folder()
    os.chdir(folder)  # .env and my_cv/ are read and written here
    os.environ.setdefault("MODE", "local")
    port = free_port()
    url = f"http://127.0.0.1:{port}"

    print("=" * 60)
    print("  CV Tailor Agent")
    print(f"  Open in your browser: {url}")
    print(f"  Your settings and master CV are kept in: {folder}")
    print("  Close this window to stop the app.")
    print("=" * 60, flush=True)

    threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    import uvicorn

    from app.main import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
