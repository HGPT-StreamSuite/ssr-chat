"""Server-owned retrieval configuration; HTTP callers provide no authority."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import re
from typing import Any

from .modular_retrieval import DOMAINS, ModularRetrievalPipeline, PipelineConfig, PipelineError, _local_path, load_object, prepare_artifact_directory


class RetrievalInputError(ValueError):
    """A bounded public request validation code, without request contents."""


class ModularRetrievalGateway:
    def __init__(self, pipeline: ModularRetrievalPipeline, template: dict[str, Any], *, expected_domain: str | None = None) -> None:
        if (not isinstance(template, dict) or not isinstance(template.get("request"), dict)
                or not isinstance(template.get("trusted_context"), dict)
                or template["request"].get("schema_version") != "1.0.1"):
            raise PipelineError("server_retrieval_template_invalid")
        if expected_domain is not None:
            access = template["trusted_context"].get("access_scope")
            scope = template["request"].get("scope")
            if (not isinstance(expected_domain, str) or expected_domain not in DOMAINS
                    or not isinstance(scope, dict) or scope.get("domain") != expected_domain
                    or not isinstance(access, dict) or not isinstance(access.get("read_scopes"), list)
                    or not access["read_scopes"] or access.get("write_scopes") != []
                    or any(not isinstance(selector, dict) or selector.get("domain") != expected_domain for selector in access["read_scopes"])):
                raise PipelineError("route_domain_or_read_authority_mismatch")
            layer = template["request"].get("layer", "raw")
            if layer not in ("raw", "draft", "published"):
                raise PipelineError("route_layer_invalid")
        self._pipeline = pipeline
        self._template = deepcopy(template)
        self._expected_domain = expected_domain

    @classmethod
    def from_files(cls, pipeline_config: str | Path, template: str | Path, *, expected_domain: str | None = None,
                   artifact_dir: str | Path | None = None) -> "ModularRetrievalGateway":
        config = PipelineConfig.read(pipeline_config)
        pipeline = (ModularRetrievalPipeline(config) if artifact_dir is None else
                    ModularRetrievalPipeline(config, artifact_dir=artifact_dir))
        return cls(pipeline, load_object(Path(template).read_bytes()), expected_domain=expected_domain)

    @property
    def trusted_answer_context(self) -> dict[str, Any]:
        """Backend-only binding; never part of HTTP health or request input."""
        return {"scope": deepcopy(self._template["request"].get("scope")),
                "actor_account_id": self._template["trusted_context"].get("actor_account_id"),
                "time_from": self._template["request"].get("time_from"),
                "time_to": self._template["request"].get("time_to"),
                "limit": self._template["request"].get("limit"),
                "current_event": deepcopy(self._template["trusted_context"].get("current_event", {}))}

    @property
    def profile(self) -> dict[str, Any]:
        declared_synthetic = (self._template.get("synthetic") is True
                              and self._template["trusted_context"].get("synthetic") is True)
        profile = {"configured": True, "context_source": "server_configuration",
                   "data_mode": "declared_synthetic" if declared_synthetic else "operator_configured",
                   "answer_generation_enabled": False, "public_output_enabled": False}
        if self._expected_domain is not None:
            profile["domain"] = self._expected_domain
            profile["layer"] = self._template["request"].get("layer", "raw")
        return profile

    def _request(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {"request_id", "text"}:
            raise RetrievalInputError("only_request_id_and_text_allowed")
        request_id, text = payload["request_id"], payload["text"]
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise RetrievalInputError("request_id_invalid")
        if not isinstance(text, str) or not text.strip() or len(text) > 16_384:
            raise RetrievalInputError("question_text_invalid")
        request = deepcopy(self._template)
        request["request"].update(request_id=request_id, text=text)
        return request

    def classification_plan(self, payload: Any) -> dict[str, Any]:
        if self._expected_domain is None:
            raise PipelineError("classification_requires_named_domain_route")
        request = self._request(payload)
        # Remove only the artificial route hint. Keep this route's own scope,
        # identity, conditions, time limits and ACL isolated from other routes.
        request["request"]["scope"]["domain"] = None
        return self._pipeline.plan_only(request)

    def retrieve(self, payload: Any, *, extraction=None) -> dict[str, Any]:
        request = self._request(payload)
        if extraction is not None:
            from .modular_extraction import validate_extraction
            validate_extraction(extraction, payload["text"], request["request"]["scope"])
            request["request"]["text"] = extraction.search_text
        # No caller scope, ACL, layer, source, command, or path is copied from HTTP.
        # Retrieval evidence remains separate from model input and chat output.
        if self._expected_domain is not None:
            report = self._pipeline.run(request, expected_domain=self._expected_domain)
        else:
            report = self._pipeline.run(request)
        if extraction is not None:
            return {**report, "original_question": payload["text"], "query_extraction": extraction.as_dict()}
        return report


def load_retrieval_routes(path: str | Path, *, artifact_root: str | Path | None = None) -> dict[str, ModularRetrievalGateway]:
    """Load exact endpoint -> independent domain-bound gateway from trusted disk."""
    selected = Path(path).resolve()
    value = load_object(selected.read_bytes())
    if (set(value) != {"schema_version", "routes"} or value["schema_version"] != "1.0.1"
            or not isinstance(value["routes"], dict) or not value["routes"]):
        raise PipelineError("retrieval_routes_configuration_invalid")
    routes = {}
    capture_root = prepare_artifact_directory(artifact_root) if artifact_root is not None else None
    for route, spec in value["routes"].items():
        if (not isinstance(route, str) or re.fullmatch(r"/retrieve/[a-z][a-z0-9-]{0,39}", route) is None
                or not isinstance(spec, dict) or set(spec) != {"domain", "pipeline_config", "template"}
                or not isinstance(spec["domain"], str) or spec["domain"] not in DOMAINS):
            raise PipelineError("retrieval_route_invalid")
        options = {"expected_domain": spec["domain"]}
        if capture_root is not None:
            options["artifact_dir"] = capture_root / ("route-" + hashlib.sha256(route.encode("utf-8")).hexdigest()[:16])
        routes[route] = ModularRetrievalGateway.from_files(
            _local_path(spec["pipeline_config"], selected.parent), _local_path(spec["template"], selected.parent),
            **options,
        )
    return routes
