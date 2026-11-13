# Care Ops Agent

Multi-agent home care visit incident resolution: missed clock-in detection, replacement ranking, operator approval console.

Detects missed clock-ins and caregiver call-offs from a live visit stream, ranks qualified replacements, reaches them by (mocked) voice, and either auto-resolves high-confidence fixes or hands the proposed fix to a human operator. Every EMR write is approval-gated and idempotent. Operator overrides feed back into replacement ranking.

## Design

```
 visit stream (clock-in / call-off events)
        |
        v
 +----------------+   incident   +----------------+  ranked candidates  +----------------+
 | Detector Agent | -----------> | Ranking Agent  | ------------------> | Outreach Agent |
 | grace=15 min,  |              | skill-indexed  |                     | voice (mock),  |
 | deduped        |              | search, score: |                     | up to 3 tries  |
 +----------------+              | reliability +  |                     +-------+--------+
                                 | accept + dist. |                             | accepted?
                                 | + feedback     |                             v
                                 +----------------+                     +----------------+
                                                                        | Approval policy|
                    +----------------+   approved only   +--------------+ score >= 0.68 ? |
                    |  EMR Adapter   | <---------------- |  Operator    | auto : handoff |
                    | SQLite, UNIQUE |   idempotent      |  console     +-------+--------+
                    | idempotency key|                   | approve /    |         |
                    +----------------+                   | reject /     | pending |
                                                           | override ----+---------+
                                                           +----------------+         |
                                                                                    v
                                                          override bonus (+0.05) back
                                                          into Ranking Agent feedback
```

| Component | Implementation | Key property |
|---|---|---|
| Detector Agent | `DetectorAgent` in `src/care_ops/core.py` | 15-min grace, incidents deduped by hash id |
| Ranking Agent | `RankingAgent`, skill join-table with SQLite index | Interpretable score; operator feedback bonus per (caregiver, skill) |
| Outreach Agent | `VoiceCaller` | Mocked telephony, deterministic seeded acceptance, max 3 calls/incident |
| Approval policy | `Orchestrator.AUTO_SCORE = 0.68` | Hard constraints (skill, availability, voice acceptance) checked before score gate |
| EMR Adapter | `EMRAdapter`, SQLite stand-in | Write requires `status='approved'` + approver; `idempotency_key UNIQUE` makes replays no-ops |
| Operator console | React + Vite (`web/`), stdlib HTTP API (`server.py`) | Incidents, proposals, audit trail, EMR writes; approve / reject / override |
| Audit | `audit` table | Every agent action, call, approval and write is recorded |

## Run

```bash
make setup          # venv + pip install -e . + npm install (web)
make run            # API on http://127.0.0.1:8000/api/state  (seeded demo data)
make console        # React console on http://localhost:5173 (proxies /api)
```

CLI without make: `.venv/bin/python -m care_ops.cli serve|demo|bench`

## Test

```bash
make test
# .venv/bin/python -m pytest tests -q   -> 6 passed
# npm test --prefix web                 -> 2 passed (vitest)
```

## Bench

The benchmark simulates a synthetic visit stream (300 caregivers), resolves incidents end-to-end, then runs red-team cases that try to write to the EMR without approval (direct write, empty approver, unknown proposal).

```bash
.venv/bin/python -m care_ops.cli bench --incidents 2000 --redteam 400
```

Laptop sanity run (this repo, ~7 s wall clock):

```json
{
  "incidents": 2000,
  "auto_resolved": 1982,
  "handoff": 18,
  "unresolved": 0,
  "auto_resolution_rate": 0.991,
  "emr_writes": 1982,
  "redteam_cases": 400,
  "unapproved_emr_writes": 0,
  "audit_events": 11378
}
```

### Targets vs. measured

| Target | Measured | Note |
|---|---|---|
| 91% of 2,000 incidents resolved without operator hand-off | **99.1%** (1,982/2,000) | Synthetic pool is dense (300 caregivers, most skills covered, a candidate is usually near), so this is *easier* than a real roster — see Limitations. The policy, detector and write path are the same code the console uses. |
| Zero unapproved EMR writes across 400 red-team cases | **0** (400 cases × 4 attack shapes) | Enforced in `EMRAdapter.write_emr`, not just the API layer. |
| Operator overrides feed back into ranking | Yes | Override adds +0.05 bonus for (caregiver, skill), persisted; covered by test. |

Scale down if needed: `--incidents 200 --redteam 40` runs in under a second.

## Limitations (honest)

- **Synthetic data only.** Caregivers, distances, reliability and voice acceptance are generated; the 99.1% auto-resolution rate reflects that dense synthetic pool and will drop on a real, sparse roster. No real EMR, telephony, or PHI is touched.
- **SQLite is the EMR stand-in.** `EMRAdapter` isolates the write path (approval gate + idempotency) so a real FHIR/REST adapter can replace its internals, but no real EMR integration exists.
- **Voice is mocked.** `VoiceCaller` decides acceptance from the caregiver's synthetic `accept_rate` with a seeded RNG. No calls are placed.
- **Ranking is a hand-weighted linear score**, deliberately interpretable. There is no learned model; the only learning is the operator-override bonus.
- **Auto-approval is a policy threshold (0.68)**, not a safety certification. In production this gate, and who may approve, needs clinical/operational review, auth, and audit retention this demo does not provide.
- **Single-process, in-memory-by-default store, no auth** on the API/console. Demo-grade only.
- Nothing here is unfinished relative to the scope above; the items above are deliberate mocks/simplifications, marked `ponytail:` in code where they cut a corner.

## License

MIT — see [LICENSE](LICENSE).
