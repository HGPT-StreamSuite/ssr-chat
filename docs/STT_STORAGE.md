# Synthetic STT text normalization and storage

`ingest-stt` connects the actual task-21 text normalizer to the actual task-8
transcript archive. It is a separate, opt-in local CLI. It accepts already
authored synthetic text Records, never opens an audio source and never starts
the chat runtime, HTTP API, query gateway, model, or output adapter.

```powershell
uv --offline run --no-sync python -m ssr_chat ingest-stt --config <pipeline.json> --state-dir <dedicated-test-child> --input <synthetic-batch.json>
```

Input is exactly `{request_id, synthetic: true, events: [Record, ...]}` with at
most 50 Records. `--input -` reads UTF-8 stdin. The CLI limits input to 1 MB and
rejects duplicate JSON keys and nonfinite numbers. Exit 0 means every item was
stored/idempotent, or the batch was empty; exit 1 means at least one item was
held/uncertain; exit 2 means invalid input/configuration. Commits are per item,
not a transaction over the entire batch. Failed items are never automatically
retried or silently replaced.

## Contract and authorization

The pipeline configuration contains exactly `schema_version` (`1.0.1`), `mode`
(`synthetic_stt_ingest_only`), `python`, `normalizer_repo`, `store_repo`,
`normalizer_policy`, `store_sources`, `store_caller`, `purpose_policy`,
`state_root`, and `binding`. Paths are explicit local paths. The binding fixes
one synthetic source, an internal actor (explicit null is allowed), and all six
scope fields. The domain is `stt_transcript`, platform is `synthetic-stt`, and
channel/session must be explicit. There is no scope wildcard or cross-session
fallback. `fixtures/stt_storage/contract.synthetic.json` contains the authored
Record and policy shapes; it grants no real-source rights.

The normalizer's caller and source-write decisions remain independent from the
store's caller, authoritative source ACL, exact source scopes, and ingest gate.
Both callers are write-only. An incoming Record's ACL must equal the normalizer's
host-authored source visibility. It cannot grant caller permissions. The host's
separate purpose policy binds the same source and scope and must explicitly
allow `local_cache` before any text is spooled. Missing, unknown, denied and
prohibited grants stop. ACL permission does not substitute for purpose consent.
Search indexing, AI input/training and public output are not invoked.

## STT handoff

Records must include all V1.0.1 fields and exactly `extensions.stt` with
`segment_id`, `segment_revision`, `state`, `speaker_id`, `started_at`, `ended_at`,
and `source_confidence`. Segment identity/revision must match the outer Record.
Unknown revisions are held; they are not filled with revision 1 or forwarded to
the store's separate invalid-attempt ingestion API. Additional personal fields
and unrelated extensions are rejected. One explicit `synthetic://` source
reference must match source/Record/revision.

The actual task-21 CLI validates timestamps (including sub-microsecond source
precision), partial/final state and confidence, and adds only its declared
quality flags: state, `speaker_unknown`, and threshold-based `low_confidence`.
The adapter checks that every other field is unchanged before forwarding the
Record to task-8. No text trimming, Unicode normalization, new event times,
speaker/account inference, summary, publication status or semantic noise label
is introduced. A speaker label is metadata independent of the host-bound actor.

Task-8's actual persistent event/revision identity handles redelivery and fresh
normalizer/CLI processes. Current uses the highest accepted revision, not arrival
order; raw history retains older revisions. Partial/final is source STT state,
not an editorial approval or published statement. Same revision with changed
payload is a conflict. Internal actor and Record identity stay immutable.

## State, failure and evidence

Use a dedicated child of the configured synthetic state root. Unowned nonempty
directories are rejected. State includes a binding/code/configuration fingerprint,
a one-byte process lock, owned SQLite file identity/schema and a hash-only purpose/status
receipt. A missing, replaced or truncated owned database is held without recreation. Code
or policy changes invalidate an existing run; the adapter does not reset state
to adopt a new binding. Temporary policy files are cleaned after each call.

Text is passed to native CLIs through stdin. Native package names are isolated
by separate working directories; no cross-project Python imports are used. The
only transcript persistence is `synthetic-stt.sqlite3` in the owned test state.
The adapter returns bounded statuses, stages and digests; no text, speaker,
actor, raw backend errors, or query evidence is returned in CLI summaries or
purpose receipts. A separate explicitly authorized synthetic reader is used by
the integration verification, not by the write-only pipeline.

A store timeout, malformed acknowledgement or uncertain outcome is not reported
as successful. There is no automatic retry. Explicit redelivery can ask the
native store to confirm idempotency. A hard crash after the first commit but
before initial file attestation remains fail closed for operator review; this
slice does not certify exactly-once delivery or hard-crash recovery.

This is synthetic text ingestion only. It does not establish real-source consent,
audio transcription quality, microphone access, live Twitch ingestion, Query/STT
answer integration, or operation of the new slice on the unchanged 8775 service.
