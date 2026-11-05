# Synthetic channel relationship and visit events

`channel-events` connects explicit synthetic source declarations to native task-26
`ingest`, `get`, and `export` in a separate process and owned SQLite database.
It does not change person/account raw records (task-6), call manual link/unlink
(task-24), infer identity or visits from chat, or connect to Twitch. Real account
links still require the separate manual workflow and human confirmation.
This CLI is not loaded on 8775 and does not add a resident service.

```powershell
uv --offline run --no-sync python -m ssr_chat channel-events --config pipeline.json --state-dir events-state/run --input request.json
```

Requests have `request_id`, `synthetic: true`, `operation`, and either `events`
(registered aliases for `ingest`) or `query` (a registered `get`/`export` alias).
They cannot supply policies, paths, person/account targets, facts, or approval
flags. The schema-1.0.1 config uses mode `synthetic_channel_events_only`, fixed
local Python/native paths, a synthetic source/actor/full-scope binding, immutable
Record visibility, separate trusted current writer/reader policies, a dedicated
state root, and explicit event/query registries. See the self-contained synthetic
fixture contract for complete policy and envelope shapes.

Each registry event carries the exact native `{record, facts, source_kind}`
envelope and a source declaration bound to its full payload hash/source/scope.
Declarations are authored synthetic test data, not real source verification or
human approval of an account link. Missing or changed declarations stop ingest.
Record text is not parsed into facts; explicit relationship type/status/effective
times or visit effective time remain native inputs. Missing facts are rejected,
not inferred. Unknown relationship status remains unknown.

Writer caller access is write only. Native receipt evidence must be empty, so
write permission cannot expose original text. Native task-26 checks caller write,
current source write, stored source/Record write visibility and local-cache
purpose. Reader access is read only, and native task-26 intersects its caller and
Query read with current and saved source/Record read visibility and search-index
purpose. The host separately requires local-cache permission before caching raw
handoffs/exports. AI grants are retained without being expanded or used.

The owner marker binds native code, interpreter, synthetic scope/source/actor,
Record visibility and dedicated root, and attests DB file identity/schema plus
handoff files. Explicit trusted policy updates do not reinitialize the DB: each
new process re-evaluates current policy and the native immutable first snapshots.
Changing policy during a request holds its result. Reads always call the native
program again; they never return an old authorized cache after revocation.
Current permission relaxation cannot exceed the native saved snapshot. There is
no permission-management endpoint, implicit policy update or schema migration.

Revisions append without rewriting original rows. Native idempotency retains the
first accepted receipt time. A source-declared revoked state is a new revision;
lower late revisions, time/status filters and replays cannot resurrect an older
active state. Full authorized JSONL export retains all revisions and exact
envelopes/facts; native `limit_reached` and journal sequence support explicit
registered paging. The adapter verifies evidence/exports against declared
originals and never constructs missing ACL from Evidence. It does not turn a
relationship or visit query into a statistic or link it to task-18.

Each invocation uses a new owned handoff directory, so exports never overwrite
an earlier file. A lost ingest acknowledgment remains uncertain without automatic
retry; explicit replay asks native durable idempotency to confirm the outcome.
Missing/replaced/truncated DB, foreign nonempty state or tampered handoffs are
held instead of adopted or recreated. Read checks verify unchanged database bytes
before returning output. This is continuity checking, not an atomic snapshot
against a hostile writer or protection against privileged offline tampering.

Only owned synthetic SQLite metadata is read for schema/file continuity. Native
Result/JSONL in the authorized cache may contain synthetic original content;
public summaries and receipts contain bounded codes/counts/hashes, no original
text, account/event IDs, facts or paths. Unit native/SQLite stand-ins are labeled
separately from real candidate CLI integration tests. No real private data, live
platform, outward message, model, microphone, repository or publication is used
or certified. Raw event corrections never remove the retained history.
