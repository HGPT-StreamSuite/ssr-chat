from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence

from .analysis import TriggerDetector
from .api import serve
from .application import build_runtime
from .catalog_knowledge import CatalogKnowledgeReader
from .models import AnswerPrompt
from .ollama_model import OllamaModelClient, OllamaModelError


_VERIFICATION_CLAIM_PATTERNS = (
    re.compile(
        r"(?<!\d)30(?!\d)\s*(?:(?:points?|pts?)\s*(?:of\s*)?)?"
        r"(?:energy|stamina)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:energy|stamina)[^.!?;，,。；！？\n]{0,24}"
        r"(?<!\d)30(?!\d)(?:\s*points?)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\d零〇一二兩两三四五六七八九十百千萬万億亿兆])"
        r"(?:30|三十)"
        r"(?![\d零〇一二兩两三四五六七八九十百千萬万億亿兆])"
        r"\s*(?:點|点)\s*(?:的\s*)?(?:能量|體力|体力)"
    ),
    re.compile(
        r"(?:能量|體力|体力)[^.!?;，,。；！？\n]{0,16}"
        r"(?<![\d零〇一二兩两三四五六七八九十百千萬万億亿兆])"
        r"(?:30|三十)"
        r"(?![\d零〇一二兩两三四五六七八九十百千萬万億亿兆])"
        r"\s*(?:點|点)"
    ),
)
_VERIFICATION_NEGATION = re.compile(
    r"\b(?:no|not|never|cannot|can't|doesn't|does not|isn't|is not|won't|"
    r"without|false|incorrect)\b"
    r"|不是|並非|并非|不能|不會|不会|無法|无法|未能|不正確|不正确|"
    r"錯誤|错误|否定|不(?!只|僅|仅|過|过|但|錯|错)|"
    r"沒(?!錯|错)|没(?!錯|错)|無(?!論|论)|无(?!論|论)|未曾|未達|未回復|未恢復"
)


def _has_positive_verification_claim(answer: str) -> bool:
    """Require an affirmative claim of exactly 30 energy/stamina points."""
    if not isinstance(answer, str) or not answer.strip():
        return False
    for clause in re.split(r"[.!?;，,。；！？\n]+", answer.lower()):
        if not any(pattern.search(clause) for pattern in _VERIFICATION_CLAIM_PATTERNS):
            continue
        # "not only 30" and "不只是 30" affirm the amount; remove those
        # contrastive forms before checking the remaining clause for denial.
        clause = re.sub(r"\bnot\s+(?:only|just)\b", "", clause)
        clause = (
            clause.replace("不只是", "")
            .replace("不僅", "")
            .replace("不仅", "")
            .replace("不過", "")
            .replace("不过", "")
            .replace("不錯", "")
            .replace("不错", "")
            .replace("沒錯", "")
            .replace("没错", "")
            .replace("無論", "")
            .replace("无论", "")
        )
        if not _VERIFICATION_NEGATION.search(clause):
            return True
    return False


def _default_fixtures() -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ssr-chat")
    sub = parser.add_subparsers(dest="command", required=True)
    live = sub.add_parser("twitch-live", help="Opt-in authenticated Twitch receive and local dry-run; use twitch-live --help")
    live.add_argument("live_args", nargs=argparse.REMAINDER)

    chat = sub.add_parser("chat", help="處理一個本機 JSON 事件")
    chat.add_argument("--input", required=True, help="JSON 檔路徑，或 - 表示 stdin")
    chat.add_argument("--state-dir", default=".local-state")
    chat.add_argument("--fixture-dir", default=str(_default_fixtures()))
    _add_model_options(chat)
    _add_knowledge_options(chat)

    api = sub.add_parser("serve", help="啟動僅綁 localhost 的 HTTP API")
    api.add_argument("--port", type=int, default=8765)
    api.add_argument("--state-dir", default=".local-state")
    api.add_argument("--lifecycle-dir", help="New local directory for generation-bound request drain/status control; opt-in only")
    api.add_argument("--fixture-dir", default=str(_default_fixtures()))
    _add_model_options(api)
    _add_knowledge_options(api)
    api.add_argument("--retrieval-pipeline-config", help="Opt-in trusted local modular retrieval pipeline")
    api.add_argument("--retrieval-template", help="Server-owned request/trusted_context template; never accepted over HTTP")
    api.add_argument("--retrieval-routes-config", help="Trusted file mapping named /retrieve/... endpoints to domain-bound local configurations")
    api.add_argument("--answer-routes-config", help="Opt-in synthetic evidence answer routes with exact AI-use policy; requires explicit local Ollama")
    api.add_argument("--entry-routes-config", help="Opt-in /query explicit commands mapped to existing synthetic domain-bound routes")
    api.add_argument("--twitch-replay-config", help="Opt-in synthetic EventSub replay only; requires exact server-owned entry context binding; never connects to Twitch")
    api.add_argument("--twitch-archive-config", help="Opt-in synthetic EventSub archive only; explicit fixture clock/content receipts and isolated task-20/task-7 binding; no query or model")

    models = sub.add_parser("models", help="List models already installed in local Ollama")
    _add_ollama_connection_options(models)

    verify = sub.add_parser(
        "verify-ollama", help="Run a fixed synthetic question against an installed Ollama model"
    )
    _add_ollama_connection_options(verify)
    verify.add_argument("--ollama-model", required=True)

    retrieve = sub.add_parser("retrieve", help="明確配置的本機 QueryPlan → dispatcher → evidence collector；不生成回答")
    retrieve.add_argument("--request", required=True, help="含 request/trusted_context 的 JSON，或 - 讀 stdin")
    retrieve.add_argument("--pipeline-config", required=True, help="可信本機元件目錄及 dispatcher 設定")
    retrieve.add_argument("--output", default="-", help="新 JSON 檔或 -；不覆寫既有檔案")
    ingest = sub.add_parser("ingest-chat", help="Synthetic local Record -> task-20 normalizer -> task-7 isolated store; no Twitch or AI")
    ingest.add_argument("--config", required=True, help="Trusted synthetic pipeline configuration")
    ingest.add_argument("--state-dir", required=True, help="Dedicated child directory of the configured state root")
    ingest.add_argument("--input", required=True, help="Synthetic batch JSON path or - for stdin")
    stt = sub.add_parser("ingest-stt", help="Synthetic STT text -> task-21 -> task-8 isolated archive; no audio or AI")
    stt.add_argument("--config", required=True, help="Trusted synthetic STT policies, binding and local component paths")
    stt.add_argument("--state-dir", required=True, help="Dedicated child directory of the configured synthetic state root")
    stt.add_argument("--input", required=True, help="Synthetic STT Record batch JSON path or - for stdin")
    game = sub.add_parser("ingest-game", help="Registered synthetic local snapshot -> task-22 handoff -> task-5 isolated store")
    game.add_argument("--config", required=True, help="Trusted synthetic snapshot registry and independent store policy")
    game.add_argument("--state-dir", required=True, help="Dedicated child directory of configured synthetic state root")
    game.add_argument("--input", required=True, help="JSON batch of registered snapshot aliases, or - for stdin")
    channel = sub.add_parser("ingest-channel", help="Registered synthetic channel export -> task-19 Evidence -> original Record to task-4")
    channel.add_argument("--config", required=True, help="Trusted synthetic export registry and independent read/write policies")
    channel.add_argument("--state-dir", required=True, help="Dedicated child directory of configured synthetic state root")
    channel.add_argument("--input", required=True, help="JSON batch of registered export aliases, or - for stdin")
    activity = sub.add_parser("count-activity", help="Read a pinned synthetic chat snapshot through task-7 and compute explicit speech counts with task-18")
    activity.add_argument("--config", required=True, help="Trusted snapshot, source reader, purpose grants and explicit speech roster")
    activity.add_argument("--state-dir", required=True, help="Dedicated synthetic counts cache directory")
    activity.add_argument("--input", required=True, help="Structured synthetic speech-count request JSON path or - for stdin")
    draft = sub.add_parser("draft-transcript", help="Pinned synthetic task-8 history -> task-9 whitespace-only cleanup drafts; no model or publication")
    draft.add_argument("--config", required=True, help="Trusted source snapshot, reader/draft policies, and registered complete raw Records")
    draft.add_argument("--state-dir", required=True, help="Dedicated child directory for isolated draft files")
    draft.add_argument("--input", required=True, help="Structured synthetic cleanup request JSON path or - for stdin")
    knowledge = sub.add_parser("draft-knowledge", help="Pinned synthetic channel current view -> marked quote drafts with native task-23 and host policy")
    knowledge.add_argument("--config", required=True, help="Trusted exact source, native reader, purpose, author, hook and caller-mark configuration")
    knowledge.add_argument("--state-dir", required=True, help="Dedicated child directory for isolated knowledge draft files")
    knowledge.add_argument("--input", required=True, help="Synthetic request JSON path or - for stdin")
    events = sub.add_parser("channel-events", help="Explicit synthetic relationship/visit declarations to isolated native task-26 ingest/get/export")
    events.add_argument("--config", required=True, help="Trusted source binding, current caller/source policies and declaration/query registry")
    events.add_argument("--state-dir", required=True, help="Dedicated child directory for the synthetic ledger and authorized handoffs")
    events.add_argument("--input", required=True, help="Structured synthetic request JSON path or - for stdin")
    identity = sub.add_parser("identity-records", help="Declared synthetic raw person/account records through native task-6; no links or name inference")
    identity.add_argument("--config", required=True, help="Trusted synthetic raw annotations, source visibility, caller/use policies and query registry")
    identity.add_argument("--state-dir", required=True, help="Dedicated child directory for an independent identity raw store")
    identity.add_argument("--input", required=True, help="Structured synthetic request JSON path or - for stdin")
    return parser


def _add_knowledge_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--knowledge-adapter", choices=("fixture", "catalog"), default="fixture")
    parser.add_argument("--knowledge-repo", help="Existing ssr-knowledge checkout; no cross-repo imports")
    parser.add_argument("--knowledge-python", help="Existing Python executable for the knowledge CLI")
    parser.add_argument("--knowledge-data-dir", help="Existing catalog directory outside Git checkouts")
    parser.add_argument("--knowledge-timeout", type=float, default=15.0)


def _knowledge_reader(args: argparse.Namespace):
    paths = (args.knowledge_repo, args.knowledge_python, args.knowledge_data_dir)
    if args.knowledge_adapter == "fixture":
        if any(paths):
            raise ValueError("knowledge paths require --knowledge-adapter catalog")
        return None
    if not all(paths):
        raise ValueError("catalog requires --knowledge-repo, --knowledge-python and --knowledge-data-dir")
    return CatalogKnowledgeReader(*paths, timeout=args.knowledge_timeout)


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
        if not _has_positive_verification_claim(answer):
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
    # Keep the authenticated receiver's account/scope gates in its own parser.
    # Delegating before parse_args also preserves its normal --help behavior.
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "twitch-live":
        from .twitch_live import main as live_main
        return live_main(arguments[1:])
    args = build_parser().parse_args(argv)
    if args.command == "identity-records":
        from .identity_records import IdentityRecordGateway
        from .modular_retrieval import load_object
        try:
            gateway = IdentityRecordGateway(args.config, args.state_dir)
            if args.input == "-":
                raw = sys.stdin.buffer.read(1_000_001)
            else:
                with Path(args.input).open("rb") as stream:
                    raw = stream.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("input_too_large")
            result = gateway.run(load_object(raw))
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return 0 if result["status"] in {"complete", "empty", "ok", "no_match"} else 1
        except (ValueError, OSError, TypeError, KeyError, AttributeError) as error:
            print(json.dumps({"status":"held","reason_code":getattr(error,"code","identity_records_input_or_configuration_invalid"),
                              "model_invoked":False,"network_used":False,"public_output":"not_attempted"}))
            return 2
    if args.command == "channel-events":
        from .channel_events import ChannelEventGateway
        from .modular_retrieval import load_object
        try:
            gateway = ChannelEventGateway(args.config, args.state_dir)
            if args.input == "-":
                raw = sys.stdin.buffer.read(1_000_001)
            else:
                with Path(args.input).open("rb") as stream:
                    raw = stream.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("input_too_large")
            result = gateway.run(load_object(raw))
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return 0 if result["status"] in {"complete", "empty", "ok", "no_match"} else 1
        except (ValueError, OSError, TypeError, KeyError, AttributeError) as error:
            print(json.dumps({"status":"held","reason_code":getattr(error,"code","channel_events_input_or_configuration_invalid"),
                              "model_invoked":False,"network_used":False,"public_output":"not_attempted"}))
            return 2
    if args.command == "draft-knowledge":
        from .knowledge_drafts import KnowledgeDraftGateway
        from .modular_retrieval import load_object
        try:
            gateway = KnowledgeDraftGateway(args.config, args.state_dir)
            if args.input == "-":
                raw = sys.stdin.buffer.read(1_000_001)
            else:
                with Path(args.input).open("rb") as stream:
                    raw = stream.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("input_too_large")
            result = gateway.draft(load_object(raw))
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return 0 if result["status"] in {"drafted", "duplicate", "no_match"} else 1
        except (ValueError, OSError, TypeError, KeyError, AttributeError) as error:
            print(json.dumps({"status":"held","draft_count":None,"reason_code":getattr(error,"code","knowledge_drafts_input_or_configuration_invalid"),
                              "model_invoked":False,"network_used":False,"public_output":"not_attempted"}))
            return 2
    if args.command == "draft-transcript":
        from .transcript_drafts import TranscriptDraftGateway
        from .modular_retrieval import load_object
        try:
            gateway = TranscriptDraftGateway(args.config, args.state_dir)
            if args.input == "-":
                raw = sys.stdin.buffer.read(1_000_001)
            else:
                with Path(args.input).open("rb") as stream:
                    raw = stream.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("input_too_large")
            result = gateway.draft(load_object(raw))
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return 0 if result["status"] in {"drafted", "no_match"} else 1
        except (ValueError, OSError, TypeError, KeyError, AttributeError) as error:
            print(json.dumps({"status":"held","draft_count":None,"reason_code":getattr(error,"code","transcript_drafts_input_or_configuration_invalid"),
                              "model_invoked":False,"network_used":False,"audio_used":False,"public_output":"not_attempted"}))
            return 2
    if args.command == "count-activity":
        from .activity_counts import ActivityCountsGateway
        from .modular_retrieval import load_object
        try:
            gateway = ActivityCountsGateway(args.config, args.state_dir)
            if args.input == "-":
                raw = sys.stdin.buffer.read(1_000_001)
            else:
                with Path(args.input).open("rb") as stream:
                    raw = stream.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("input_too_large")
            result = gateway.count(load_object(raw))
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return 0 if result["status"] == "counted" else 1
        except (ValueError, OSError, TypeError, KeyError, AttributeError) as error:
            print(json.dumps({"status":"held","message_count":None,"reason_code":getattr(error,"code","activity_counts_input_or_configuration_invalid"),
                              "model_invoked":False,"network_used":False,"public_output":"not_attempted"}))
            return 2
    if args.command == "ingest-channel":
        from .channel_ingest import ChannelIngestPipeline
        from .modular_retrieval import load_object
        try:
            pipeline = ChannelIngestPipeline(args.config, args.state_dir)
            if args.input == "-":
                raw = sys.stdin.buffer.read(1_000_001)
            else:
                with Path(args.input).open("rb") as stream:
                    raw = stream.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("input_too_large")
            result = pipeline.ingest(load_object(raw))
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return 0 if result["status"] in {"complete", "empty"} else 1
        except (ValueError, OSError, TypeError, KeyError, AttributeError) as error:
            print(json.dumps({"status":"held","reason_code":getattr(error,"code","channel_ingest_input_or_configuration_invalid"),
                              "model_invoked":False,"network_used":False,"public_output":"not_attempted"}))
            return 2
    if args.command == "ingest-game":
        from .game_ingest import GameIngestPipeline
        from .modular_retrieval import load_object
        try:
            pipeline = GameIngestPipeline(args.config, args.state_dir)
            if args.input == "-":
                raw = sys.stdin.buffer.read(1_000_001)
            else:
                with Path(args.input).open("rb") as stream:
                    raw = stream.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("input_too_large")
            result = pipeline.ingest(load_object(raw))
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return 0 if result["status"] in {"complete", "empty"} else 1
        except (ValueError, OSError, TypeError, KeyError, AttributeError) as error:
            print(json.dumps({"status":"held","reason_code":getattr(error,"code","game_ingest_input_or_configuration_invalid"),
                              "model_invoked":False,"network_used":False,"public_output":"not_attempted"}))
            return 2
    if args.command == "ingest-stt":
        from .stt_ingest import SttIngestPipeline
        from .modular_retrieval import load_object
        try:
            pipeline = SttIngestPipeline(args.config, args.state_dir)
            if args.input == "-":
                raw = sys.stdin.buffer.read(1_000_001)
            else:
                with Path(args.input).open("rb") as stream:
                    raw = stream.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("input_too_large")
            result = pipeline.ingest(load_object(raw))
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return 0 if result["status"] in {"complete", "empty"} else 1
        except (ValueError, OSError, TypeError, KeyError) as error:
            print(json.dumps({"status":"held","reason_code":getattr(error,"code","stt_ingest_input_or_configuration_invalid"),
                              "model_invoked":False,"audio_used":False,"public_output":"not_attempted"}))
            return 2
    if args.command == "ingest-chat":
        from .chat_ingest import ChatIngestPipeline
        from .modular_retrieval import load_object
        try:
            pipeline = ChatIngestPipeline(args.config, args.state_dir)
            raw = sys.stdin.buffer.read(1_000_001) if args.input == "-" else Path(args.input).read_bytes()
            if len(raw) > 1_000_000:
                raise ValueError("input_too_large")
            result = pipeline.ingest(load_object(raw))
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return 0 if result["status"] in {"complete", "empty"} else 1
        except (ValueError, OSError, TypeError, KeyError) as error:
            print(json.dumps({"status":"held","reason_code":getattr(error,"code","chat_ingest_input_or_configuration_invalid"),
                              "model_invoked":False,"public_output":"not_attempted"}))
            return 2
    if args.command == "retrieve":
        from .modular_retrieval import ModularRetrievalPipeline, PipelineConfig, PipelineError, load_object
        try:
            if args.output != "-" and Path(args.output).exists():
                raise PipelineError("output_exists", "output")
            config = PipelineConfig.read(args.pipeline_config)
            raw = sys.stdin.buffer.read(2_000_001) if args.request == "-" else Path(args.request).read_bytes()
            report = ModularRetrievalPipeline(config).run(load_object(raw))
            rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            if args.output == "-":
                print(rendered, end="")
            else:
                with Path(args.output).open("x", encoding="utf-8", newline="\n") as output:
                    output.write(rendered)
            return 0 if report["status"] == "complete" else 1
        except (PipelineError, OSError, ValueError) as exc:
            print(json.dumps({"status": "error", "stage": getattr(exc, "stage", "input"),
                              "code": getattr(exc, "code", "input_or_output_error")}), file=sys.stderr)
            return 2
    if args.command in {"models", "verify-ollama"}:
        return _run_ollama_command(args)

    retrieval_gateway = None
    retrieval_routes = {}
    if args.command == "serve":
        from .modular_api import ModularRetrievalGateway, load_retrieval_routes
        from .modular_retrieval import PipelineError
        paths = (args.retrieval_pipeline_config, args.retrieval_template)
        if any(paths):
            try:
                if not all(paths):
                    raise PipelineError("retrieval_pipeline_and_template_required")
                retrieval_gateway = ModularRetrievalGateway.from_files(*paths)
            except (PipelineError, OSError, ValueError) as exc:
                print(json.dumps({"error": {"code": getattr(exc, "code", "retrieval_configuration_invalid"),
                                           "detail": "Local retrieval startup configuration is invalid."}}), file=sys.stderr)
                return 2
        if args.retrieval_routes_config:
            try:
                retrieval_routes = load_retrieval_routes(args.retrieval_routes_config)
            except (PipelineError, OSError, ValueError):
                print(json.dumps({"error": {"code": "retrieval_routes_configuration_invalid",
                                           "detail": "Local retrieval route configuration is invalid."}}), file=sys.stderr)
                return 2

    try:
        knowledge_reader = _knowledge_reader(args)
    except ValueError as exc:
        print(json.dumps({"error": {"code": "invalid_configuration", "detail": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 2
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
        knowledge_reader=knowledge_reader,
        model=model,
    )
    if args.command == "serve":
        if not 1 <= args.port <= 65535:
            print("port must be between 1 and 65535", file=sys.stderr)
            return 2
        answer_routes = {}
        if args.answer_routes_config:
            from .modular_answer import load_answer_routes
            from .stores import JsonlLog
            try:
                if model is None or not model.model:
                    raise ValueError("explicit_local_model_required")
                answer_routes = load_answer_routes(args.answer_routes_config, retrieval_routes, model,
                    JsonlLog(Path(args.state_dir) / "modular_answer_traces.jsonl"))
            except (ValueError, OSError):
                print(json.dumps({"error": {"code": "answer_routes_configuration_invalid",
                    "detail": "Synthetic answer routes require a valid policy and explicit local Ollama model."}}), file=sys.stderr)
                return 2
        entry_gateway = None
        if args.entry_routes_config:
            from .modular_entry import EntryGateway
            from .stores import JsonlLog
            try:
                entry_gateway = EntryGateway.read(args.entry_routes_config, retrieval_routes, answer_routes,
                    JsonlLog(Path(args.state_dir) / "modular_entry_traces.jsonl"))
            except (ValueError, OSError):
                print(json.dumps({"error": {"code": "entry_routes_configuration_invalid",
                    "detail": "Entry commands require existing synthetic domain-bound retrieval or answer routes."}}), file=sys.stderr)
                return 2
        twitch_replay_gateway = None
        if args.twitch_replay_config:
            import sqlite3
            from .twitch_replay import TwitchReplayBridge
            from .stores import JsonlLog
            try:
                twitch_replay_gateway = TwitchReplayBridge.read(args.twitch_replay_config, entry_gateway, args.state_dir,
                    JsonlLog(Path(args.state_dir) / "twitch_replay_traces.jsonl"))
            except (ValueError, OSError, sqlite3.Error):
                print(json.dumps({"error": {"code": "twitch_replay_configuration_invalid",
                    "detail": "Synthetic replay requires an exact existing entry binding and its own persistent ledger."}}), file=sys.stderr)
                return 2
        twitch_archive_gateway = None
        if args.twitch_archive_config:
            from .twitch_archive import TwitchArchiveGateway
            try:
                twitch_archive_gateway = TwitchArchiveGateway.read(args.twitch_archive_config)
            except (ValueError, OSError, TypeError, KeyError):
                print(json.dumps({"error":{"code":"twitch_archive_configuration_invalid",
                    "detail":"Synthetic archiving requires explicit source/storage mapping and authored fixture receipts."}}), file=sys.stderr)
                return 2
        serve(runtime, args.port, retrieval_gateway=retrieval_gateway, retrieval_routes=retrieval_routes, answer_routes=answer_routes,
              entry_gateway=entry_gateway, twitch_replay_gateway=twitch_replay_gateway, twitch_archive_gateway=twitch_archive_gateway,
              lifecycle_dir=args.lifecycle_dir)
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
    if result.get("status") in {"failed", "delivery_failed"}:
        return 1
    return 0
