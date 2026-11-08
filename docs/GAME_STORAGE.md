# Registered synthetic game snapshots

`ingest-game` joins the actual task-22 offline importer to the actual task-5
append-only game archive. It is an independent, opt-in CLI, not a new resident
service and not loaded on the existing 8775 API.

```powershell
uv --offline run --no-sync python -m ssr_chat ingest-game --config <pipeline.json> --state-dir <dedicated-test-child> --input <synthetic-alias-batch.json>
```

Input has exactly `request_id`, `synthetic: true`, and `snapshots`, a list of up
to 50 host-registered aliases. It accepts no URLs, text, scopes, ACLs, policies,
registries or identity overrides from the batch. Unknown aliases stop. The JSON
input limit is 1 MB; duplicate keys and nonfinite numbers are rejected. Exit 0
means every item was stored/idempotent or the batch was empty, 1 means a held or
uncertain item, and 2 means invalid input/configuration. Commits are per item.

The trusted configuration has exactly `schema_version: "1.0.1"`, `mode:
"synthetic_game_ingest_only"`, `python`, `importer_repo`, `store_repo`,
`store_policy`, `state_root`, `binding`, `importer_caller`, and `snapshots`.
`fixtures/game_storage/contract.synthetic.json` shows the authored fixture shapes.
Synthetic fixture approvals do not establish real-source consent.

## Authorization and scope

The binding fixes one synthetic source ID, exact `document:synthetic-...` or
`urn:synthetic:...` locator, actor (explicit null allowed) and complete scope.
`game_id` and `content_version` must both be explicit synthetic identifiers;
there is no latest-version or cross-game fallback. Every snapshot must have the
same binding and be `synthetic`, `local_file`, `utf8`, with the fixed relative
path `source.txt`. HTTP locators, Collector exports and arbitrary files are
outside this slice.

Snapshots contain host-authored text and the native registered-source object,
including original event IDs, Record ID, integer revision, event/receipt times,
quality flags and exact source reference. The adapter does not invent these
values, normalize text, infer authors, summarize, create draft/published layers,
or expand source rights.

Task-22 returns original text, so its exact caller and source **read and write**
rights are all required. Its source approval, existing local-cache purpose
approval and local robots decision must be explicit. The wrapper checks these
before creating any cached text, then the actual native importer enforces its
own policy again. Missing/unknown/denied purposes and permissions stop without
creating state. An ACL allow does not imply purpose consent.

Task-5 independently receives its own trusted source/caller policy. Its caller
is write-only and its capability is ingest only. No readback evidence from a
store acknowledgement is accepted. Search indexing, AI input/training, query
answering and public output are never invoked. A separate explicitly authorized
synthetic reader is used only by integration verification.

The native task-5 acknowledgement codes are `inserted` and `idempotent`. Only
those codes with status `ok` and exit 0 confirm stored/duplicate outcomes. An
unrecognized successful acknowledgement remains uncertain; codes from other
storage components are not substituted.

## Handoff and persistence

Each registered alias gets an owned persistent importer job containing the exact
UTF-8 source, registry/query and task-22 handoff. BOM, CRLF, whitespace, source
timestamps and policy provenance are preserved. Before calling task-5 the wrapper
checks the handoff against the registered Record, content SHA-256, stable-payload
SHA-256, source/locator, acquisition snapshot and returned Evidence. Handoff time
is not substituted for event time.

Repeated use of an alias reuses task-22's existing handoff. Each alias has its own
native handoff history; task-5 provides persistent event/revision conflict and
idempotency checks across aliases, revisions and process restarts. Equal text
under different event IDs stays distinct. Raw game get/export retains revisions;
it does not apply STT current/final semantics.

Use an empty dedicated child of the configured state root. State is bound to
configuration and pinned native code, guarded by a process lock. Importer files
are attested by file identity and SHA-256; missing or changed handoffs stop without
recreation. The SQLite identity, application ID, version and schema are checked
before resuming. Missing/replaced/truncated owned archives stop. Foreign nonempty
directories are not adopted. Code/policy changes require explicit new owned state
or review; the adapter never resets existing state automatically.

Only synthetic content is permitted here. Plaintext intentionally persists in the
owned registered-source jobs, handoffs and `synthetic-game.sqlite3`, as well as the
authored fixtures. Status/purpose receipts and CLI summaries contain bounded
status and digests rather than content or backend output. Store acknowledgements
are not claimed successful when the outcome is uncertain; there are no automatic
retries. Explicit redelivery can ask the native programs to confirm idempotency.

An importer/store chain is not one atomic transaction: a valid handoff can remain
after a store refusal. A hard crash before ownership attestation can require
manual review; no exactly-once or tamper-proof certification is claimed. This
slice does not fetch real sites, verify real robots/consent, write a formal
catalog, connect Twitch, or feed the existing game search/answer path.
