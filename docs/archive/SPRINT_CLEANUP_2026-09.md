# SHAD0W proof-convergence sprint

Status: planning / inventory. No implementation merge to `main`.

Working branch: `integ/p5a-reconcile-canary`
Spine SHA at branch creation: `01b6fc33f6806095859bb161c649d438c5774e66`
(`feat/crypto-market-data-foundation`)
Historical P1B SHA: `9aec707ba48d497177fafd1d12019c4364871e00`
(`fix/p1b-content-integrity`)

There is one numbering system: GitHub issues #3–#10. Do not invent a
parallel "Ticket N" namespace.

## Diagnosis

SHAD0W's bottleneck is not missing architecture. It is uncollapsed
evidence-bearing histories. The sealed research artifact and the paper
execution path live on different refs. `main` is behind both.

## Decisions (approved 2026-09-21)

1. Spine = `01b6fc3`. Selectively port P1B onto it. Do not merge `main`
   forward first.
2. First remote change = this branch + sprint/inventory docs only.
3. Eight GitHub issues (#3–#10). One issue per agent session.
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

## Conflict classes (Issue #3)

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

## Issue order

```
#3 Inventory / conflict map
 ↓
#4 Port P1B
 ↓
#5 Audit P5A
 ↓
#6 Bind sealed study → deployment
 ↓
#7 Offline proof bundle
 ↓
#8 Prepare canary
 ↓
HUMAN OPERATOR GATE (not a GitHub issue; agents do not arm)
 ↓
#9 Reconciliation / recovery
 ↓
#10 Merge + retire refs
```

#8 may run as an *early integration probe* if labeled as such. It does
not close the portfolio claim. Operator execution sits between #8 and #9
as a gate, not a numbered ticket.

Do not begin #4 until #3 produces the explicit port manifest and all
relevant branch tips pass—or failures are documented as environment vs
product failures.

## Out of sprint

Crypto execution combined with equity paper. Live capital. New strategies.
LLM authority. Domain-type renames. Force-push. Squash of the convergence PR.
Updating `AGENTS.md` phase text before Issue #5.

## Agent session contract

1. Read `AGENTS.md`, this file, `docs/P5A_EXECUTION_CONTRACT.md`, ADR 0005.
2. `git fetch` and print SHAs of `main`, this branch, `feat/crypto-market-data-foundation`, `feat/p5a-paper-canary`, `feat/live-shadow-warm-start`, `fix/p1b-content-integrity`. If they moved unexpectedly, stop.
3. Work only on `integ/p5a-reconcile-canary`.
4. Work one GitHub issue per session. Prompt: "Work GitHub issue #N only."
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
