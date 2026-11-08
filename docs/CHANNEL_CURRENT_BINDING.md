# Synthetic channel-store current-view bridge v1

`python -m ssr_chat.channel_current_binding --profile HOST.json --query QUERY.json --run-dir NEW_EVIDENCE_DIR`
is a private host-owned, retrieval-only entry. It runs the fixed canonical
channel-store snapshot CLI and then the fixed canonical channel-search CLI.
It has no server, browser, model, public-send or arbitrary-command input.

The profile is `channel_store_current_search_synthetic_v1`, schema version 1.
It binds every runtime Python/JSON source file selected by `source_pins()` and
the byte hashes of an operator-owned synthetic archive, scope, independent
current trusted context, snapshot request and explicit snapshot grant.
The archive must already exist under the workspace evidence directory.
The caller never creates or mutates that archive. Test setup uses a separate
writer process only against new synthetic data, before each read-only run.

This profile supports current/latest channel evidence only. It explicitly
rejects history, exact revision, revision_view and include_withdrawn requests.
The unchanged Query is passed to the native reader. This is not registration
of a full-history `ReadOnlyAdapter` and does not implement that protocol.

Before reader execution, the caller verifies a newly executed full-history
packet: version, scope/source/layer, canonical snapshot and payload hashes,
current source policy, stored source ACL, original Record ACL, latest identities,
withdrawal facts, lineage and current eligibility. It preserves the complete
provider packet privately. It creates a separate, explicitly named
`eligible_current_only` view, containing unchanged current Records only.
Direct withdrawal and withdrawal of a linked/latest ancestor exclude the
whole family, so older revisions cannot reappear as current. The native reader
applies the original query, ACL, text/time/identity filtering and limit to this
view. Every returned Evidence is checked against the exact original Record.

The stored snapshot grant authorizes `search_index` only. The bridge verifies
`downstream_ai_uses_granted=false`, creates no AI grants, and never sends input
to a model. Exact/any ACL selectors are supported by this closed synthetic
profile; unsupported selectors fail closed. Policy or metadata cannot be
manufactured from caller permissions, text or source order.

Each operation writes an exclusive new directory with input Query, source
pins before/after, full actual argv/cwd/times/exits, raw stdout/stderr, complete
packet, current-view declaration and final receipt. Source/data changes yield
held/exit4 with no returned Result. A returned native Result keeps its native
status and exit mapping; input/transport/provider-boundary holds use exit4.
Errors are never retried. This profile does not activate a persistent service,
change a source policy, or assert upstream collection completeness.
