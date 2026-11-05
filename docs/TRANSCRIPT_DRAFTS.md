# Synthetic transcript cleanup drafts

`draft-transcript` connects an owned synthetic task-8 raw archive to the native
task-9 cleanup CLI. The archive is seeded through the actual task-21 -> task-8
`ingest-stt` path. The raw database is read only during drafting. Native task-9
stores a separate Result JSON and a text-free trace; there is no new draft DB.
This independent CLI is not loaded on 8775 or connected to Twitch or a model.

```powershell
uv --offline run --no-sync python -m ssr_chat draft-transcript --config pipeline.json --state-dir drafts-cache/run --input request.json
```

The request has `request_id` and `synthetic: true`; optional fields are
`revision_view` (`current` or `history`), `time_from`, `time_to`,
`exclude_event_ids`, and `limit` (1..100). Scope, source, actor, paths, permissions,
and hook are host config, not request fields. The only connected hook is the
native `whitespace-v1` cleanup. It merges whitespace without adding or correcting
words. A missing hook stays `not_configured`; it does not select a default.
Manual drafts, summaries, knowledge drafts, review, publication, and draft search
or answer routing are not connected.

The trusted config uses schema 1.0.1 and mode `synthetic_transcript_drafts_only`.
It names fixed local component/Python paths, the existing synthetic STT ingest
config/state, its exact database SHA-256, read-only native reader sources/caller,
independent draft context, exact-bound purpose policy, hook, cache root, and a
complete roster of normalized original Records. See
`fixtures/transcript_drafts/contract.synthetic.json` for the policy/roster shape.
Normalizer quality flags are retained; no fields missing from Evidence are
invented and no speaker label is converted into an actor account.

`local_cache` and `transcript_cleanup` each need an explicit allowed grant with
a basis. A separate source-read decision and the caller/current Source read
permissions are also required before reading database bytes or caching an export.
These are host-only policy fields, not changes to the common Record schema or
existing policy.uses. The task-9 context must independently grant draft writes;
raw read access alone does not grant cleanup storage, AI use, or publication.

The source retains its ingest owner marker, fingerprint and file identity. A
complete unfiltered native task-8 `history` query is compared exactly against
the registered original Records. Missing, unexpected, changed or potentially
truncated Evidence is held. The original Records, including ACL and STT metadata,
go to task-9 unchanged. Native task-9 selects the highest supplied revision before
time/exclusion filters; it never falls back to an older visible/matching revision.
`history` preserves the supplied revisions, and identical text from distinct
segment IDs stays distinct. No packet-only current claim is promoted to a live
authoritative current claim.

Authorized host cache files include the explicit raw handoff, source Result,
native draft Result and trace. The standalone task-9 program itself adds no raw
cache; this host cache is separately authorized and isolated. Derived text has a
separate draft source and record ID. The adapter verifies literal cleanup,
unchanged scope/actor/time/STT metadata, retained quality flags and original refs,
and an additional raw revision/byte-hash locator and transformation version.
Public summaries and receipts omit transcript text, actor/speaker/event IDs and
local paths. They expose bounded codes, draft counts and provenance hashes only.

Restart and exact redelivery use native exclusive-create output behavior:
identical bytes are retained, and different existing output is not overwritten.
Cache files are hash/file-ID attested and the operation is locally locked.
Uncertain or partial file output is held without automatic retry; an explicit
replay must confirm the native Result and trace. This does not promise an OS
crash transaction across both output files. A changed source snapshot requires
explicit registration of its new hash and complete roster and a new owned cache;
an old cache cannot silently adopt the new binding. Missing/corrupt source or
cache is held instead of recreated. Hashes do not provide atomic reads against
a hostile writer or protection against privileged offline tampering.

All integration inputs are authored synthetic data and the component programs
are real pinned candidates. Unit-test stand-ins and constructed local faults
are labeled separately. No audio, microphone, real private data, network, live
Twitch, model, remote repository or publication is used or certified.
