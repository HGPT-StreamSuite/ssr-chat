from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .analysis import TriggerDetector
from .api import serve
from .application import build_runtime


def _default_fixtures() -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ssr-chat")
    sub = parser.add_subparsers(dest="command", required=True)

    chat = sub.add_parser("chat", help="處理一個本機 JSON 事件")
    chat.add_argument("--input", required=True, help="JSON 檔路徑，或 - 表示 stdin")
    chat.add_argument("--state-dir", default=".local-state")
    chat.add_argument("--fixture-dir", default=str(_default_fixtures()))

    api = sub.add_parser("serve", help="啟動僅綁 localhost 的 HTTP API")
    api.add_argument("--port", type=int, default=8765)
    api.add_argument("--state-dir", default=".local-state")
    api.add_argument("--fixture-dir", default=str(_default_fixtures()))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime = build_runtime(
        args.state_dir,
        fixture_dir=args.fixture_dir,
    )
    if args.command == "serve":
        if not 1 <= args.port <= 65535:
            print("port must be between 1 and 65535", file=sys.stderr)
            return 2
        serve(runtime, args.port)
        return 0

    try:
        if args.input == "-":
            event = json.load(sys.stdin)
        else:
            with Path(args.input).open("r", encoding="utf-8") as handle:
                event = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    result = runtime.handle(event)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
