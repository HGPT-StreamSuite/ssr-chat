# Synthetic chat-history binding

The fixed local profile `planner_chat_history_to_reader_history_synthetic_v1`
connects current native planner output to the native chat reader. It changes
exactly `Query.intent` from `chat_history` to `history`. The original Plan remains
unchanged; every other Query key, value, presence and order is preserved.

`question_only=true` keeps the reader's traceable question-flag filter. False and
absence both mean no question filter, with their original distinct representations
retained. The binding never changes true to `question_history`, guesses identities,
adds grants, strips conditions, changes sources, or retries with a broader query.
Unknown profiles/versions/intents, missing actor/person, unresolved task dependencies
and unsupported conditions are explicit failures. `recent_utterance` and LT are
outside this profile. The existing recent option is independent and mutually
exclusive with this new pipeline option.

## Local native chain

    python -m ssr_chat.chat_history_pipeline --config OPERATOR.json --input REQUEST.json --run-dir NEW_EVIDENCE_DIRECTORY

The request has exactly `request_id` and `text`. The operator configuration has
exact keys `schema_version`, `profile`, `synthetic`, `template`, `snapshot`,
`authorization`, `source_id`, `source_files_sha256`. Both the template and its
trusted_context explicitly declare synthetic=true. Snapshot and authorization
are separate immutable local files beneath the operator configuration directory;
the snapshot must be synthetic and source IDs must match the registered binding.
`source_files()` supplies the current three-checkout file pins. No commands,
interpreter, endpoint, database, or caller ACL can be supplied by the public input.

The fixed native commands use the canonical planner and reader checkouts and the
current interpreter with file arguments and DEVNULL stdin. Each run exclusively
creates an evidence directory under the workspace evidence root, containing the
original input/Plan/Query, mapped Query, exact stdout/stderr, command/cwd/exit and
source/config fingerprints before and after. No Git metadata, live state, network,
HTTP, model or public reply is accessed.

The complete native reader Query parser validates the mapped Query. Its Result
and exit are preserved: ok/no_match/skipped=0, missing_input=2, not_configured=3,
denied=4, error=1. An earlier caller/binding hold uses exit3 with reader_invoked=false
and its own reason; it is not a fabricated native Result. Source drift always holds.

## Existing retrieval routes

`PipelineConfig.chat_history_intent_adapter` is optional and defaults to None.
Only the exact profile is accepted; it requires an explicit synthetic host envelope
and conflicts with the old recent option. The existing `retrieve` CLI and named
`/retrieve/chat-history` gateway can use it with trusted operator configuration.
The original Plan is sent to collector; the adapted copy and exact mapping audit
are retained separately. No existing production route is enabled by this change.
The three-repo chain receipts do not claim a new dispatcher/collector/browser run.

The producer proposal and consumer handoff profiles were read as evidence; their
disabled documents remain unchanged. This caller-owned profile is authorized only
for the current isolated synthetic integration. Chat-store's original Record exit,
STT/channel stores and real-source/AI/publication policies remain separate work.
