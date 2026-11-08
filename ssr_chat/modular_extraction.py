"""Scope-bound registered topics in two explicit, non-generative grammars."""
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import re
import unicodedata

from .modular_retrieval import PipelineError

SCOPE_FIELDS = {"domain", "platform", "channel_id", "game_id", "session_id", "content_version"}
RULES = {
    "/game": ("registered_gameplay_records_v1", "game_knowledge", r"請找出[ \t]+", r"[ \t]+gameplay[ \t]+records[?？]?", r"請找出[ \t]+(?P<whole>.{1,240})[ \t]+records[?？]?"),
    "/channel": ("registered_channel_announcement_v1", "channel_knowledge", r"請問本頻道公告[ \t]+", r"[ \t]+幾點[?？]?", r"請問本頻道(?P<whole>公告[ \t]+.{1,240})[ \t]+幾點[?？]?"),
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()


def key(value): return re.sub(r"[ \t]+", " ", unicodedata.normalize("NFKC", value)).casefold()


@dataclass(frozen=True)
class ExtractedQuery:
    original_text: str
    search_text: str
    span_start: int
    span_end: int
    entity_id: str
    entity_name: str
    rule: str
    scope_sha256: str
    registry_sha256: str

    def as_dict(self): return asdict(self)


def validate_extraction(value, original_text, scope):
    try:
        if (not isinstance(value, ExtractedQuery) or value.original_text != original_text
                or not isinstance(scope, dict) or set(scope) != SCOPE_FIELDS or value.scope_sha256 != digest(scope)
                or not any(value.rule == rule[0] and scope["domain"] == rule[1] for rule in RULES.values())
                or type(value.span_start) is not int or type(value.span_end) is not int
                or not 0 <= value.span_start < value.span_end <= len(original_text)
                or not isinstance(value.search_text, str) or not value.search_text.strip() or len(value.search_text) > 120
                or original_text[value.span_start:value.span_end] != value.search_text
                or key(value.search_text) != key(value.entity_name)
                or not isinstance(value.entity_id, str) or not value.entity_id.strip()
                or not isinstance(value.registry_sha256, str) or re.fullmatch("[0-9a-f]{64}", value.registry_sha256) is None):
            raise ValueError("binding_invalid")
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise PipelineError("query_extraction_binding_invalid", "input") from error


class EntityQueryExtractor:
    def __init__(self, config, targets):
        if (not isinstance(config, dict) or set(config) != {"schema_version", "routes"} or config["schema_version"] != "1.0.1"
                or not isinstance(config["routes"], dict) or not config["routes"] or set(config["routes"]) - set(RULES)):
            raise PipelineError("query_extraction_configuration_invalid")
        self.rules = {}
        for command, value in config["routes"].items():
            if command not in targets or not isinstance(value, dict) or set(value) != {"rule", "entities"}:
                raise PipelineError("query_extraction_configuration_invalid")
            rule, domain, prefix, suffix, whole = RULES[command]
            _, target_domain, operation, gateway = targets[command]
            provider = gateway if operation == "retrieve" else gateway.retrieval
            scope = provider.trusted_answer_context["scope"]
            if (value["rule"] != rule or target_domain != domain or not isinstance(scope, dict) or set(scope) != SCOPE_FIELDS
                    or scope["domain"] != domain or not isinstance(value["entities"], list) or not 1 <= len(value["entities"]) <= 50):
                raise PipelineError("query_extraction_configuration_invalid")
            ids, names, compiled = set(), {}, []
            for entity in value["entities"]:
                if (not isinstance(entity, dict) or set(entity) != {"id", "name"}
                        or not isinstance(entity["id"], str) or not entity["id"].strip() or len(entity["id"]) > 80
                        or not isinstance(entity["name"], str) or not 1 <= len(entity["name"]) <= 120 or entity["name"].strip() != entity["name"]
                        or any(ord(char) < 32 for char in entity["name"]) or "/" in entity["name"]
                        or entity["id"] in ids or key(entity["name"]) in names):
                    raise PipelineError("query_extraction_registry_invalid")
                ids.add(entity["id"]); names[key(entity["name"])] = entity["id"]
                pattern = re.compile(prefix + "(?P<topic>" + re.escape(entity["name"]) + ")" + suffix, re.I | re.ASCII)
                compiled.append((deepcopy(entity), pattern))
            self.rules[command] = (rule, digest(scope), digest({"rule": rule, "scope": scope, "entities": value["entities"]}),
                                   tuple(compiled), names, re.compile(whole, re.I | re.ASCII))

    def handles(self, command): return command in self.rules

    def extract(self, command, text):
        rule, scope_hash, registry_hash, patterns, names, whole = self.rules[command]
        base = {"status": "held", "rule": rule, "registry_sha256": registry_hash}
        matches = [(entity, pattern.fullmatch(text)) for entity, pattern in patterns]
        matches = [(entity, match) for entity, match in matches if match is not None]
        if not matches:
            return None, {**base, "reason_code": "query_extraction_unresolved"}
        if len(matches) != 1:
            return None, {**base, "reason_code": "query_extraction_ambiguous"}
        entity, match = matches[0]
        raw = whole.fullmatch(text)
        alternative = names.get(key(raw.group("whole").strip())) if raw else None
        if alternative is not None and alternative != entity["id"]:
            return None, {**base, "reason_code": "query_extraction_ambiguous"}
        start, stop = match.span("topic")
        record = ExtractedQuery(text, text[start:stop], start, stop, entity["id"], entity["name"], rule, scope_hash, registry_hash)
        return record, {"status": "selected", **record.as_dict()}
