# Integration status

Issue #3 inventory. Completed 2026-09-21. No source, test, or contract files were modified.
Do not update `AGENTS.md` until Issue #5.

## 1. Snapshot

Verified after `git fetch` against `origin` (2026-09-21).

| Ref | Tip SHA | Date (author) | Role |
| --- | --- | --- | --- |
| `origin/main` | `8e5e6f9e94dd47a7ca2a209e73d6019d51ac201f` | 2026-09-20 | Published tip; same as `feat/p5a-2-durable-journal` |
| `origin/feat/p5a-2-durable-journal` | `8e5e6f9e94dd47a7ca2a209e73d6019d51ac201f` | 2026-09-20 | Alias of `main` |
| `origin/feat/p5a-paper-canary` | `2b59d315f3d874570467ee2cc5f70820cc916fd2` | 2026-09-21 | Linear ancestor of spine |
| `origin/feat/live-shadow-warm-start` | `5902ba31d54f7e0d290d7f82331895cc03fdd657` | 2026-09-21 | Linear ancestor of spine |
| `origin/feat/p5a-crypto-paper-foundation` | `5902ba31d54f7e0d290d7f82331895cc03fdd657` | 2026-09-21 | Same SHA as warm-start |
| `origin/feat/crypto-market-data-foundation` | `01b6fc33f6806095859bb161c649d438c5774e66` | 2026-09-21 | Operational/data spine |
| `origin/fix/p1b-content-integrity` | `9aec707ba48d497177fafd1d12019c4364871e00` | 2026-09-20 | Historical sealed P1B |
| `origin/integ/p5a-reconcile-canary` | `37f53cb539b815178591758961a85dfb74d5713d` before this commit | 2026-09-21 | Spine + two approved docs commits |

Sprint-start identities match. Integration branch contains only the two planning-doc commits on top of `01b6fc3`. History was not treated as unreviewed.

## 2. Ancestry

Operational spine for ahead/behind: `01b6fc3` (`feat/crypto-market-data-foundation`).

| Ref | Merge base with spine | Ahead | Behind | Unique commits vs spine | Topology |
| --- | --- | --- | --- | --- | --- |
| `main` / `feat/p5a-2-durable-journal` | `8e5e6f9` | 0 | 13 | none | Strict ancestor of spine |
| `feat/p5a-paper-canary` | `2b59d31` | 0 | 6 | none | Strict ancestor of spine |
| `feat/live-shadow-warm-start` | `5902ba3` | 0 | 5 | none | Strict ancestor of spine |
| `feat/p5a-crypto-paper-foundation` | `5902ba3` | 0 | 5 | none | Same commit as warm-start |
| `feat/crypto-market-data-foundation` | `01b6fc3` | 0 | 0 | none | Spine |
| `integ/p5a-reconcile-canary` | `01b6fc3` | 2 | 0 | two docs-only commits | Spine plus approved integration docs |
| `fix/p1b-content-integrity` | `22dd85a002a4e80dbe8fbbd9248290480cf992ab` | 1 | 8 | `9aec707` only | **Diverges** after paper source-binding commit `22dd85a` |

Linear chain (oldest → newest operational):

`8e5e6f9` (journal foundation / main) → … → `22dd85a` (source bindings; **P1B fork point**) → canary `2b59d31` → warm-start `5902ba3` → crypto spine `01b6fc3` → integ docs `7b7fe21` → `37f53cb`.

P1B is the only genuine divergence: one commit on top of `22dd85a`.

## 3. Unique P1B commits

| SHA | Subject | Purpose | Paths | Class | Action | Reason |
| --- | --- | --- | --- | --- | --- | --- |
| `9aec707` | feat(research): add sealed P1B content integrity | Add frozen bar artifacts, sealed study / one-shot final release, P1B contracts and tests | See file list below | A for new research modules and P1B-only docs; C for overlapping prose and ADR number; B for leaving operational files untouched | PORT SELECTED PATHS |

Paths in `9aec707` vs merge base `22dd85a`:

- **Added:** `src/shadow/data/frozen.py`, `src/shadow/evaluation/sealed.py`, `tests/test_frozen_datasets.py`, `tests/test_sealed_study.py`, `docs/P1B_CRYPTO_INTEGRITY_CONTRACT.md`, `docs/P1B_CRYPTO_THREAT_MODEL.md`, `docs/decisions/0006-sealed-study-content-addressed-integrity.md`
- **Export wiring:** `src/shadow/data/__init__.py`, `src/shadow/evaluation/__init__.py` (additive exports only)
- **Prose:** `AGENTS.md`, `README.md`, `docs/ARCHITECTURE.md`, `docs/DATA_CONTRACTS.md`, `docs/EVALUATION.md`, `docs/PROJECT_STRATEGY_AND_ENGINEERING_ROADMAP.md`, `docs/RESEARCH_METHOD.md`

`9aec707` does **not** touch `src/shadow/execution/`, `src/shadow/risk/`, `src/shadow/application/`, journal schema, or client-order IDs.

## 4. Conflict map

Compared: P1B `9aec707` vs spine `01b6fc3`; P1B vs canary `2b59d31`; canary vs spine; `main` vs spine.

Canary vs spine is linear (Class B everywhere): crypto capture/replay/book, feed relay, paper preparation, journal v4 / source-opportunity v2. No P1B content on canary or spine.

`main` vs spine is the same 13-commit operational climb. Not a P1B conflict.

### P1B-only paths (absent on spine)

| Path | P1B role | Spine role | Semantic difference | Class | Action |
| --- | --- | --- | --- | --- | --- |
| `src/shadow/data/frozen.py` | Content-addressed frozen P0.1 bars | missing | new research storage | A | PORT |
| `src/shadow/evaluation/sealed.py` | Sealed study / final release | missing | new research procedure | A | PORT |
| `tests/test_frozen_datasets.py` | Frozen dataset tests | missing | | A | PORT |
| `tests/test_sealed_study.py` | Sealed study tests | missing | | A | PORT |
| `docs/P1B_CRYPTO_INTEGRITY_CONTRACT.md` | P1B integrity contract | missing | | A | PORT |
| `docs/P1B_CRYPTO_THREAT_MODEL.md` | P1B threat model | missing | | A | PORT |
| `docs/decisions/0006-sealed-study-content-addressed-integrity.md` | P1B ADR | missing; spine has a **different** `0006` | ADR number collision | C | PORT CONTENT under a new ADR number; do not overwrite spine `0006-crypto-microstructure-boundary.md` |

### Additive package exports

| Path | P1B | Spine | Class | Action |
| --- | --- | --- | --- | --- |
| `src/shadow/data/__init__.py` | re-exports frozen types | fingerprint + validation only | A | PORT selected lines (add frozen exports; keep any spine additions if present — none today) |
| `src/shadow/evaluation/__init__.py` | re-exports sealed types | historical + trades only | A | PORT selected lines |

### Overlapping prose both sides edited after `22dd85a`

| Path | P1B role | Spine role | Class | Action |
| --- | --- | --- | --- | --- |
| `AGENTS.md` | Adds P1B phase sentence + sealed-study working rule | Later operational phase text (stale vs code; evidence for #5) | C | KEEP SPINE for now. Do not port P1B sentences in #4. Issue #5 owns AGENTS. |
| `README.md` | P1B mention | Later paper/crypto mentions | C | KEEP SPINE in #4. Port P1B sentence after #5 or as a later docs commit. |
| `docs/EVALUATION.md` | P1B sealed procedure paragraphs | No P1B text; other evaluation text may have drifted | C | PORT P1B paragraphs only; three-way merge against `22dd85a` |
| `docs/RESEARCH_METHOD.md` | P1B limitations / procedure | spine edits after fork | C | PORT P1B paragraphs only |
| `docs/DATA_CONTRACTS.md` | P1B frozen-dataset identity notes | spine edits after fork | C | PORT P1B paragraphs only |
| `docs/ARCHITECTURE.md` | P1B one-line addition | spine edits after fork | C | PORT P1B paragraphs only |
| `docs/PROJECT_STRATEGY_AND_ENGINEERING_ROADMAP.md` | P1B milestone line | spine edits after fork | C | PORT P1B paragraphs only |

### Shared primitives (bytes compared `9aec707` vs `01b6fc3`)

| Path / symbol | Difference | Class | Action |
| --- | --- | --- | --- |
| `src/shadow/data/fingerprint.py` (`canonical_dataset_bytes`, `dataset_fingerprint`) | **identical** | C (shared) | KEEP SPINE / no edit; do not fork |
| `src/shadow/evaluation/manifest.py` (`ExperimentManifest`, `fingerprint`) | **identical** | C (shared) | KEEP SPINE / no edit |
| `EXECUTION_MODEL_ID` = `shadow.execution.quote_bid_ask.v2` | **identical** | C (bound by sealed studies) | KEEP SPINE / no edit |
| `EVALUATION_MODEL_ID` | **identical** | C | KEEP SPINE / no edit |
| `MEAN_REVERSION_STRATEGY_VERSION` | **identical** | C | KEEP SPINE / no edit |

### Runtime identity evolved on spine after P1B fork (not used by P1B code)

| Path | P1B (`9aec707` / `22dd85a`) | Spine | Class | Action |
| --- | --- | --- | --- | --- |
| `src/shadow/execution/opportunity.py` | `shadow.source-opportunity.v1` | `v2` adds `strategy_configuration_id` + `signal_type` | B | KEEP SPINE |
| `src/shadow/execution/journal.py` | `shadow.execution.journal.v3` / key v1 | `journal.v4` / key v2 | B | KEEP SPINE |
| `src/shadow/application/paper_canary.py` and later paper/crypto/relay files | older or absent | authoritative | B | KEEP SPINE |
| `pyproject.toml` scripts | live-data + canary | adds `shadow-paper-prepare`, `shadow-crypto-capture` | B | KEEP SPINE |
| `docs/P5A_*`, ADR 0005, runbook | older P5A prose on P1B tip | later operational docs | B | KEEP SPINE |

P1B research code does not import `ExecutionJournal`, client-order IDs, `SourceOpportunityKey`, paper dispatch, or broker account types. The v1→v2 source-opportunity change is therefore **not** Class D.

## 5. Package ownership

**P1B only:** `shadow.data.frozen`, `shadow.evaluation.sealed`, P1B docs listed above, two test modules.

**Spine only:** `shadow.application.paper_preparation`, `paper_canary` (newer), crypto application/adapters/domain/book, `shadow.operations.alpaca_feed_relay`, feed-relay tool, related tests, `docs/ALPACA_FEED_RELAY.md`, `docs/CRYPTO_MARKET_DATA_CONTRACT.md`, `docs/STATUS.md`, ADR `0006-crypto-microstructure-boundary.md`.

**Both (same or additive):** `shadow.evaluation` historical/manifest/trades, `shadow.data.fingerprint`/`validation`, `shadow.execution` (P1B uses only `EXECUTION_MODEL_ID`), `shadow.strategies.models`, `shadow.domain.market`.

## 6. Identity audit

### Does P1B depend on PAPER/runtime identity?

| Dependency | Result | File + symbol | Why | Class |
| --- | --- | --- | --- | --- |
| ExecutionJournal schema | NO | no import of `journal.py` from `sealed.py` / `frozen.py` / P1B tests | P1B commit does not touch execution package except reading `EXECUTION_MODEL_ID` | — |
| PAPER client-order ID mapping | NO | not referenced | | — |
| source-opportunity identity | NO | not imported | Spine later changed this identity; P1B correctness does not use it | B on spine |
| broker account identity | NO | not referenced | | — |
| P2A risk capability identity | NO | not referenced | | — |
| PAPER dispatch identity | NO | not referenced | | — |
| `EXECUTION_MODEL_ID` | YES (research implementation version) | `sealed.py` `_manifest_matches_plan` / `StudyPlan.execution_implementation_version` | Binds quote execution *model id* into the study, not journal identity | C, values identical |

**Proven from code:** P1B has zero authority over PAPER journal identity.

### Research identity-bearing fields (P1B)

Implemented in `src/shadow/evaluation/sealed.py` and `src/shadow/data/frozen.py`:

- frozen dataset: `FrozenBarDatasetRef` + SHA-256 of canonical P0.1 bytes (`shadow.data.fingerprint.canonical_dataset_bytes`)
- candidate / candidate set: sorted unique SHA-256 `candidate_fingerprints`; set identity = `fingerprint(tuple)`
- study plan: `StudyPlan` / `study_plan_id = fingerprint(self)`
- development selection: `DevelopmentSelection` / `development_selection_id`
- sealed study: `sealed_study_id`
- final release: keyed by sealed-study identity; no separate standalone fingerprint (ADR text)
- code revision: `StudyPlan.code_revision` (hex SHA-256 field)
- strategy configuration: candidate fingerprints + `MEAN_REVERSION_STRATEGY_VERSION`
- execution configuration: `execution_config_fingerprint` + `EXECUTION_MODEL_ID`
- economics configuration: `economics_config_fingerprint` or `fingerprint(None)`
- implementation versions: evaluation / execution / strategy model ids above
- `supersedes_study_id` already exists for reproduction/supersession linkage

Shared with spine: `fingerprint()`, `ExperimentManifest` fields (`bar_dataset_fingerprint`, `strategy_configuration_fingerprint`, `execution_configuration_fingerprint`, `economics_configuration_fingerprint`, `code_revision`), `EXECUTION_MODEL_ID`. Those primitives are currently byte-identical.

Live source opportunities are a **different** identity (`shadow.source-opportunity.v2` on spine). They must not be written into sealed study ids.

## 7. Isolated verification

Temporary worktrees. No merges. Interpreter: system Python 3.12.3. `pytest`/`ruff`/`mypy` from user pip (no `uv` in this environment). `PYTHONPATH=src`. `websockets` not required for the suites that ran.

| Ref | SHA | pytest | ruff check | ruff format | mypy | git diff --check | Kind |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `main` | `8e5e6f9` | 554 passed | pass | 83 files formatted | 65 files clean | clean | PRODUCT PASS |
| `feat/crypto-market-data-foundation` | `01b6fc3` | 912 passed | pass | 114 files | 90 files clean | clean | PRODUCT PASS |
| `fix/p1b-content-integrity` | `9aec707` | 648 passed | pass | 96 files | 74 files clean | clean | PRODUCT PASS |
| `feat/p5a-paper-canary` | `2b59d31` | 652 passed | pass | 96 files | 75 files clean | clean | PRODUCT PASS |

`uv run …` was not executed (`uv` absent). That is an environment limitation, not a product failure. Checks above are not recorded as `uv` PASS.

`integ/p5a-reconcile-canary` at `37f53cb` is spine plus markdown; test suite is the spine suite. Not re-run separately.

## 8. Canary evidence inventory

No recorded real PAPER canary artifact is tracked in Git on `main`, P1B, canary, spine, or integ.

Tracked related paths (code/docs only): `docs/P5A_CANARY_RUNBOOK.md`, `src/shadow/application/paper_canary.py`.

`.gitignore` on the spine excludes `.paper-journal.sqlite*`, `paper-canary-result.json`, `current-risk-decision.canonical.json`, `.paper-owner/`.

`docs/P5A_CANARY_RUNBOOK.md` **exists on the operational spine**. It labels the current path an early one-order PAPER integration probe, not final P5A.8, not broker-authoritative reconciliation. Secrets are named, not stored.

## 9. Class D findings

No Class D semantic conflicts identified.

The source-opportunity v1→v2 and journal v3→v4 changes after the P1B fork would be D only if P1B evaluation required those older representations. It does not.

## 10. Proposed P1B port manifest

PORT
- `src/shadow/data/frozen.py`
- `src/shadow/evaluation/sealed.py`
- `tests/test_frozen_datasets.py`
- `tests/test_sealed_study.py`
- `docs/P1B_CRYPTO_INTEGRITY_CONTRACT.md`
- `docs/P1B_CRYPTO_THREAT_MODEL.md`
- Additive exports in `src/shadow/data/__init__.py` and `src/shadow/evaluation/__init__.py`
- P1B paragraphs only (three-way vs `22dd85a`) in `docs/EVALUATION.md`, `docs/RESEARCH_METHOD.md`, `docs/DATA_CONTRACTS.md`, `docs/ARCHITECTURE.md`, `docs/PROJECT_STRATEGY_AND_ENGINEERING_ROADMAP.md`
- Content of `docs/decisions/0006-sealed-study-content-addressed-integrity.md` under a **new** ADR number that does not collide with spine `0006-crypto-microstructure-boundary.md`

KEEP FROM SPINE
- Entire `src/shadow/execution/` including `journal.py` and `opportunity.py`
- Entire `src/shadow/application/`, adapters, operations, crypto packages
- `pyproject.toml`
- `docs/P5A_*`, ADR 0005, `docs/P5A_CANARY_RUNBOOK.md`
- `AGENTS.md` and `README.md` during Issue #4
- Shared fingerprint/manifest/model-id modules (already identical)

MANUAL RECONCILIATION
- ADR number collision: two different ADRs both named 0006. Assign the next free number to the P1B ADR on port. Do not change P1B ADR substance.
- Three-way merge of the five overlapping research docs listed under PORT paragraphs.
- Confirm after port that `fingerprint`, `ExperimentManifest`, `EXECUTION_MODEL_ID`, and `MEAN_REVERSION_STRATEGY_VERSION` remain unmodified.
- Do not bind `SourceOpportunityKey` / live observations into sealed study identity.

DO NOT PORT
- P1B copies of paper/canary/journal/opportunity files (they are older, not P1B work)
- P1B `AGENTS.md` / `README.md` hunks (Issue #5)
- Spine `docs/decisions/0006-crypto-microstructure-boundary.md` must not be replaced

CLASS D
- none

## 11. Handoff

- Issue #4 may begin. Class D is none. Isolated checks on the four tips are product PASS.
- Unresolved Class C: ADR numbering; overlapping doc paragraph merges; shared fingerprint/manifest/model ids (verify unchanged, do not edit).
- Checks not completed as `uv run` (tool missing). Equivalent pytest/ruff/mypy/`git diff --check` ran.
- Next agent must not rediscover ancestry: P1B unique work is exactly commit `9aec707` on merge base `22dd85a`. Do not import P1B-era paper stack. Do not retarget sealed identity onto `SourceOpportunityKey`. Do not update `AGENTS.md` in #4. Do not create a new sealed study in #4 except the reproduction-identity *linkage field* if a study is re-executed later (#4 should port code, not re-seal a historical study).
