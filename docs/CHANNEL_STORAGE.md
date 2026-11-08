# Registered synthetic channel export storage

`ingest-channel` connects the actual task-19 offline export normalizer to the
actual task-4 channel archive. This independent opt-in CLI does not start the chat
runtime, network, model, Twitch transport or output adapter. It is not loaded on
the existing 8775 API and does not feed its channel search/answer path.

```powershell
uv --offline run --no-sync python -m ssr_chat ingest-channel --config <pipeline.json> --state-dir <dedicated-test-child> --input <synthetic-export-alias-batch.json>
```

Input contains exactly `request_id`, `synthetic: true`, and `exports`, a list of
up to 20 host-registered aliases. No text, identity, scope, policy, layer, mode or
URL overrides are accepted. Each registered bundle contains at most 20 Records.
The input JSON limit is 1 MB, with duplicate keys and nonfinite values rejected.
Exit 0 means stored/idempotent items or an empty request, 1 means held/uncertain
items, and 2 means invalid input/configuration. A native `no_match` remains
explicit and is never counted as stored. Commits are per Record, not atomic over
the selected exports. Mixed outcomes remain partial.

## Binding and independent policies

Configuration contains exactly `schema_version: "1.0.1"`, `mode:
"synthetic_channel_ingest_only"`, `python`, `importer_repo`, `store_repo`,
`importer_config`, `store_context`, `purpose_policy`, `state_root`, `binding`,
and `exports`. The binding fixes a synthetic source ID, original actor (explicit
null allowed) and all six scope fields. Domain is strictly `channel_knowledge`;
synthetic platform and channel must be explicit. Other dimensions may be exact
null, which never means all values. There is no chat-history or cross-channel
fallback. `fixtures/channel_storage/contract.synthetic.json` shows the authored
fixture shapes; synthetic policy examples do not prove real-source permission.

Task-19 requires caller/config/source/Record read visibility and an explicit
`read_policy_allowed` decision. The wrapper checks these and the separately bound
`local_cache` purpose before creating cached text. Missing, unknown or denied
permissions stop without spooling. Read permission is not storage permission.
The actual normalizer checks its own boundary again using manual mode, one
configured source and the fixed relative `export.json` path.

Task-4 has independent trusted caller and source policy. The caller is write-only,
with raw write layer and ingest capability only; current source `allow_ingest`
and write visibility are rechecked by task-4. Read/query, withdrawal and publish
rights are not granted. Only `raw_event` Records are accepted by this slice.
Draft/published statements, STT extensions and unrelated fields are rejected.
No existing review/publish policy is inferred or replaced.

## Evidence is not a complete Record

Task-19 returns Evidence without Record `schema_version`, `record_kind` or
`access_scope`. The adapter never invents those fields from Evidence. It verifies
every selected Evidence item against the first matching **original complete
Record** in the registered export, then forwards that original Record unchanged.
The selected identity/revision set must be complete and unique. Changed,
incomplete or extra-field Evidence is held before storage.

Task-19 deduplicates within the supplied export, preserving first receipt time
and raw revisions; same revision with changed payload refuses the import. The
wrapper preserves BOM, CRLF, whitespace, source timestamps, identity, visibility
and source references. Across exports and fresh processes, task-4 provides
persistent idempotency, immutable identity and revision conflict checks.

Task-4 keeps append-only raw revisions but native get/export presents the greatest
revision for each event before visibility and query filtering. It does not fall
back to older records when the latest is hidden. The integration checks that
behavior and separately inspects raw row counts in the owned synthetic test
database. That operator inspection is not a new history API or reader permission
for the write-only pipeline.

The native task-4 new-ingest acknowledgement is status `ok`, exit 0 and a null
reason; duplicate acknowledgement uses `idempotent`. Other successful reason
codes remain uncertain. Codes from the other storage candidates are not reused.
Store acknowledgements with readback Evidence are rejected rather than exposing
content to the write-only caller.

## Owned state and failure limits

Use a dedicated child of the configured synthetic state root. Foreign nonempty
directories are refused. Configuration and native code are pinned; owned importer
files are attested by file identity and hashes, protected by a process lock. The
SQLite file identity, configuration contract/storage version and full scope are
checked on resumption. Missing/replaced/truncated state stops without recreation
or automatic adoption.

Plaintext intentionally persists only in the owned synthetic export jobs, source
Results, forwarded Record files and `synthetic-channel.sqlite3`, plus the authored
fixtures. CLI summaries and purpose receipts contain bounded stages, statuses and
digests rather than raw text, actor, paths or backend errors. A separate explicitly
authorized synthetic reader is used by verification, not by this pipeline.

Importer output and later Record commits are not one transaction. A store refusal
can leave an authorized cached export, and earlier Records can remain committed.
There is no automatic retry. An uncertain acknowledgement remains uncertain;
explicit redelivery can confirm native idempotency. A hard crash before ownership
attestation requires review, and no exactly-once or privileged offline tamper-proof
guarantee is claimed. Real-source consent, platform-native exports, websites,
formal data, Twitch, publishing and the existing live service remain outside this
synthetic slice.
