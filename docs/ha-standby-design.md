# SUNI High-Availability: Active–Passive Warm Standby (Design Sketch)

Status: **design — review before implementation.** No code yet.
Target topology: **1 active SUNI + 1 warm standby**, with the inference tier
(Ollama/vLLM) handled separately (see `project_vllm_backend` / capability-router
notes). This doc covers HA of the *SUNI application node only*.

## 1. Goals & non-goals

**Goals**
- Survive a crash/host failure of the single SUNI app instance with **seconds of
  downtime** and **near-zero loss of durable data**.
- A standby that **serves nothing** but continuously holds the primary's durable
  state, ready to be promoted.
- No rewrite: SUNI stays a single-process app; we add a mode flag + replication +
  a promotion mechanism.

**Non-goals (explicit)**
- Preserving *in-flight* requests across failover (active SSE streams, a pending
  approval `Future`, an in-progress OIDC handshake, the turn currently being
  generated). These are re-tried by the client. Preserving them would require
  externalizing session/approval state to Redis — a much larger refactor, not
  worth it for an interactive assistant.
- Active–active / horizontal scaling of the app tier (that is the "federation"
  build; a separate, larger effort).

**Targets:** RTO (downtime on failover) ≈ **5–30 s** (health-check interval +
promotion). RPO (data loss) ≈ **replication lag**, seconds to <1 min.

## 2. State classification (SUNI-specific)

The whole design hinges on this split.

### Durable — MUST be replicated to the standby
| Store | File(s) | Notes |
|---|---|---|
| Auth / users | `memory/users.db` | incl. OIDC identities |
| Conversations | `memory/conversations.db` | chat history (per-turn writes) |
| Audit + token usage | `memory/audit.db` | append-only |
| Contacts / projects / skills / monitor / bg_tasks | `memory/*.db` | SQLite |
| Episodic memory | `memory/suni_memory.json`, `memory/users/<id>/*.json` | atomic writes (tmp+rename) |
| Doc KB index | FAISS index + `doc_meta.json` | large, changes slowly |
| Config / governance | `suni_config.json`, `role_config.json`, `tool_policies.json`, `mcp_servers.json` | |
| Secrets (identity of the deployment) | `jwt_secret.txt`, `api_key.txt` | **must match** or all JWTs/API tokens break on failover |
| Ingestion cursors | `seen_email_uids.json`, `ingestion_state.json`, `article_ingest_state.json` | prevents re-processing after failover |

### Ephemeral — NOT replicated (lost on failover, re-created by client)
- `_sessions` (in-memory `Context` + `asyncio.Lock`) — rebuilt from
  `conversations.db` on reconnect.
- `approval._pending` (`asyncio.Future`s) — a waiting human approval; re-issued.
- `oidc._states` (in-flight PKCE login) — user re-clicks "Sign in".
- `health._breakers`, `usage` accumulator, `_user_memories` cache — rebuilt.

**Key property already in our favor:** SUNI's JSON stores write via
temp-file + atomic rename (`store.py`), so a file-level replica never sees a
half-written file. SQLite in WAL mode gives the same guarantee for the DBs.

## 3. Node roles: the `SUNI_ROLE` flag

A single environment variable / config key drives everything:

```
SUNI_ROLE = active | standby        (default: active — unchanged single-node behavior)
```

- **active**: serves traffic, runs all schedulers, is the sole writer.
- **standby**: replication target only. Serves **no** user traffic and runs
  **no** write/side-effecting schedulers. Exposes only a private
  `/internal/health` + `/internal/promote` endpoint.

`default = active` means every existing single-node install behaves exactly as
today — HA is strictly opt-in.

## 4. Scheduler gating (the critical correctness point)

If the standby ran the background workers, it would double-write the replicated
stores (corruption) and duplicate outbound side-effects (double emails/briefings).
So on `standby`, gate the write/side-effecting tasks at `server.py` startup
(currently unconditional `asyncio.create_task(...)`):

| Task (server.py:413–435) | On standby |
|---|---|
| `watch()` session→memory ingester | **OFF** (writes memory) |
| `watch_inbox()` email reader | **OFF** (writes cursors, may act) |
| `watch_documents()` doc scanner | **OFF** (writes index/meta) |
| `_monitor_mod.start_monitor()` news | **OFF** (writes monitor.db) |
| `start_briefing_scheduler()` | **OFF** (sends notifications) |
| `start_consolidation_scheduler()` | **OFF** (rewrites memory) |
| `_metrics_collector()` | OFF (local only, pointless on standby) |
| `_bhealth.start_monitor()` breaker probe | optional (read-only, harmless) |

Implementation: wrap the block in `if _role == "active":`. On **promotion**, the
standby restarts (or re-runs the startup block) as `active`, bringing the
schedulers up. Simplest: promotion = flip the flag + process restart.

## 5. Replication

Replicate the **Durable** set from active → standby continuously. Two platform
tracks, same target (RPO of seconds):

**Linux (recommended for the enterprise deployment)**
- SQLite: **Litestream** (streams WAL to the standby / object store; standby runs
  `litestream restore -continuous`). Per DB.
- JSON/FAISS/secrets: `rsync`/`lsyncd` (inotify-driven) or a shared/replicated
  volume. Atomic renames make this safe.

**Windows (current dev/deploy platform)**
- SQLite: enable WAL; ship either WAL frames or frequent **Online Backup API**
  snapshots (SQLite `.backup`) on a short timer; or VSS consistent snapshots.
- JSON/FAISS/secrets: **DFS-R** or scheduled `robocopy /MON` (monitors + mirrors
  on change).
- (Longer term, moving the JSON memory stores into SQLite would let *one*
  replication mechanism cover everything.)

**Secrets caveat:** `jwt_secret.txt` and `api_key.txt` must be **identical** on
both nodes (replicate once / bootstrap together) — otherwise every session token
and API token is invalidated the instant the standby takes over.

## 6. Failover flow

```
        ┌──────────── Failover authority (VIP / LB / keepalived) ───────────┐
        │  health-checks the ACTIVE every 5–10s on /internal/health          │
        └───────────────┬───────────────────────────────┬───────────────────┘
                  primary OK                       primary FAILS N checks
                        │                                 │
                   route to active                  1. FENCE old primary (below)
                                                     2. tell standby → promote
                                                        (flip SUNI_ROLE=active,
                                                         restart → schedulers up,
                                                         starts serving)
                                                     3. VIP now points at new active
                                                     4. clients reconnect (SSE),
                                                        re-submit in-flight turn
```

- **Health check**: a cheap `/internal/health` that confirms the process is up
  and the stores are readable.
- **Client behavior**: browsers already use SSE; add auto-reconnect + idempotent
  resend of the last unanswered turn so a failover reads as a brief hiccup.
- **What's lost**: only the in-flight turn / pending approval. Conversation is
  intact to the last persisted message.

## 7. Split-brain prevention (must-have)

If a network partition makes both nodes believe they are active, both write →
corruption. Guards:
- **Single failover authority** owns the decision (the VIP/keepalived holder or
  the LB). Only it promotes.
- **Fencing**: before promoting the standby, the authority must ensure the old
  primary is not writing — STONITH-style (kill/isolate the old host) or a
  **witness/lease**: the active must hold a renewable lease (a lock file on a
  shared witness, or a small consensus key); it may only write while the lease is
  valid. Standby promotes only after the lease provably expires.
- For a 2-node LAN deployment, **keepalived/VRRP + a fencing script** is the
  standard, proven answer. Do **not** hand-roll consensus.

## 8. Concrete SUNI touchpoints

1. `config.py` / env: add `SUNI_ROLE` (default `active`).
2. `server.py` startup (~413–435): wrap write/side-effecting schedulers in
   `if role == "active"`.
3. New router: `/internal/health` (both roles) and `/internal/promote`
   (standby→active; auth-restricted to the failover authority).
4. Request auth middleware: on `standby`, reject/redirect user traffic (serve
   only `/internal/*`).
5. Ops (outside the app): replication agents (§5), the VIP/keepalived config,
   the fencing script.
6. Frontend: SSE auto-reconnect + idempotent turn resubmit.

## 9. Phased plan (each phase independently valuable)

- **Phase 0 — MTTR reduction (do first, no standby):** supervised auto-restart
  (Task Scheduler restart-on-failure / a watchdog), external health monitor +
  alerting, scheduled backups (SUNI's backup subsystem). Turns "down until
  noticed" into seconds of self-heal. *Highest value per effort.*
- **Phase 1 — Continuous replication:** stand up the replication of the Durable
  set to a replica/object store. Now any fresh box can be restored to seconds-ago
  state (manual failover already possible).
- **Phase 2 — Warm standby + automatic promotion:** the `SUNI_ROLE` flag,
  scheduler gating, `/internal/*` endpoints, VIP/keepalived + fencing, client
  reconnect. Full active–passive HA.

## 10. Open decisions (for review)

1. **Target OS for the HA deployment** — Linux (Litestream/keepalived, easiest)
   vs Windows (VSS/DFS-R + WinFailover)? Changes the replication + fencing tools.
2. **RPO tolerance** — is "up to ~1 min of memory writes lost" acceptable, or do
   we need WAL-streaming (near-zero)? Conversation history is per-turn regardless.
3. **Consolidate JSON stores into SQLite?** Would unify replication under one
   mechanism (recommended before Phase 1 if we go Litestream).
4. **Manual vs automatic failover for v1** — automatic (VIP+fencing) is more work
   but real HA; manual promotion (a documented runbook) is a cheaper Phase 1.5.
5. Is **Phase 0 enough** for the near-term SLA, deferring the standby until a
   concrete uptime requirement exists? (Matches the "build HA when a deployment
   demands it" rule.)
