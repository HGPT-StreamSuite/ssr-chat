# Synthetic activity counts

`count-activity` reads an explicitly registered synthetic chat snapshot through the
native task-7 `export` command, then passes the verified original Records to the
native task-18 counter. It is a separate local CLI; it does not change the running
8775 API or connect to Twitch, a model, or a network service.

```powershell
uv --offline run --no-sync python -m ssr_chat count-activity --config pipeline.json --state-dir counts-cache/run --input request.json
```

The request has `request_id`, `synthetic: true`, and `metrics: ["speech"]`.
Optional `time_from`, `time_to`, `exclude_event_ids`, and `limit` go to the native
counter. `limit` limits returned evidence, not the message count. Identity, scope,
layer, source, file paths, and permissions come only from the trusted config.

The config uses schema 1.0.1 and mode `synthetic_activity_counts_only`. It names
the fixed local Python and counter checkout, an existing synthetic `ingest-chat`
config/state directory, the exact source SQLite SHA-256, reader policy, counts
context, purpose policy, cache root, and a complete roster of original Records.
See `fixtures/activity_counts/contract.synthetic.json` for the policy/roster
contract. Source creation remains the existing task-20 -> task-7 ingest path.

Both `local_cache` and `activity_counts` need separate explicit allowed grants
with a basis, bound to the exact source and full scope. The archive reader is
read-only. Its caller and current Source permissions, each original Record's
permissions, and the counter's separate source-read decision all remain effective.
Missing or unknown permission stops the operation before caching chat content.
The grants are host policy; they do not extend the common Record schema or grant
AI use, publication, person linking, or write access to the source archive.

The source must retain its ingest ownership marker, file identity, and registered
database hash. The adapter exports the full raw scope without time/actor filters
and compares every native Evidence item against the original registered Record.
Evidence omits Record ACL fields, so the adapter forwards verified originals
unchanged instead of reconstructing ACLs. An incomplete roster, unexpected row,
possible truncation, changed source, or failed native command produces a held
result with `message_count: null`. It never substitutes zero for a denied read.

Each roster entry must explicitly have `kinds: ["speech"]`; punctuation and text
are not classifiers. Revisions of one event count once, while distinct event IDs
with identical text remain distinct messages. Native task-18 chooses the latest
revision before applying time/exclusion filters, so a filtered latest revision
does not reveal an older revision. Questions, visits, identity resolution,
person-level aggregation, and relationship changes are not connected here.

Repeated requests and new CLI processes recompute the snapshot; they do not
increment a cumulative counter. A legitimately updated archive requires explicit
registration of its new hash and complete roster and a new owned cache. The old
cache cannot silently adopt a different binding. Cache files are hash/file-ID
attested, locked per operation, and not adopted from a foreign nonempty directory.
This is continuity checking in an isolated test environment, not protection
against a privileged attacker or proof of atomic reads under a hostile writer.

The authorized cache retains original synthetic Records and the full native
envelope, including provenance and evidence truncation metadata. Public CLI
summaries and receipts carry only counts, bounded codes and hashes; they omit
chat text, actor/event IDs and local paths. This slice uses real candidate code
with authored synthetic data. Unit-test stand-ins are separate from the native
CLI integration checks. It is not live Twitch or real-data end-to-end acceptance.
