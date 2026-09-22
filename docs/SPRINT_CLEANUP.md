# SHAD0W proof-convergence sprint

Status: planning / inventory. No implementation merge to `main`.

Working branch: `integ/p5a-reconcile-canary`
Spine SHA at branch creation: `01b6fc33f6806095859bb161c649d438c5774e66`
(`feat/crypto-market-data-foundation`)
Historical P1B SHA: `9aec707ba48d497177fafd1d12019c4364871e00`
(`fix/p1b-content-integrity`)

## Diagnosis

SHAD0W's bottleneck is not missing architecture. It is uncollapsed
evidence-bearing histories. The sealed research artifact and the paper
execution path live on different refs. `main` is behind both.

## Decisions (approved 2026-09-21)

1. Spine = `01b6fc3`. Selectively port P1B onto it. Do not merge `main`
   forward first.
2. First remote change = this branch + sprint/inventory docs only.
3. Eight GitHub issues, one ticket per agent session.
4. Existing P1B sealed artifacts are immutable historical evidence.
   Integration reproduction does not overwrite their identity.
5. Final proof requires sealed-study → deployment-manifest → live-opportunity
   identity chain. Live observations are never part of the sealed study.
6. Early supervised paper canary ≠ final proof. Final proof requires
   broker-authoritative reconciliation.
7. Convergence PR uses coherent commits. Do not squash.
8. No branch deletion before merged proof of supersession.
9. No forced trade. No-trade is a valid canary outcome.
10. Runtime broker/journal evidence stays outside Git.

## Identity model agents must not collapse

```
sealed study
    binds strategy / config / implementation / economics / code revision
fresh live observation
    creates a new source opportunity (not sealed beforehand)
deployment manifest
    proves the live opportunity was evaluated by the exact sealed
    strategy/config/implementation
```

P1B has zero authority over paper journal identity.

## Conflict classes (Ticket 0)

| Class | Meaning | Action |
| --- | --- | --- |
| A | research-only | safe to port |
| B | paper-only | retain spine |
| C | shared identity primitive | manual reconciliation |
| D | semantic conflict | stop |

Stop only if P1B integrity semantics depend on an older journal or
client-ID representation that cannot be separated from research evaluation.
Ordinary branch age (older docs, older pyproject, evolved journal on the
spine) is not a stop.

Stop examples: different source-opportunity identity meaning; different
strategy fingerprint semantics.

## Sealed artifacts

Never rewrite an existing sealed identity.

- Historical study: code revision = `9aec707`.
- Reproduction: integration code revision = NEW_SHA. It reproduces
  historical study *semantics* and is a new sealed-study identity even if
  outputs are identical. Link it as reproduction/supersession. Do not
  pretend the integration commit is the same study.

## Ticket order

| # | Purpose |
| --- | --- |
| 1 | Inventory and semantic conflict classification |
| 2 | Port sealed P1B research integrity onto operational spine |
| 3 | Audit P5A implementation against execution contract |
| 4 | Bind sealed study identity to PAPER deployment |
| 5 | Offline end-to-end proof + mutation / negative controls |
| 6 | Prepare supervised Alpaca PAPER canary |
| 7 | Operator executes bounded canary (not an agent unless assigned) |
| 8 | Close broker reconciliation and recovery gaps |
| 9 | Merge proof-convergence branch; retire superseded refs |

Ticket 6/7 may run as an *early integration probe* if labeled as such.
They do not close the portfolio claim.

## Out of sprint

Crypto execution combined with equity paper. Live capital. New strategies.
LLM authority. Domain-type renames. Force-push. Squash of the convergence PR.

## Agent session contract

1. Read `AGENTS.md`, this file, `docs/P5A_EXECUTION_CONTRACT.md`, ADR 0005.
2. `git fetch` and print SHAs of `main`, this branch, `feat/crypto-market-data-foundation`, `feat/p5a-paper-canary`, `feat/live-shadow-warm-start`, `fix/p1b-content-integrity`. If they moved unexpectedly, stop.
3. Work only on `integ/p5a-reconcile-canary`.
4. One issue per session.
5. Canonical checks before commit: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests`, `git diff --check`.
6. End with files touched, tests run, invariants checked, what the next agent must not redo.

## Convergence commits (when implementation starts)

Do not squash.

```
chore(integration): establish proof-convergence baseline
feat(research): port sealed P1B integrity boundary
test(integration): verify research and paper paths coexist
feat(execution): bind sealed release to paper deployment identity
docs(status): reconcile implemented capability claims
```

Additional coherent commits for recon/canary gaps are allowed. History is
provenance.
