# Synthetic game-name retrieval

`planner_game_name_retrieval_synthetic_v1` connects the current native planner
to game-search without changing the generated Query. The intent stays
`game_lookup`; the reader accepts it as a diagnostic label. The caller requires
one game task, no dependencies, the same request ID/text and the registered
complete game scope. Missing versions are never replaced with `latest`.

    python -m ssr_chat.game_name_pipeline --config OPERATOR.json --input REQUEST.json --run-dir NEW_EVIDENCE_DIRECTORY

The public request has only `request_id` and `text`. Operator configuration has
`schema_version`, `profile`, `synthetic`, `template`, `source_id`,
`source_files_sha256` and six file paths: `snapshot`, `source_records`,
`projection_registry`, `manifest`, `context`, `handoffs`. Both template and its
trusted_context declare synthetic=true. Source-code pins come from this module's
`source_files()` and include ssr-chat, planner and game-search. Git metadata is
not accessed. Interpreter, canonical checkouts, commands and file-argument CLI
entrypoints are fixed by code, not public/config-supplied commands.

## Data provenance

`contracts/game_name_source.synthetic.v1.json` pins six saved artifacts from the
previously executed synthetic importer -> original Record -> game store ->
authorized raw Record export -> literal sidecar -> reader chain. This caller
does not rerun that chain or access a store database. Each new case copies its
files into a new isolated evidence directory.

Snapshot rows and exported full Records must agree in both directions. Every row
must match the registered full scope/source/event/record/revision and canonical
complete Record hash, including original ACL, references and field presence.
Name and number/date fields come from the registered literal sidecar, not text
analysis or a model. Evidence is never relabelled or completed as a Record.
Source ID must agree across registry, manifest, Records, attempts and Evidence.

Manifest/context may be the original profile or an explicitly narrower refusal:
disable query_allowed with a reason, remove existing read selectors or allowed
layers. Broader source grants, changed Record ACLs or invented policy fail closed.
The saved source permits search indexing; AI input/training remain denied.

## Query behavior

Known names such as `Integration Blade`, finite native lexical text and unknown
literal names are sent to the real reader. Exact name matches affect ranking;
they do not create an exclusive exact-only filter. Unknown names return the
native no_match, never a fabricated caller response.

Only empty conditions or the existing `before_event_time` upper bound are
supported. The latter is preserved; native planner/reader validate its timestamp.
Numeric `filters` have no planner object-list capability and are explicitly
rejected. Explicit comparison syntax in text, reserved numeric field/value forms
and supported Chinese numeric-comparison markers are also rejected; this guard
does not infer a predicate or claim general natural-language understanding.
No unsupported condition is dropped to manufacture a successful query.

Native Result/exit are preserved: ok/no_match/skipped=0,
missing_input/not_configured/denied=2, error=1. Caller/profile holds use exit3,
with zero reader calls when blocked before that boundary. Failed source pins or
changed source/input fingerprints hold the result; no fallback is attempted.
Exact command, input, stdout/stderr and outcome are saved even for timeout or
process unavailability. The original Plan and Query remain separate artifacts.

This is isolated retrieval only. It does not activate production, game answers,
LLM, platform messages, services or browser access. The blocked retrieval
regression (16 tests/19 WinError5) and all other historical refusals remain
independent; this matrix is not a substitute or a retry.
