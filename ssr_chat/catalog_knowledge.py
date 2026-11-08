from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from .models import QueryResult


class CatalogReaderError(RuntimeError):
    """A bounded diagnostic; never includes child stderr or catalog contents."""


_QUOTED = re.compile(r'「([^「」]+)」|“([^“”]+)”|"([^"\n]+)"')
_FOLLOWUP = re.compile(r"^(?:那|它|這個|這項|這件|該物品|it\b|its\b|this\b)", re.I)
_MULTIPLE_SUBJECTS = re.compile(r"[，,；;。！？?!、\r\n]|以及|還有|和|與")
_EFFECT_QUESTION_SUFFIXES = ("是什麼效果", "有什麼效果", "的效果是什麼")


def _quoted_name(text: str) -> str | None:
    matches = list(_QUOTED.finditer(text))
    if len(matches) > 1:
        raise CatalogReaderError("catalog_multiple_names: ask about one quoted name at a time")
    if not matches:
        return None
    return next(value.strip() for value in matches[0].groups() if value is not None)


def _effect_question_name(text: str) -> str | None:
    """Recognize three explicit single-name question forms, without guessing."""
    for suffix in _EFFECT_QUESTION_SUFFIXES:
        if text.endswith(suffix):
            name = text[:-len(suffix)].strip()
            if name and not _MULTIPLE_SUBJECTS.search(name) and not _FOLLOWUP.fullmatch(name):
                return name
    return None


def catalog_query_text(question: str, context_messages: tuple[dict[str, str], ...]) -> str:
    """Explicit names, bounded effect questions, or a single-hop quoted referent.

    No model extraction, broad keyword guessing, or catalog-derived name cache.
    """
    text = question.strip()
    name = _quoted_name(text)
    if name is None:
        text = text.rstrip("?？!！。 ")
        name = _effect_question_name(text)
    if name is None and _FOLLOWUP.match(text):
        # Only the immediately preceding triggered question may establish a
        # referent. An intervening new question must not resurrect an older name.
        for message in reversed(context_messages):
            previous = message["text"].strip()
            if previous.startswith("!ask"):
                name = _quoted_name(previous)
                break
    text = name if name is not None else text
    if not text or len(text) > 200:
        raise CatalogReaderError("catalog_invalid_query: use one name of 1 to 200 characters")
    return text


class CatalogKnowledgeReader:
    """Opt-in subprocess boundary to ssr-knowledge's existing published CLI.

    Never imports another repository, opens SQLite directly, enables preview,
    creates data directories, installs packages, or falls back to fixtures.
    """

    def __init__(self, repo_dir: str | Path, python: str | Path,
                 data_dir: str | Path, *, timeout: float = 15.0) -> None:
        self.repo_dir = Path(repo_dir).expanduser().resolve()
        self.python = Path(python).expanduser().resolve()
        self.data_dir = Path(data_dir).expanduser().resolve()
        if not (self.repo_dir / "ssr_knowledge" / "__main__.py").is_file():
            raise ValueError("knowledge repo must contain ssr_knowledge/__main__.py")
        if not self.python.is_file():
            raise ValueError("knowledge Python executable must already exist")
        if not (self.data_dir / "knowledge.sqlite3").is_file():
            raise ValueError("knowledge data directory must contain an existing knowledge.sqlite3")
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 120:
            raise ValueError("knowledge timeout must be finite and between 0 and 120 seconds")
        self.timeout = timeout

    def _search(self, text: str) -> dict[str, Any]:
        # --text= prevents a user-supplied name from becoming a CLI option.
        command = [str(self.python), "-B", "-X", "utf8", "-m", "ssr_knowledge",
                   "catalog", "--data-dir", str(self.data_dir), "search",
                   "--text=" + text, "--names-only", "--limit", "2"]
        allowed_env = {"systemroot", "windir", "systemdrive", "comspec", "path",
                       "pathext", "temp", "tmp", "userprofile", "localappdata",
                       "appdata", "programfiles", "programdata"}
        env = {key: value for key, value in os.environ.items() if key.lower() in allowed_env}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        try:
            result = subprocess.run(command, cwd=self.repo_dir, env=env,
                                    stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=self.timeout, check=False,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise CatalogReaderError("catalog_timeout: published lookup timed out") from exc
        except OSError as exc:
            raise CatalogReaderError("catalog_unavailable: published lookup could not start") from exc
        if result.returncode:
            raise CatalogReaderError("catalog_failed: published lookup exited unsuccessfully")
        if len(result.stdout) > 2_000_000:
            raise CatalogReaderError("catalog_invalid_response: response exceeds size limit")
        try:
            value = json.loads(result.stdout.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise CatalogReaderError("catalog_invalid_response: expected UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise CatalogReaderError("catalog_invalid_response: expected an object")
        return value

    def query(self, purpose: str, question: str,
              context_messages: tuple[dict[str, str], ...]) -> QueryResult:
        text = catalog_query_text(question, context_messages)
        result = self._search(text)
        matches = result.get("matches")
        if (result.get("scope") != "published" or result.get("query") != text
                or result.get("names_only") is not True or not isinstance(matches, list)
                or result.get("status") not in {"ok", "no_data"}
                or (result["status"] == "no_data") != (not matches)):
            raise CatalogReaderError("catalog_invalid_response: published search contract mismatch")
        if result.get("truncated") or len(matches) > 1:
            return QueryResult(purpose, "tool_error", diagnostic="catalog_ambiguous_name")
        if not matches:
            return QueryResult(purpose, "no_data")
        match = matches[0]
        if (not isinstance(match, dict) or match.get("status") != "published"
                or match.get("stale") is not False or match.get("match_type") != "exact_name"
                or not isinstance(match.get("entry"), dict)
                or not isinstance(match.get("sources"), list) or not match["sources"]):
            raise CatalogReaderError("catalog_invalid_response: invalid published match")
        # search_index permission alone is insufficient for model input. Mixed
        # source entries cannot safely be split, so exclude the entire entry.
        for source in match["sources"]:
            if not isinstance(source, dict):
                raise CatalogReaderError("catalog_invalid_response: invalid source")
            provenance = source.get("provenance", {})
            if any(provenance.get(scope, {}).get("ai_input", {}).get("status") != "allowed"
                   for scope in ("snapshot_usage", "current_usage")):
                return QueryResult(purpose, "tool_error", diagnostic="catalog_ai_input_not_allowed")
        entry = match["entry"]
        # Project answer material only; omit editor/reviewer identities, raw
        # snapshots, source configuration, and private evidence quotes/anchors.
        keys = ("entry_id", "game", "category", "name_en", "name_zh", "aliases",
                "summary_zh", "game_version", "limitations", "item_details",
                "badge_details", "guide_details", "knowledge_details")
        material = {key: entry[key] for key in keys if key in entry}
        material.update(text=entry.get("summary_zh", ""), revision_id=match["revision_id"],
                        publication_status="published", payload_sha256=match["payload_sha256"])
        citations = entry.get("citations", [])
        sources = tuple({
            "source_id": source["source_id"], "snapshot_id": source["snapshot_id"],
            "locator": source["provenance"].get("original_url", source["source"].get("url")),
            "title": source["source"].get("title"),
            "attribution": source["source"].get("policy", {}).get("attribution"),
            "license": source["source"].get("policy", {}).get("license"),
            "citations": [c for c in citations if c.get("snapshot_id") == source["snapshot_id"]],
        } for source in match["sources"])
        if len(json.dumps([material, sources], ensure_ascii=False)) > 16000:
            return QueryResult(purpose, "tool_error", diagnostic="catalog_material_too_large")
        return QueryResult(purpose, "ok", materials=(material,), sources=sources)
