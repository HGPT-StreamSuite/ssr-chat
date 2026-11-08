"""Fixed, host-only synthetic chat-store full Record readback.

Only the provider's existing RecordReader101 CLI is executed, against a fresh
byte copy of its registered quiescent synthetic DB. This is not a chat-search
adapter: source metadata and AI purpose authority are unavailable, so reader
execution is explicitly held. No database client, network, model or fallback.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = REPO.parent.parent
PROVIDER = WORKSPACE / "repos/components/hgpt-chat-store"
HOST_PROFILE = REPO / "contracts/chat_store_raw_record.synthetic.v1.json"
PROFILE = "chat_store_raw_record_readback_synthetic_v1"
FORMAT = "hgpt-chat-store.record-get/1"
EXIT_CODES = {"ok": 0, "no_match": 0, "missing_input": 2, "not_configured": 2, "error": 2, "denied": 3}
PROFILE_FIELDS = {"schema_version", "profile", "synthetic", "provider_checkout", "caller_profile", "caller_profile_sha256",
                  "producer_manifest", "producer_source_sha256", "final_manifest", "final_manifest_sha256",
                  "final_source_sha256", "database_sha256", "binding_sha256", "policy_sha256",
                  "reference_receipt_sha256", "record_sha256", "policy_override"}
QUERY_FIELDS = {"schema_version", "request_id", "text", "intent", "scope", "actor_account_id", "person_id",
                "time_from", "time_to", "exclude_event_ids", "limit", "layer", "conditions", "access_scope"}
SCOPE_FIELDS = {"domain", "platform", "channel_id", "game_id", "session_id", "content_version"}
RECORD_FIELDS = {"schema_version", "record_kind", "record_id", "source_id", "source_event_id", "revision", "event_time",
                 "received_at", "scope", "actor_account_id", "text", "quality_flags", "source_refs", "access_scope"}
RECEIPT_FIELDS = {"profile_format", "operation", "observed_at_utc", "binding", "request_id", "status", "reason_code",
                  "records", "record_sha256", "source_authorization", "native_result", "query_sha256",
                  "trusted_policy_sha256", "producer", "grants_created"}


class BindingError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise BindingError(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def digest(value):
    return sha(canonical(value))


def _pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def load(raw):
    require(len(raw) <= 2_000_000, "json_input_too_large")
    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(BindingError("nonfinite_json")))
    require(isinstance(value, dict), "json_object_required")
    return value


def read(path):
    return load(Path(path).read_bytes())


def write(path, value):
    with Path(path).open("xb") as stream:
        stream.write(canonical(value) + b"\n")


def write_bytes(path, raw):
    with Path(path).open("xb") as stream:
        stream.write(raw)


def now():
    return datetime.now(timezone.utc).isoformat()


def local(path, root=WORKSPACE):
    path = Path(path)
    require(path.is_absolute() and not str(path).startswith(("\\\\", "//")) and "://" not in str(path), "absolute_local_path_required")
    for part in (path, *path.parents):
        require(not part.is_symlink() and not getattr(part, "is_junction", lambda: False)(), "linked_path_refused")
    path = path.resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        raise BindingError("path_outside_allowed_root") from None
    return path


def provider_file(relative):
    require(isinstance(relative, str) and not Path(relative).is_absolute() and ".." not in Path(relative).parts,
            "provider_relative_path_required")
    return local(PROVIDER / relative, PROVIDER)


def manifest_map(manifest):
    require(isinstance(manifest.get("files_sha256"), dict) and manifest["files_sha256"], "source_manifest_invalid")
    return {name: sha(provider_file(name).read_bytes()) for name in sorted(manifest["files_sha256"])}


def source_files():
    """Fixed source files only; never Git, state, a service or a DB connection."""
    result = {}
    for directory in ("ssr_chat", "tests", "contracts", "docs"):
        for root, dirs, files in os.walk(REPO / directory):
            base = Path(root)
            dirs[:] = sorted(d for d in dirs if d not in {"__pycache__", "acceptance", "evidence", ".test-output", "runs"}
                             and not (base / d).is_symlink())
            for name in sorted(files):
                path = base / name
                if path.suffix in {".py", ".json", ".md", ".js", ".ps1"} and not path.is_symlink():
                    result["ssr-chat/" + path.relative_to(REPO).as_posix()] = sha(path.read_bytes())
    for name in ("interfaces/record-get-producer.json", "acceptance/independent-verification.json"):
        path = provider_file(name)
        result["chat-store/" + name] = sha(path.read_bytes())
        for relative, fingerprint in manifest_map(read(path)).items():
            result["chat-store/" + relative] = fingerprint
    return dict(sorted(result.items()))


def quiescent(path):
    require(path.is_file(), "registered_database_missing")
    require(not any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")), "database_not_quiescent")
    with path.open("rb") as stream:
        header = stream.read(20)
    require(header[:16] == b"SQLite format 3\x00" and len(header) == 20 and header[18:20] == b"\x01\x01",
            "database_not_quiescent")


def acl_narrows(value, original):
    return (isinstance(value, dict) and set(value) == {"read_scopes", "write_scopes"}
            and all(isinstance(value[k], list) and all(selector in original[k] for selector in value[k])
                    for k in ("read_scopes", "write_scopes")))


def policy_narrows(policy, base):
    return (isinstance(policy, dict) and set(policy) == set(base) and policy["sources"] == base["sources"]
            and isinstance(policy["operations"], list) and all(isinstance(v, str) for v in policy["operations"])
            and len(set(policy["operations"])) == len(policy["operations"])
            and all(operation in base["operations"] for operation in policy["operations"])
            and acl_narrows(policy["access_scope"], base["access_scope"])
            and isinstance(policy["source_access_scopes"], dict)
            and set(policy["source_access_scopes"]) == set(base["source_access_scopes"])
            and all(acl_narrows(policy["source_access_scopes"][source], base["source_access_scopes"][source])
                    for source in base["source_access_scopes"]))


def policy_snapshot(policy):
    return {"access_scope": policy["access_scope"], "operations": sorted(policy["operations"]),
            "sources": policy["sources"], "source_access_scopes": policy["source_access_scopes"]}


def allows_read(access, scope):
    return any(selector.get("domain") == scope["domain"] and all(
        selector.get(key, {}).get("mode") == "any" or selector.get(key, {}).get("value") == scope[key]
        for key in SCOPE_FIELDS - {"domain"}) for selector in access["read_scopes"])


def validate_schema(value, node, schema):
    """Validate the pinned owner's local schema vocabulary; no remote refs."""
    if "$ref" in node:
        reference = node["$ref"]
        require(reference.startswith("#/$defs/"), "nonlocal_schema_reference")
        return validate_schema(value, schema["$defs"][reference.split("/")[-1]], schema)
    for keyword in ("oneOf", "anyOf"):
        if keyword in node:
            matches = 0
            for choice in node[keyword]:
                try:
                    validate_schema(value, choice, schema)
                    matches += 1
                except BindingError:
                    pass
            require(matches == 1 if keyword == "oneOf" else matches > 0, "native_schema_mismatch")
    if "const" in node:
        require(type(value) is type(node["const"]) and value == node["const"], "native_schema_mismatch")
    if "enum" in node:
        require(any(type(value) is type(choice) and value == choice for choice in node["enum"]), "native_schema_mismatch")
    kinds = node.get("type")
    if kinds is not None:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        accepted = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
                    "integer": type(value) is int, "number": type(value) in (int, float), "boolean": type(value) is bool,
                    "null": value is None}
        require(any(accepted.get(kind, False) for kind in kinds), "native_schema_mismatch")
    if isinstance(value, dict):
        require(set(node.get("required", [])) <= set(value), "native_schema_mismatch")
        properties = node.get("properties", {})
        if node.get("additionalProperties") is False:
            require(set(value) <= set(properties), "native_schema_mismatch")
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key], schema)
            elif isinstance(node.get("additionalProperties"), dict):
                validate_schema(item, node["additionalProperties"], schema)
    elif isinstance(value, list):
        require(len(value) >= node.get("minItems", 0) and len(value) <= node.get("maxItems", len(value)), "native_schema_mismatch")
        if node.get("uniqueItems"):
            require(len({canonical(item) for item in value}) == len(value), "native_schema_mismatch")
        if "items" in node:
            for item in value:
                validate_schema(item, node["items"], schema)
    elif isinstance(value, str):
        require(len(value) >= node.get("minLength", 0) and len(value) <= node.get("maxLength", len(value)), "native_schema_mismatch")
        if "pattern" in node:
            require(re.search(node["pattern"], value) is not None, "native_schema_mismatch")
        if node.get("format") == "date-time":
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise BindingError("native_schema_mismatch") from None
            require(parsed.tzinfo is not None, "native_schema_mismatch")
    elif type(value) in (int, float):
        require(value >= node.get("minimum", value) and value <= node.get("maximum", value), "native_schema_mismatch")
        if "exclusiveMinimum" in node:
            require(value > node["exclusiveMinimum"], "native_schema_mismatch")
        if "exclusiveMaximum" in node:
            require(value < node["exclusiveMaximum"], "native_schema_mismatch")


def bundle(profile_path):
    path = local(profile_path)
    profile = read(path)
    require(set(profile) == PROFILE_FIELDS and profile["schema_version"] == "1.0.1"
            and profile["profile"] == PROFILE and profile["synthetic"] is True, "host_profile_invalid")
    require(profile["provider_checkout"] == "repos/components/hgpt-chat-store"
            and profile["caller_profile"] == "interfaces/record-get-caller-profile.synthetic.json"
            and profile["producer_manifest"] == "interfaces/record-get-producer.json"
            and profile["final_manifest"] == "acceptance/independent-verification.json", "fixed_provider_required")
    caller_path = provider_file(profile["caller_profile"])
    require(sha(caller_path.read_bytes()) == profile["caller_profile_sha256"], "caller_profile_pin_mismatch")
    caller = read(caller_path)
    require(caller["no_live_database_bound"] is True and caller["example_is_executable_binding"] is True
            and caller["example_selection"]["source_kind"] == "synthetic", "synthetic_delivery_required")
    producer = read(provider_file(profile["producer_manifest"]))
    final_path = provider_file(profile["final_manifest"])
    require(sha(final_path.read_bytes()) == profile["final_manifest_sha256"], "final_manifest_pin_mismatch")
    final = read(final_path)
    for name, manifest, expected in (("producer", producer, profile["producer_source_sha256"]),
                                     ("final", final, profile["final_source_sha256"])):
        actual = manifest_map(manifest)
        require(actual == manifest["files_sha256"] and digest(actual) == expected == manifest["source_sha256"], name + "_source_pin_mismatch")
    # Match the producer's own complete Python source inventory, not just an
    # arbitrary subset supplied by an operator profile.
    python_files = {p.relative_to(PROVIDER).as_posix() for p in (PROVIDER / "source").rglob("*.py")}
    require(python_files == {p for p in producer["files_sha256"] if p.startswith("source/")}, "producer_source_inventory_changed")
    paths = {name: provider_file(caller["fixed_host_arguments"][name]) for name in ("db", "binding", "policy")}
    require(caller["fixed_host_arguments"]["db"].startswith(".test-output/record-get-delivery/synthetic-"), "synthetic_database_path_required")
    for name, key in (("db", "database_sha256"), ("binding", "binding_sha256"), ("policy", "policy_sha256")):
        require(sha(paths[name].read_bytes()) == profile[key], name + "_pin_mismatch")
    quiescent(paths["db"])
    binding, base_policy = read(paths["binding"]), read(paths["policy"])
    require(binding == caller["example_selection"] and binding["producer_source_sha256"] == profile["producer_source_sha256"]
            and binding["database_sha256"] == profile["database_sha256"], "registered_selector_mismatch")
    reference_path = provider_file(caller["native_delivery"]["stdout"])
    require(sha(reference_path.read_bytes()) == profile["reference_receipt_sha256"], "reference_receipt_pin_mismatch")
    reference = read(reference_path)
    require(reference["binding"] == binding and reference["status"] == "ok" and len(reference["records"]) == 1
            and digest(reference["records"][0]) == profile["record_sha256"], "reference_record_pin_mismatch")
    policy, policy_raw = base_policy, paths["policy"].read_bytes()
    override = profile["policy_override"]
    if override is not None:
        override_path = local(override, WORKSPACE / "evidence")
        policy_raw = override_path.read_bytes()
        policy = load(policy_raw)
        require(policy_narrows(policy, base_policy), "policy_authority_broadened")
        paths["policy_override"] = override_path
    paths.update(host_profile=path, caller_profile=caller_path, reference_receipt=reference_path,
                 producer_manifest=provider_file(profile["producer_manifest"]), final_manifest=final_path)
    return {"profile": profile, "binding": binding, "policy": policy, "base_policy": base_policy,
            "policy_raw": policy_raw, "producer": {k: producer[k] for k in ("application", "source_sha256", "files_sha256")},
            "reference": reference, "paths": paths,
            "schema": read(provider_file("fixtures/v1.0.1/contracts_v1.0.1.json"))}


def validate_query(query, material):
    validate_schema(query, material["schema"]["$defs"]["Query"], material["schema"])
    require(set(query) == QUERY_FIELDS and query["schema_version"] == "1.0.1", "query_fields_or_version_invalid")
    require(isinstance(query["request_id"], str) and query["request_id"].strip() and isinstance(query["text"], str), "query_identity_invalid")
    require(query["intent"] == "record-get" and query["layer"] == "raw" and type(query["limit"]) is int and query["limit"] == 1,
            "exact_raw_record_query_required")
    bound = material["binding"]
    require(query["scope"] == bound["scope"], "query_scope_mismatch")
    require(query["actor_account_id"] in (None, bound["actor_account_id"]) and query["person_id"] is None, "query_selector_mismatch")
    require(query["conditions"] == {}, "query_conditions_unsupported")
    require(all(query[k] is None or isinstance(query[k], str) for k in ("time_from", "time_to")), "query_time_invalid")
    require(isinstance(query["exclude_event_ids"], list) and all(isinstance(v, str) and v for v in query["exclude_event_ids"])
            and len(set(query["exclude_event_ids"])) == len(query["exclude_event_ids"]), "query_exclusions_invalid")
    require(acl_narrows(query["access_scope"], material["base_policy"]["access_scope"]), "query_authority_broadened")


def validate_receipt(receipt, query, material, exit_code):
    require(set(receipt) == RECEIPT_FIELDS and receipt["profile_format"] == FORMAT and receipt["operation"] == "record-get", "receipt_shape_invalid")
    require(receipt["binding"] == material["binding"] and receipt["producer"] == material["producer"], "receipt_binding_or_producer_mismatch")
    require(receipt["query_sha256"] == digest(query) and receipt["trusted_policy_sha256"] == digest(policy_snapshot(material["policy"])), "receipt_query_or_policy_hash_mismatch")
    require(receipt["grants_created"] == [], "receipt_created_grants")
    status, native = receipt["status"], receipt["native_result"]
    validate_schema(native, material["schema"]["$defs"]["Result"], material["schema"])
    require(exit_code == EXIT_CODES.get(status) and isinstance(native, dict) and native.get("schema_version") == "1.0.1"
            and native.get("domain") == "chat_history" and native.get("status") == status
            and native.get("request_id") == receipt["request_id"] == query["request_id"], "native_result_or_exit_mismatch")
    require(isinstance(receipt["observed_at_utc"], str) and isinstance(receipt["reason_code"], str), "receipt_diagnostics_invalid")
    require(set(native["source_kind"]) <= {material["binding"]["source_kind"]}, "native_source_kind_mismatch")
    for attempt in native["execution"]["attempted_sources"]:
        require(attempt["source_id"] in (None, material["binding"]["source_id"])
                and attempt["source_kind"] == material["binding"]["source_kind"], "native_attempt_source_mismatch")
    if status == "no_match":
        require(receipt["reason_code"] == "bound_record_not_found" and native["reason_code"] == "no_records", "empty_receipt_reason_invalid")
    else:
        require(receipt["reason_code"] == native.get("reason_code"), "receipt_reason_mismatch")
    if status != "ok":
        require(receipt["records"] == [] and receipt["record_sha256"] == [] and receipt["source_authorization"] == []
                and native.get("evidence") == [], "failed_receipt_leaks_records")
        return
    require(native["source_kind"] == [material["binding"]["source_kind"]]
            and native["missing_fields"] == [] and native["blocked_by"] == [] and native["execution"]["errors"] == []
            and len(native["execution"]["attempted_sources"]) == 1
            and native["execution"]["attempted_sources"][0]["source_id"] == material["binding"]["source_id"]
            and native["execution"]["attempted_sources"][0]["status"] == "ok", "successful_native_trace_inconsistent")
    require(len(receipt["records"]) == len(receipt["record_sha256"]) == len(receipt["source_authorization"]) == len(native.get("evidence", [])) == 1,
            "receipt_exact_one_record_required")
    record = receipt["records"][0]
    require(isinstance(record, dict) and (set(record) == RECORD_FIELDS or set(record) == RECORD_FIELDS | {"extensions"}
            and record["extensions"] == {}), "receipt_record_shape_invalid")
    require(record == material["reference"]["records"][0] and receipt["record_sha256"] == [digest(record)]
            and digest(record) == material["profile"]["record_sha256"], "original_record_changed")
    require(record["schema_version"] == "1.0.1" and record["record_kind"] == "raw_event"
            and "synthetic" in record["quality_flags"], "synthetic_original_record_required")
    for field in ("scope", "source_id", "source_event_id", "record_id", "revision", "actor_account_id"):
        require(record[field] == material["binding"][field], "record_selector_mismatch")
    evidence = native["evidence"][0]
    require(evidence["source_kind"] == material["binding"]["source_kind"] and evidence["layer"] == "raw"
            and evidence["rank"] is None, "native_evidence_kind_or_layer_mismatch")
    require(all(evidence.get(k) == v for k, v in record.items() if k not in {"schema_version", "record_kind", "access_scope"}), "native_evidence_original_values_changed")
    authority = receipt["source_authorization"][0]
    require(set(authority) == {"source_id", "scope", "first_persisted_source_access_scope", "current_source_access_scope",
                              "authorized_operation", "record_acl_is_authorization_override"}, "source_authority_shape_invalid")
    require(authority["source_id"] == record["source_id"] and authority["scope"] == record["scope"]
            and authority["authorized_operation"] == "read" and authority["record_acl_is_authorization_override"] is False,
            "source_authority_binding_invalid")
    require(authority["first_persisted_source_access_scope"] == material["reference"]["source_authorization"][0]["first_persisted_source_access_scope"]
            and authority["current_source_access_scope"] == material["policy"]["source_access_scopes"][record["source_id"]],
            "independent_source_authority_changed")
    require("get" in material["policy"]["operations"] and all(allows_read(acl, record["scope"]) for acl in (
        material["base_policy"]["access_scope"], material["policy"]["access_scope"], query["access_scope"],
        authority["first_persisted_source_access_scope"], authority["current_source_access_scope"])), "success_read_intersection_missing")


def safe_source_files():
    try:
        return source_files()
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return {"__snapshot_unavailable__": True}


def path_hashes(paths):
    result = {}
    for path in paths:
        try:
            result[str(path)] = sha(Path(path).read_bytes())
        except (OSError, ValueError):
            result[str(path)] = None
    return result


def execute(profile_path, query_path, run_dir):
    root = local(run_dir, WORKSPACE / "evidence")
    root.mkdir(parents=True, exist_ok=False)
    before = safe_source_files()
    write(root / "source-before.json", before)
    report = {"profile": PROFILE, "data_mode": "registered_synthetic_readback", "status": "held", "records": [],
              "provider_invoked": False, "reader_invoked": False, "model_invoked": False, "public_output": "not_attempted", "calls": []}
    code, effective, original_hashes, material = 4, {}, {}, None
    try:
        require("__snapshot_unavailable__" not in before, "source_snapshot_unavailable")
        material = bundle(profile_path)
        query_path = local(query_path)
        raw = query_path.read_bytes()
        query = load(raw)
        validate_query(query, material)
        original_hashes = {str(path): sha(path.read_bytes()) for path in material["paths"].values()}
        effective = {**original_hashes, str(query_path): sha(raw)}
        write(root / "authority-before.json", original_hashes)
        write(root / "effective-inputs.json", effective)
        copied = root / "provider-snapshot.sqlite3"
        with material["paths"]["db"].open("rb") as source, copied.open("xb") as target:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                target.write(block)
        require(sha(copied.read_bytes()) == material["profile"]["database_sha256"], "copied_database_pin_mismatch")
        quiescent(copied)
        write_bytes(root / "binding.json", material["paths"]["binding"].read_bytes())
        write_bytes(root / "policy.json", material["policy_raw"])
        write_bytes(root / "query.json", raw)
        command = [sys.executable, "-B", "-X", "utf8", "-m", "source.hgpt_ssr.record_reader", "--db", str(copied),
                   "--policy", str(root / "policy.json"), "--binding", str(root / "binding.json"), "--query", str(root / "query.json")]
        started, stdout, stderr, native_code, outcome, failure = now(), b"", b"", None, "unavailable", None
        try:
            child = subprocess.run(command, cwd=PROVIDER, stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            stdout, stderr, native_code, outcome = child.stdout, child.stderr, child.returncode, "returned"
        except subprocess.TimeoutExpired as exc:
            stdout, stderr, outcome = exc.stdout or b"", exc.stderr or b"", "timeout"
            failure = {"type": "TimeoutExpired", "timeout": exc.timeout}
        except OSError as exc:
            failure = {"type": type(exc).__name__[:80], "errno": exc.errno, "winerror": getattr(exc, "winerror", None),
                       "filename": str(exc.filename)[:512] if exc.filename is not None else None}
        write_bytes(root / "provider-stdout.bin", stdout)
        write_bytes(root / "provider-stderr.bin", stderr)
        call = {"stage": "record-get", "command": command, "cwd": str(PROVIDER), "stdin": "DEVNULL", "started_at": started,
                "finished_at": now(), "exit_code": native_code, "outcome": outcome, "failure": failure,
                "stdout_sha256": sha(stdout), "stderr_sha256": sha(stderr)}
        write(root / "provider-execution.json", call)
        report["calls"].append(call)
        report["provider_invoked"] = outcome in {"returned", "timeout"}
        require(outcome == "returned", "provider_execution_" + outcome)
        require(sha(copied.read_bytes()) == material["profile"]["database_sha256"], "copied_database_changed")
        quiescent(copied)
        receipt = load(stdout)
        validate_receipt(receipt, query, material, native_code)
        write(root / "provider-receipt.json", receipt)
        report.update(status="raw_record_result", provider_status=receipt["status"], provider_exit_code=native_code,
                      provider_receipt=receipt, records=receipt["records"], reader_status="held",
                      reader_hold_reasons=["chat_reader_metadata_unavailable", "chat_reader_purpose_profile_unavailable"])
        code = native_code
    except (ValueError, OSError, TypeError, KeyError, RecursionError) as exc:
        report.update(status="held", reason_code=getattr(exc, "code", "raw_record_input_or_execution_invalid"), records=[])
    after = safe_source_files()
    write(root / "source-after.json", after)
    unchanged = "__snapshot_unavailable__" not in before and "__snapshot_unavailable__" not in after and before == after and path_hashes(effective) == effective
    write(root / "authority-after.json", path_hashes(material["paths"].values()) if material else {})
    if not unchanged:
        report.pop("provider_receipt", None)
        report.update(status="held", reason_code="source_or_authority_changed_during_execution", records=[])
        code = 4
    report.update(source_unchanged=unchanged, grants_created=[], real_llm_success=False)
    write(root / "receipt.json", {**report, "exit_code": code})
    return report, code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=str(HOST_PROFILE), help="Trusted host-only profile, never a public Query field")
    parser.add_argument("--query", required=True)
    parser.add_argument("--run-dir", required=True, help="New private directory below workspace/evidence")
    args = parser.parse_args(argv)
    report, code = execute(args.profile, args.query, args.run_dir)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
