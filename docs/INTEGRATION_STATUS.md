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
