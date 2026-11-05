# Synthetic channel knowledge quote drafts

`draft-knowledge` reads an owned synthetic channel archive through actual native
task-4 `get` and calls native task-23 `organize` in a separate local Python
process. The raw source is seeded through task-19 -> task-4 `ingest-channel`.
Task-23 returns an in-memory Result/audit; the host independently stores those
outputs as isolated, unpublished draft files. There is no new knowledge DB and
no draft/publish write to task-4. This CLI is not loaded on 8775.

```powershell
uv --offline run --no-sync python -m ssr_chat draft-knowledge --config pipeline.json --state-dir drafts-cache/run --input request.json
```

The request has exactly `request_id` and `synthetic: true`. Scope, source,
component paths, author, hook, permissions, caller labels and fragment selections
come from trusted config, never request text. The only connected transformation
is native `quote_fragments`: literal Unicode-codepoint slices joined by the
configured separator. The caller explicitly declares `setting`, `audience_claim`
or `inference`; the adapter does not infer truth or labels, summarize or use AI.
Missing/invalid hooks or references remain native failures without a fallback.

The schema-1.0.1 config uses mode `synthetic_knowledge_drafts_only` and names the
fixed local Python/candidate, synthetic channel ingest config/state, exact source
SQLite SHA-256, read-only task-4 reader context, task-23 author/read access,
purpose policy, hook, caller marks, dedicated cache root and complete current
original Record roster. The fixture contract is self-contained synthetic data;
the additional captured native envelope is a unit-test stand-in, not a substitute
for the real API integration checks.

`local_cache` and `knowledge_drafts` require separate explicit allowed grants
with a basis and exact source/full scope binding. An independent archive-read
decision and task-4 caller/current Source read access are checked before reading
DB bytes or caching source data. The source's ingest marker, fingerprint, file
identity, handoff hashes and database snapshot hash must remain valid.

Task-4 selects each event's latest revision before returning its raw current
view. The unfiltered full-scope Evidence must exactly match the registered
current originals. An unexpected row, stale/incomplete roster, changed source,
or possible truncation stops drafting. Evidence lacks Record ACL fields, so the
host forwards verified original Records unchanged rather than constructing ACLs.
Older revisions remain in the owned source DB but are not passed to task-23.
This connects only the channel current view, not task-23 STT/history or game paths.

A fixed host bridge calls the actual candidate API in its own import namespace.
It supplies a read-policy callback that checks operation, synthetic source kind,
exact source/scope and independent organizer-read decision/purpose grants for
each native policy invocation. No `fixture-read-policy` flag or synthetic-policy
stub is used. Missing organizer policy remains native `not_configured`; refusal
remains native `denied`. Task-23 performs its own Record ACL and trusted caller
read checks. Its in-memory Result does not grant storage or publication rights;
the host's explicit cache/draft grants govern these local files.

The adapter verifies the unmodified packet hash, raw originals in the audit,
every caller label, literal fragment text, Unicode offsets, retained source refs,
raw revisions and transformation versions. Derived Evidence keeps the native
draft source/ID, new draft revision 1, actual creation time and explicit editor
identity. Raw authors/timestamps/revisions stay in the audit and refs; they are
not replaced by draft metadata. A new raw revision creates new provenance, not
an in-place rewrite of an existing draft.

Completed requests retain their native output bytes, creation time and IDs on
redelivery/new process restart. Before returning a duplicate, current purpose,
configuration, source hash, owner and cache continuity are rechecked. Duplicate
requests do not invoke the native transform again. A changed source requires
explicit registration of the new hash/current roster/marks and a new cache;
old bindings are not silently adopted. Different request IDs are distinct draft
requests; cross-request semantic deduplication is not claimed.

Native computation is read only and has no persisted child side effects. If
its response is lost before the host stores a result, a later explicit replay
can recompute. A result file without a completion receipt is held for review,
not overwritten or silently recomputed. Cache files have file-ID/hash attestation
and local locking; they are not adopted from foreign nonempty directories.
No cross-file crash transaction, hostile-writer atomic snapshot or privileged
offline tamper protection is claimed.

The authorized synthetic cache contains original handoffs, native Result and
audit. Public summaries/receipts expose only bounded codes, counts and hashes,
omitting raw/derived text, authors, event IDs, labels and local paths. Tests use
authored synthetic data, real pinned candidate code, and separately labeled
local fault injections. No private data, network, Twitch, microphone, model,
remote repository, publication or real-data end-to-end acceptance is involved.
