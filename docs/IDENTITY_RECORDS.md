# Synthetic raw person and platform-account records

`identity-records` connects explicit synthetic raw annotations to native task-6
init/source-configure, person-ingest, account-ingest, query, record-history and
record-get in an independent owned SQLite schema-3 store. It does not create
person/account links, infer identities from names, or call task-24. Manual
link/unlink and its human confirmation remain separate. The CLI is not loaded
on 8775 and adds no resident service.

```powershell
uv --offline run --no-sync python -m ssr_chat identity-records --config pipeline.json --state-dir identity-state/run --input request.json
```

Requests have `request_id`, `synthetic: true`, `operation`, and `events`
(registered ingest aliases) or `query` (registered query/history/get alias).
They cannot supply person/account IDs, annotations, source ACLs, paths, links or
approval flags. The trusted schema-1.0.1 config uses mode
`synthetic_identity_records_only`, fixed local Python/native paths, a full source
scope and explicit synthetic ID allowlists, immutable Record/source visibility,
separate write-only/read-only caller contexts, current host purpose/source
decisions, a dedicated root and declaration/query registries. The self-contained
fixture contract documents these shapes without depending on external test data.

Each raw annotation declares `person` with an explicit person ID and unknown
actor, or `platform_account` with an explicit stable actor account and no person
ID. A source/scope/payload-hash declaration binds the annotation and raw Record.
These are authored synthetic annotations, not verified real identities or human
account-link approvals. Equal display text never links, merges or changes IDs.
Native combined person/account queries stay `manual_link_not_configured`.

The native source is configured as synthetic with independent trusted visibility.
The first Record ACL must match it, and native event metadata/annotation stays
immutable across revisions. Native source ACL reconfiguration is not exposed.
Raw ingests use write-only caller access and return no original Record in the
receipt. Query/history/get use read-only callers, and native source/event read
ACL intersections remain effective. Original text, first receipt time, source
refs, revision and ACL are preserved; Evidence does not supply invented ACLs.

Task-6 has no native policy.uses interface. This host explicitly checks exact-bound
current source read/write decisions and `local_cache`; reads additionally need
`search_index`. Scope approval does not grant these uses, AI or publication.
The host does not claim to implement an unseen external current/snapshot consent
or review runtime. Source Record ACL remains a native immutable boundary; any
real-source bridge would still require its own verified use/snapshot policy.

Initialization occurs only inside a new owned state for an authorized synthetic
write; reads never initialize a missing store. Source configuration is explicitly
idempotent and checked before each raw write. Owned schema/file identity and
handoff hashes prevent adopting a foreign, missing, replaced or truncated store.
Ownership binds code/interpreter/source/scope/ID allowlists/visibility/root;
explicit trusted current permission changes are re-evaluated without replacing
raw data. Files changed during a request hold its output. Reads always invoke
native authorization and check unchanged source bytes rather than return an old
cache after revocation.

Raw revisions append, including late lower revisions; native current selection
uses the highest revision before time filters. History/get retain exact raw
Records. No raw deletion, withdrawal, link/unlink or publication is invented.
Revocation tests here mean read/write permission revocation; account-link
revocation belongs to task-24. Lost raw-write acknowledgments remain uncertain
without automatic retry; authorized native readback and explicit redelivery
confirm durable idempotency. No cross-file crash transaction, hostile-writer
atomic snapshot or privileged offline tamper protection is claimed.

Only owned synthetic SQLite metadata/file hashes are inspected directly; raw
readback uses native commands. Authorized cache files may contain synthetic raw
Records/results, while public summaries/receipts omit names, account/person IDs,
original text, source event IDs and paths. Unit native/schema stand-ins are
labeled separately from real candidate CLI integration. No real private data,
Twitch, model, microphone, outward message, repository, Git or real-data
end-to-end acceptance is involved.
