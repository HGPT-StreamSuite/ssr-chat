from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .analysis import TriggerDetector
from .api import serve
from .application import build_runtime
from .models import AnswerPrompt
from .ollama_model import OllamaModelClient, OllamaModelError


def _default_fixtures() -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ssr-chat")
    sub = parser.add_subparsers(dest="command", required=True)

    chat = sub.add_parser("chat", help="處理一個本機 JSON 事件")
    chat.add_argument("--input", required=True, help="JSON 檔路徑，或 - 表示 stdin")
    chat.add_argument("--state-dir", default=".local-state")
    chat.add_argument("--fixture-dir", default=str(_default_fixtures()))
    _add_model_options(chat)

    api = sub.add_parser("serve", help="啟動僅綁 localhost 的 HTTP API")
    api.add_argument("--port", type=int, default=8765)
    api.add_argument("--state-dir", default=".local-state")
    api.add_argument("--fixture-dir", default=str(_default_fixtures()))
    _add_model_options(api)

    models = sub.add_parser("models", help="List models already installed in local Ollama")
    _add_ollama_connection_options(models)

    verify = sub.add_parser(
        "verify-ollama", help="Run a fixed synthetic question against an installed Ollama model"
    )
    _add_ollama_connection_options(verify)
    verify.add_argument("--ollama-model", required=True)
    return parser


def _add_ollama_connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ollama-endpoint", help="Loopback Ollama URL; defaults to SSR_CHAT_OLLAMA_ENDPOINT")
    parser.add_argument("--ollama-timeout", type=float, default=120.0)


def _add_model_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model-adapter", choices=("fixture", "ollama"), default="fixture")
    _add_ollama_connection_options(parser)
    parser.add_argument("--ollama-model", help="Installed model name; defaults to SSR_CHAT_OLLAMA_MODEL")


def _verification_prompt() -> AnswerPrompt:
    return AnswerPrompt(
        user_text="測試資料中的月光藥水回復多少點能量？",
        context_snapshot_id="verification-snapshot-001",
        context_messages=(
            {"message_id": "verification-context-001", "author_id": "fixture", "text": "這是合成測試對話。"},
        ),
        analysis={
            "intents": ["game_item"],
            "query_text": "月光藥水回復多少點能量？",
            "snapshot_id": "verification-snapshot-001",
            "queries": [{"purpose": "game_item", "query_text": "月光藥水回復多少點能量？"}],
        },
        query_results=(
            {
                "status": "ok",
                "materials": [{"text": "月光藥水回復 30 點能量。"}],
                "sources": [{"source_id": "fixture-item-wiki", "locator": "fixture://item-wiki"}],
            },
        ),
    )


def _run_ollama_command(args: argparse.Namespace) -> int:
    try:
        client = OllamaModelClient(
            endpoint=args.ollama_endpoint,
            model=getattr(args, "ollama_model", None),
            timeout=args.ollama_timeout,
        )
        installed = client.list_models()
        if args.command == "models":
            print(json.dumps({"models": installed}, ensure_ascii=False, indent=2))
            return 0

        if not client.model:
            raise OllamaModelError("model_not_configured", "No Ollama model is configured.")
        if client.model not in {str(item["name"]) for item in installed}:
            raise OllamaModelError(
                "model_not_installed",
                "The configured model is not installed; this command never downloads models.",
            )
        answer = client.generate(_verification_prompt())
        if "30" not in answer and "三十" not in answer:
            print(
                json.dumps(
                    {
                        "status": "verification_failed",
                        "endpoint": client.endpoint,
                        "model": client.model,
                        "answer": answer,
                        "expected_material": "30 points of energy",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        print(
            json.dumps(
                {
                    "status": "verified",
                    "endpoint": client.endpoint,
                    "model": client.model,
                    "question": "測試資料中的月光藥水回復多少點能量？",
                    "answer": answer,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (OllamaModelError, ValueError) as exc:
        diagnostic = getattr(exc, "safe_diagnostic", None)
        if not isinstance(diagnostic, dict):
            diagnostic = {"code": "invalid_configuration", "detail": str(exc)}
        print(json.dumps({"error": diagnostic}, ensure_ascii=False), file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"models", "verify-ollama"}:
        return _run_ollama_command(args)

    model = None
    if args.model_adapter == "ollama":
        try:
            model = OllamaModelClient(
                endpoint=args.ollama_endpoint,
                model=args.ollama_model,
                timeout=args.ollama_timeout,
            )
        except ValueError as exc:
            print(
                json.dumps(
                    {"error": {"code": "invalid_configuration", "detail": str(exc)}},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
    runtime = build_runtime(
        args.state_dir,
        fixture_dir=args.fixture_dir,
        model=model,
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
