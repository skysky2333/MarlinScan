from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn

from .server import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local MarlinScan web service")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_open:
        opener = threading.Timer(0.8, webbrowser.open, args=(url,))
        opener.daemon = True
        opener.start()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
