# SUNI Enterprise Memory Governance — Design Proposal

**Status:** proposal · **Author:** SUNI dev session · **Scope:** enterprise multi-user memory

SUNI already runs two memory scopes today — a private per-user store
(`memory/users/{id}/…`) and one shared global store (`collective_memory.json`),
both injected into context, plus a scope-tagged document KB. This document
designs the **governance layer** that makes an *organizational* memory (built
from user activity) safe: who may contribute to it, who may read it, and how
every such action is audited.

---

## 0. Guiding principle

**One retrieval engine, scoped by identity + clearance.** "Personal assistant"
and "enterprise assistant" are the *same* engine with a different scope filter —
not two systems. Global promotion is **default-deny**: anything uncertain never
becomes silently queryable.

---

## 1. The two load-bearing decisions

| Decision | Options | Recommendation |
|---|---|---|
| How memory becomes global | fully-automatic / fully-manual / **hybrid** | **Hybrid** — auto-extract *candidates* → classify + redact → governance gate → live. Auto alone leaks; manual alone doesn't scale. |
| How global memory is read | uniform / **clearance-scoped per entry** | **Clearance-scoped**, starting **coarse** (`org` / `department` / `restricted`) before fine-grained ACLs. |

---

## 2. Data model — governance metadata

Today a `MemoryStore` entry is `{id, content, e16, type, timestamp}`. A **global**
entry additionally carries:

```
provenance:  {source_type: user|doc|conversation, source_user_id, source_ref, extracted_at}
visibility:  org | dept:<name> | role:<name> | restricted     # who may read it
sensitivity: normal | pii | confidential                       # from classification
status:      candidate | approved | rejected | redacted
review:      {approved_by, approved_at, policy_version}
contributors:[user_id, …]                                      # corroboration
cluster_id:  <dedup / reconciliation key>
```

**Storage change:** move global memory from `collective_memory.json` to
`memory/global_memory.db` (SQLite). Governance needs queryable metadata, status
filtering, and provenance joins a flat JSON can't do. Embeddings live alongside
(blob column) or in a FAISS sidecar; vector search stays numpy-cosine at Phase 1
scale. This is where "move global memory to a real store" earns its keep — for
**governance**, not just ANN speed.

---

## 3. Promotion pipeline (staging → gate → live)

```
A. Extract   (auto, per-user)  existing consolidator extracts facts per user;
                               add an "org-relevant?" flag (heuristic/classifier)
B. Classify  (auto)            PII/secret scan (reuse benchmark PII regexes +
             & redact          a sensitivity classifier) → set sensitivity +
                               proposed visibility; quarantine confidential
C. Gate                        policy auto-approves low-sensitivity org-general
                               facts; anything sensitive → candidate review queue
D. Live                        approved entries become queryable with labels
```

Every transition writes an audit event (§6).

---

## 4. Retrieval scope + ACL filter

`build_context` already has `user_id`; add the caller's **clearance set** (from
role/dept). Retrieval:

- always: the user's private store
- global: `status = approved AND visibility ∈ caller.clearance`
- merge, rank across the comparable-embedding pool, **threshold weak matches**

**Enterprise-assistant mode** = the same call with a service/org identity and
broad clearance — still bounded, never sees `restricted`.

---

## 5. Provenance & revocation

Every global entry links to its source. Revocation triggers — user offboarded,
doc retracted, fact corrected, manual takedown — run a `revoke(source_ref)`
sweep that **soft-deletes** derived entries (kept for audit, dropped from the
query index). Hook into the existing consolidator scheduler for periodic
reconciliation.

---

## 6. Audit — extend the existing system

The current `audit.db` (append-only SQLite; `purge_old` / `export_csv` /
`stats`; admin Audit tab) is the right backbone — **extend, don't replace**.
New memory-governance event types:

```
memory.promote.candidate / .approved / .rejected
memory.redact          (what, why)
memory.access.denied   (clearance mismatch)
memory.revoke
memory.access          (restricted-entry reads only, or sampled)
```

Each event: actor, target entry id, provenance, visibility, policy_version,
timestamp. The Audit tab gains an event-category filter; a new **Memory
Governance** view surfaces the review queue + provenance trail. This turns the
audit into the compliance story: who promoted what, who saw what, why something
was redacted.

---

## 7. Injection safety

Activity-aggregated memory is the highest-injection-risk source. The
`[MEMORY-UNTRUSTED]` markers (already shipped) are a prerequisite; the step-B
classifier also flags injection-shaped content ("ignore previous instructions",
"you are now…") and quarantines it.

---

## 8. Phasing

Each phase is independently shippable and testable.

- **Phase 1 — Foundation.** Governance metadata (in the existing `MemoryStore`
  `metadata` field) + audit event types + coarse retrieval ACL (`org` vs
  `restricted`). Promotion stays **manual** (existing endpoint) but now
  **gated, deduped, audited**. Storage stays JSON+numpy — the SQLite migration
  is **deferred to Phase 3** (it only pays off at scale / for the review-queue
  and revocation joins). → Safe shared org memory, *no* auto-aggregation yet.
  **Low-risk first ship.**
- **Phase 2 — Pipeline.** Auto candidate extraction + classify/redact + admin
  review queue (Memory Governance tab). → The "aggregated from activity"
  capability, behind a gate.
- **Phase 3 — Scale/ACL.** Fine-grained dept/role clearance, revocation sweeps,
  contradiction reconciliation, dashboards.

---

## 9. Open decisions

1. **Clearance source.** RBAC today is *capability*-based
   (`read-only`/`standard`/`power`/`admin`), not *org-structural*. Enterprise
   needs a **department/team** dimension — extend the user model?
   *Recommendation: defer to Phase 3; Phase 1 uses coarse `org`/`restricted`
   derived from role.*
2. **Auto-approve threshold.** *Recommendation: default-deny early — anything
   uncertain → manual review — loosen as the classifier proves out.*
3. **Read-audit granularity.** *Recommendation: log `restricted`-entry reads +
   sampling, not every global read (too heavy).*

---

## 10. Phase 1 implementation sketch (reuse `MemoryStore`, defer SQLite)

- **`MemoryStore.search(..., scope=None)`** — add an optional candidate-stage
  predicate. **Critical:** scope filtering happens *before* top-k selection, not
  after — filtering the returned top-k would silently drop relevant in-clearance
  entries whenever out-of-clearance entries rank higher. Default `None` keeps
  per-user private stores byte-for-byte unchanged.
- **Governance metadata** lives in the existing per-entry `metadata` dict:
  `{visibility, sensitivity, status, provenance, review}`. Legacy collective
  entries (no metadata) default to `status=approved, visibility=org`, so no data
  migration is needed.
- **Clearance mapping** (`rbac.clearance_for_role`): `admin`/`power-user` →
  `{org, restricted}`; `standard`/`read-only` → `{org}`. Threaded from the chat
  endpoint through `run` → `_safe_run` → `build_context` (same plumbing as
  `response_language`).
- **`build_context(query, top_k, user_id, clearance)`** — builds the scope
  predicate (`status==approved AND visibility ∈ clearance`) and passes it to the
  collective store's `search`.
- **Promotion endpoint** (`/api/memory/promote`) — dedup against existing global
  entries (cosine), set provenance/visibility/status, write an audit event.
- **`audit.py`** — `log_memory_event(...)` helper + memory-governance categories.
- **Admin** — minimal for Phase 1 (governance UI is Phase 2); surface the new
  audit categories in the existing Audit tab filter.

**Known limitation (not solved in Phase 1):** the promote path does a JSON
read-modify-write with no lock, so concurrent promotes could clobber. Rare at
manual/admin scale; revisited with the SQLite move.
