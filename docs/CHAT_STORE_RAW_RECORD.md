# Synthetic chat-store raw Record caller

`python -m ssr_chat.chat_store_binding --profile HOST.json --query QUERY.json --run-dir NEW_EVIDENCE_DIR`

This host-only entry uses the registered provider checkout and existing
`source.hgpt_ssr.record_reader` CLI. It copies the pinned quiescent synthetic
SQLite file into a new evidence directory, then passes only fixed file arguments
and DEVNULL stdin. It never opens the registered DB through SQLite, writes the
provider checkout, accepts a command/runtime/endpoint from a Query, or falls back
to another revision/source. No server, browser, network, model or public message
is invoked. The local host profile is not a shared wire schema or a grant.

The profile pins the producer's 18 files, the final 52-file source map, original
DB, binding, caller policy and reference receipt. A host-only `policy_override`
may point to a new evidence file that only removes existing operations/read/write
selectors; source identities cannot be relabelled and no authority can be added.
The public native Query remains 1.0.1, exact scope, raw, record-get, limit=1.

The caller validates canonical query/policy/Record hashes, the pinned original
Record, native Result schema, source kind/layer/trace, exact selection, separate
first-persisted/current source ACLs, current caller/query READ intersection and
empty `grants_created`. It preserves native ok/no_match exit0, denied exit3 and
missing_input/not_configured/error exit2. Local guards, invalid receipts and
source/authority changes return held exit4 with no returned Record. Full raw
stdout/stderr and hashes stay in the private new evidence folder; stderr is not
copied into the caller report. Empty and denial results never trigger fallback.

Raw readback success explicitly leaves chat-search held before invocation:
`chat_reader_metadata_unavailable` and `chat_reader_purpose_profile_unavailable`.
The delivery has no authoritative sequence/questionflag sidecar, and storage
READ does not grant AI use. No sidecar, host authorization or AI allow is inferred
from text, array order, Record ACL or Query ACL. A successful raw caller result is
not positive reader integration, Twitch/LLM acceptance, deployment or browser QA.

`test_chat_store_binding.py` uses pure receipt/process/source fault injection;
those mocks are not native evidence. The separate r2 matrix runs actual fixed
provider CLI subprocesses against copied synthetic DBs and records every command,
stream, exit, original/copy DB fingerprint and source/authority before/after map.
