# ADR 0007: Content-addressed integrity for sealed temporal studies

* **Status:** proposed
* **Decision scope:** P1B research evidence only

## Context

P1A reconstructs deterministic historical evaluation evidence but does not prevent a researcher or later process from substituting historical datasets, changing the declared candidate set, changing implementation identity, or replacing a final result while continuing to refer to the same logical study.

P1B introduces frozen temporal partitions and one-shot final releases.

Those controls require deterministic identity semantics.

## Decision

Use existing canonical SHAD0W serialization and SHA-256 content identities to bind P1B research evidence.

Frozen datasets are persisted by the SHA-256 digest of their canonical P0.1 bytes.

Study, candidate-set, development-selection, and sealed-study identities are derived from deterministic typed evidence rather than mutable labels or filesystem paths. The canonical final-release artifact is keyed by its sealed-study identity and is deterministically serialized and revalidated; P1B defines no separate standalone final-release fingerprint.

The candidate-set identity must be cryptographically derived from the canonical candidate fingerprints. It must not be an independent caller assertion.

A sealed study binds:

```text
study plan
+
development selection
```

and excludes final evaluation results from the sealed identity.

The final evaluation may be released only when its manifest corresponds exactly to the sealed:

* final dataset;
* selected candidate;
* execution assumptions;
* economics assumptions;
* code revision;
* implementation versions.

One final artifact may be created for each sealed-study identity.

Existing final artifacts are never overwritten.

## Canonical identity rule

A material change must either:

1. produce a different identity; or
2. fail validation.

No material research change may silently retain an earlier study's authority.

## Selection-rule limitation

P1B records:

* selection rule;
* selection metric;
* tie-break declaration;
* selection evidence.

Unless a later milestone introduces a deterministic selection evaluator, P1B proves only that the sealed candidate was declared in advance and bound to the development evaluation.

It does not independently prove that the selected candidate was the winner of the declared selection procedure.

This limitation is intentional to keep P1B separate from parameter optimization and model selection.

## Consequences

Positive:

* frozen evidence becomes content addressed;
* dataset substitution is detectable;
* candidate substitution is detectable;
* implementation drift is detectable where existing manifests expose identity;
* final results cannot be overwritten under the same sealed identity;
* historical study artifacts can be reconstructed and independently checked.

Costs:

* materially changed studies require new identities;
* final releases are deliberately one-shot;
* stronger integrity increases explicit artifact/version bookkeeping;
* compatibility changes may require schema-version migration rather than mutation.

## Non-goals

This ADR does not introduce:

* encryption;
* signing keys;
* trusted timestamps;
* remote attestation;
* hostile-process security;
* proof of authorship;
* proof that undeclared research did not occur;
* optimization;
* statistical inference;
* benchmark comparison;
* evidence of trading edge.

## Alternatives rejected

### Caller-supplied study labels as identity

Rejected because a mutable label cannot prove that underlying evidence is unchanged.

### Mutable final-result files

Rejected because rewriting a final holdout after observation destroys the meaning of a sealed evaluation.

### Hashing arbitrary Python representations

Rejected because process- or serializer-dependent representations are not stable scientific identities.

### Automatically rerunning final evaluation after code changes

Rejected because implementation changes alter the experiment and require a new study identity.

### Adding digital signatures in P1B

Rejected as premature. Current requirements need deterministic integrity and tamper evidence, not a public-key trust infrastructure.

## Follow-up

A later research-integrity milestone may add a committed trial/search ledger and independently verified selection procedure.

Those additions should build on this identity boundary rather than weakening it.
