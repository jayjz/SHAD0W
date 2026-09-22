# Integration status

Filled by Issue #3. This stub records the branch snapshot at sprint start.
Do not update `AGENTS.md` until Issue #5.

## Refs at 2026-09-21

| Ref | SHA | Role |
| --- | --- | --- |
| `main` / `feat/p5a-2-durable-journal` | `8e5e6f9e94dd47a7ca2a209e73d6019d51ac201f` | Stale published tip (P5A.2A journal foundation) |
| `feat/p5a-paper-canary` | `2b59d315f3d874570467ee2cc5f70820cc916fd2` | Paper canary + localhost feed relay |
| `feat/live-shadow-warm-start` / `feat/p5a-crypto-paper-foundation` | `5902ba31d54f7e0d290d7f82331895cc03fdd657` | Identity align + warm start |
| `feat/crypto-market-data-foundation` | `01b6fc33f6806095859bb161c649d438c5774e66` | Operational/data spine |
| `fix/p1b-content-integrity` | `9aec707ba48d497177fafd1d12019c4364871e00` | Historical sealed P1B |
| `integ/p5a-reconcile-canary` | created from `01b6fc3` | Convergence working branch |

## Issue #3 must complete

### Ancestry table

| Ref | Ahead/behind spine | Merge base | Unique commits | Classification |
| --- | --- | --- | --- | --- |
| | | | | |

### Each unique P1B commit

| SHA | Purpose | Paths touched | A/B/C/D | Port whole / port paths / superseded / investigate |
| --- | --- | --- | --- | --- |
| | | | | |

- File-level conflict map: P1B vs spine vs canary vs `main`.
- Classify each conflict A/B/C/D per `docs/SPRINT_CLEANUP.md`.
- Packages unique to one line.
- Whether P1B evaluation depends on paper journal / client-ID (expected: no).
- Isolated test results on each tip. Separate environment failures from product failures.
- Presence/absence of recorded canary artifacts (paths only; no secrets).
- Whether `docs/P5A_CANARY_RUNBOOK.md` exists on the spine.

### Proposed P1B port manifest (required output of #3)

```
PORT:
- ...
KEEP FROM SPINE:
- ...
MANUAL RECONCILIATION:
- ...
DO NOT PORT:
- ...
CLASS D:
- none
```

Do not begin Issue #4 until this manifest exists and tip checks pass or are classified environment vs product.

## Capability claims

Do not update `AGENTS.md` phase text until Issue #5 audit is done.
`main` documentation still describes unimplemented broker execution.
The spine has later paper/canary/crypto work. Treat that as unverified until
audited against the contract.

## Identity chain (not yet implemented)

Required before any *proof* canary:

- `sealed_study_id` (historical `9aec707` remains immutable)
- `final_release_id` / reproduction study id on this branch
- candidate fingerprint
- strategy config fingerprint
- execution config fingerprint
- economics fingerprint
- code revision
- implementation versions
- immutable deployment manifest

Paper preparation must reject: alternate candidate, altered thresholds,
altered execution assumptions, wrong final release, missing sealed release,
wrong code revision, altered manifest.
