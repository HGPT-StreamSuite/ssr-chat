# Synthetic Twitch event archive

`POST /twitch/archive` is an opt-in, archive-only local test adapter. It accepts
the same synthetic EventSub notification shape as `/twitch/replay`, validates it
with the shared fixture validator, projects one minimal Record, then invokes the
actual task-20 normalizer and task-7 store through `ChatIngestPipeline`.
It does not call the query gateway, a model, or an output adapter.

Enable it only on an isolated loopback test API:

```powershell
uv --offline run --no-sync python -m ssr_chat serve --port 8776 --state-dir <test-api-state> --twitch-archive-config <archive.json>
```

Without the flag the archive route returns 503. Existing chat, query and replay
routes keep their behavior. The running 8775 service has not loaded this adapter.

## Trusted synthetic configuration

The configuration has exactly `schema_version` (`1.0.1`), `mode`
(`synthetic_twitch_archive_only`), `source_binding`, `storage_binding`,
`storage_pipeline`, `storage_state_dir`, and `fixture_receipts`.

`source_binding` fixes the subscription, broadcaster, reader and chatter IDs.
`storage_binding` must equal the referenced pipeline's binding. Its source ID is
`synthetic-twitch-<broadcaster_user_id>-<chatter_user_id>`, its platform is
`synthetic-twitch`, and its channel equals the broadcaster ID. The internal actor
and six-field storage scope come from this trusted binding. A transport reconnect
session never replaces the stored stream session.

Each allowed chat-message UUID needs a host-authored fixture receipt with exactly
`event_time`, `received_at`, and `semantics_sha256`. Both times must include a
timezone and received time cannot precede event time. The digest is SHA-256 of
UTF-8 JSON with sorted keys, compact separators and literal Unicode:

```json
{"broadcaster_user_id":"1001","chatter_user_id":"2001","message_id":"00000000-0000-4000-8000-000000000001","text":"authored synthetic text"}
```

The EventSub notification timestamp is used only for notification freshness.
It is not treated as chat creation time. The supplied fixture has fixed synthetic
times; refresh notification metadata when replaying it. Unknown message IDs and
content changes under an existing ID stop before storage. Receipt values are
test data, not authenticated Twitch timestamps or evidence of user consent.
See `fixtures/twitch/archive.synthetic.json` for the complete synthetic shape.

## Requests and storage

Requests have exactly `operation`, `session_id`, `synthetic: true`, plus
`envelope` for operation `notification`. Connect the chosen synthetic session
before submitting notifications; disconnect it before reconnecting with a new
session. The shared validator checks notification type/version, subscription,
websocket session, broadcaster, chatter, message UUID, fragments and freshness.
Foreign/shared-chat origins and messages from the bound reader are held.
Reconnect URLs are never followed. Identity and scope fields in the HTTP body
cannot override trusted configuration. This local control API has no Twitch
transport authentication and is unsuitable as a live ingestion endpoint.

Projection preserves the authored message text, message UUID, host-bound actor,
storage scope and receipt times. The Record is a synthetic `raw_event`, revision
1, with one `synthetic://twitch-replay/<broadcaster>/<message_uuid>/1` source
reference. Display names, badges, delivery metadata and unused fields are not
projected. The task-20 and task-7 contracts reject nonempty Record extensions;
receipt and policy digests remain in sidecar summaries rather than Record fields.

The independent pipeline retains its source and caller write ACLs, explicit
`local_cache` grant, code/configuration pins, owned state, file lock and durable
SQLite identity checks. Writing grants no reading, AI or public-output right.
Only an explicitly owned synthetic state directory may be used. Do not point
this adapter at private chat data or production databases.

Task-7's persistent event/revision key makes redelivery idempotent across new
notification IDs, transport reconnects and API restarts. Storage exceptions and
lost acknowledgements are reported as held or uncertain; the adapter never
automatically retries and never claims an unknown commit succeeded. A deliberate
redelivery can ask task-7 to confirm the existing revision. Continuity failures
retain the existing fail-closed behavior; there is no automatic reset.

HTTP replies expose bounded status, stage, outcome and hashes. They do not expose
message text, actor IDs, raw events, backend additions, or filesystem paths.
Pipeline receipts contain hashes and purpose decisions; plaintext exists only in
the isolated synthetic archive, apart from the authored input fixtures and test
verification exports. Successful and failed calls clean temporary spool files.

This adapter does not connect to Twitch, authenticate real events, generate real
event clocks, ingest private chat, answer stored messages, or send replies. The
synthetic storage test does not establish live Twitch end-to-end readiness.
