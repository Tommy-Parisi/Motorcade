#!/usr/bin/env python3
"""
Continuously render and host the vertical dashboard.

This keeps `vertical_dashboard.html` fresh by regenerating it on an interval and
serves the output directory over HTTP so the page can be left open 24/7.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import threading
import time
from pathlib import Path

from render_vertical_dashboard import build_payload, render_html


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESEARCH_DIR = REPO_ROOT / "var" / "research"
DEFAULT_OUTPUT = REPO_ROOT / "vertical_dashboard.html"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--research-dir",
        default=str(DEFAULT_RESEARCH_DIR),
        help="Research directory to read (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Dashboard HTML path to regenerate and serve (default: %(default)s)",
    )
    parser.add_argument(
        "--since",
        default="0000-00-00",
        help="Only include rows on or after DATE (YYYY-MM-DD). Default: all time.",
    )
    parser.add_argument(
        "--refresh-seconds",
        type=int,
        default=60,
        help="How often to regenerate the dashboard snapshot. Default: %(default)s seconds.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="HTTP port to bind. Default: %(default)s",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="HTTP host/interface to bind. Default: %(default)s",
    )
    return parser.parse_args()


def render_once(research_dir: Path, output_path: Path, since: str, refresh_seconds: int) -> None:
    payload = build_payload(research_dir, since)
    output_path.write_text(render_html(payload, refresh_seconds))
    print(f"[vertical-dashboard] rendered {output_path}", flush=True)


def start_render_loop(research_dir: Path, output_path: Path, since: str, refresh_seconds: int) -> threading.Thread:
    def loop() -> None:
        while True:
            try:
                render_once(research_dir, output_path, since, refresh_seconds)
            except Exception as exc:  # pragma: no cover - operational guardrail
                print(f"[vertical-dashboard] render failed: {exc}", flush=True)
            time.sleep(max(1, refresh_seconds))

    thread = threading.Thread(target=loop, name="vertical-dashboard-render", daemon=True)
    thread.start()
    return thread


def main() -> None:
    args = parse_args()
    research_dir = Path(args.research_dir).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    render_once(research_dir, output_path, args.since, args.refresh_seconds)
    start_render_loop(research_dir, output_path, args.since, args.refresh_seconds)

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(output_path.parent))
    server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    print(
        f"[vertical-dashboard] serving http://{args.host}:{args.port}/{output_path.name} "
        f"(refresh every {args.refresh_seconds}s)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[vertical-dashboard] shutting down", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
