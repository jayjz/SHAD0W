# P1B cryptographic integrity contract

## Status

Implemented P1B contract for the sealed temporal study boundary.

This document defines identity and integrity requirements only. It does not define confidentiality, hostile-process security, digital signatures, remote attestation, trading authority, or statistical validity.

## Purpose

P1B provides deterministic integrity through content-addressed, tamper-evident,
fail-closed verification of the declared research relationships. It makes
research inputs and final evidence difficult to silently substitute after a
study has been declared or sealed.

The contract is intended to answer:

> Given a study artifact, can SHAD0W determine whether the datasets, candidate set, configuration, implementation identity, and final release still correspond to the exact evidence originally declared?

The answer must fail closed when the available evidence disagrees.

---

## Cryptographic primitive

P1B uses SHA-256 as a content-identification primitive.

SHA-256 is used to identify canonical bytes. It is not used as:

* encryption;
* authentication of the human researcher;
* authorization;
* proof that a file was created at a claimed historical time;
* proof that undeclared experiments never occurred;
* proof that a result is statistically valid.

An identity is meaningful only when the bytes being hashed are produced by a documented deterministic canonicalization process.

---

## Canonicalization rule

Never hash an arbitrary in-memory object representation.

The sequence must be:

```text
typed domain values
        ↓
validated canonical representation
        ↓
deterministic bytes
        ↓
SHA-256
        ↓
content identity
```

Canonicalization must have:

* explicit field semantics;
* deterministic field ordering;
* deterministic collection ordering where ordering is not itself meaningful;
* deterministic timestamp representation;
* deterministic Decimal representation;
* explicit schema/version identity;
* no ambient clock values;
* no random identifiers;
* no process-dependent serialization;
* no object memory addresses;
* no provider SDK objects.

Existing SHAD0W canonicalization utilities take precedence over new implementations.

---

## Frozen dataset identity

A frozen bar dataset is identified by the SHA-256 digest of its existing canonical P0.1 dataset bytes.

Conceptually:

```text
dataset_sha256 = SHA256(canonical_dataset_bytes)
```

The content identity must bind all identity-bearing dataset information already represented by the canonical artifact, including:

* observations;
* instrument identity;
* observation and availability timestamps;
* availability semantics;
* interval;
* values;
* provenance;
* dataset metadata;
* source;
* coverage;
* schema/version information.

A file path is not the source of truth.

The content digest is the source of identity.

Expected storage form:

```text
frozen-bars/
    <sha256-prefix>/
        <sha256>.json
```

Loading must verify:

```text
requested identity
        =
path identity
        =
SHA256(actual bytes)
        =
identity reconstructed from decoded canonical content
```

Any disagreement rejects the artifact.

---

## Candidate-set identity

A candidate set must not have a caller-controlled identity independent of its contents.

The canonical candidate set is:

```text
sorted unique candidate fingerprints
```

The candidate-set identity must be derived from that canonical value.

Conceptually:

```python
candidate_set_id = fingerprint(candidate_fingerprints)
```

Required invariant:

```text
candidate_set_id == fingerprint(candidate_fingerprints)
```

Prefer exposing `candidate_set_id` as a derived property rather than accepting it as independent caller input.

P1B implements `candidate_set_id` as that derived property. Persisted releases
retain it only as recomputed derived evidence; loading rejects disagreement.

Changing any candidate must produce a different candidate-set identity.

Adding or removing a candidate must produce a different candidate-set identity.

Reordering an already canonical set must not create an alternate semantic identity.

Duplicate candidates are invalid.

---

## Study-plan identity

A study plan identity must bind all material pre-final research declarations.

At minimum:

```text
hypothesis
limitations

train dataset identity
development dataset identity
final dataset identity

candidate-set identity
candidate fingerprints

selection rule declaration
selection metric declaration
selection tie-break declaration

execution configuration identity
economics configuration identity

quote currency
minimum descriptive sample requirement

code revision
feature implementation version
strategy implementation version
simulation implementation version
execution implementation version
evaluation implementation version

supersession metadata when present
```

The final dataset contents may be named by immutable reference, but final evaluation results must not participate in the StudyPlan identity.

The StudyPlan therefore represents a pre-final declaration.

---

## Development-selection identity

DevelopmentSelection records and binds:

```text
study_id label
selected candidate fingerprint
development evaluation manifest
selection evidence declaration
```

Its standalone identity does not include a StudyPlan identity. The sealed-study
identity binds the full StudyPlan together with the DevelopmentSelection, and
sealing verifies that their study labels agree.

The selected candidate must be a member of the declared candidate set.

The development manifest must correspond to the P1A-exposed identity fields:

* the development dataset;
* selected strategy configuration;
* execution configuration;
* economics configuration;
* code revision;
* feature implementation version;
* simulation implementation version;
* evaluation implementation version.

P1A has no independent manifest fields for strategy or execution implementation
versions. P1B therefore requires the plan's declared values for those two
versions to equal the currently executable local constants; it does not claim
that their provenance is independently established by the P1A manifest.

P1B currently binds a declared selection to matching development evidence.

Unless a deterministic selection evaluator is explicitly implemented, P1B does not claim that `selection_rule`, `selection_metric`, and `selection_tie_break` were independently executed or verified by SHAD0W.

That limitation must remain explicit.

---

## Sealed-study identity

A sealed study is the canonical identity of:

```text
StudyPlan
+
DevelopmentSelection
```

Conceptually:

```python
sealed_study_id = fingerprint(
    (
        study_plan,
        development_selection,
    )
)
```

A sealed study is immutable.

Any material change requires a new sealed-study identity.

Examples:

```text
different selected candidate
different dataset
different candidate set
different code revision
different execution assumptions
different criterion
different implementation version
```

must produce a different identity.

---

## Final-release identity and authority

A final release may exist only for an already sealed study.

Before release, P1B must verify that the final evaluation manifest corresponds exactly to:

```text
sealed final dataset
sealed selected candidate
sealed execution configuration
sealed economics configuration
sealed code revision
sealed feature, simulation, and evaluation implementation versions
sealed quote currency
```

Strategy and execution implementation-version declarations remain identity-bound
in the StudyPlan and must equal the currently executable local constants, with
the P1A-manifest limitation stated above.

No semantic-equivalence fallback is permitted where the contract requires an exact fingerprint.

---

## One-shot release

There is exactly one canonical final-release artifact per sealed-study identity.

Storage must be non-overwriting.

Required behavior:

```text
first creation
    → succeeds

second creation
    → rejects

equivalent second creation
    → rejects

different second creation
    → rejects
```

The original artifact must remain unchanged.

A later research attempt requires a new StudyPlan and therefore a new sealed-study identity.

---

## Artifact verification

Persisted final releases contain derived information.

Loaders must not trust that information merely because it was stored.

Where deterministic recomputation is possible, recompute and compare.

Examples:

* sealed-study identity;
* candidate-set identity;
* candidate membership;
* dataset identities;
* final disposition;
* descriptive summary;
* manifest correspondence.

A stored derived field that disagrees with recomputation invalidates the artifact.

---

## Supersession

Supersession does not mutate history.

A new study may declare:

```text
supersedes_study_id
supersession_reason
```

This creates a new study.

It does not:

* overwrite the superseded study;
* inherit its final-release authority;
* delete its evidence;
* revive its final holdout;
* reset its one-shot release invariant.

---

## Explicit non-goals

This contract does not establish:

* signed provenance;
* trusted timestamps;
* proof of authorship;
* proof that a Git commit existed at a historical wall-clock time;
* tamper resistance against an attacker with full filesystem and code control;
* confidentiality;
* encrypted storage;
* remote attestation;
* hostile multi-user security;
* proof that undeclared experiments never occurred;
* statistical significance;
* strategy profitability;
* evidence of a durable trading edge.

P1B provides deterministic content integrity and internal evidence consistency
within the declared research protocol. It is content-addressed, tamper-evident,
and fail-closed, not a claim of authentication or historical attestation.

---

## Required invariant

The central P1B cryptographic invariant is:

> A material research change must either produce a new identity or cause verification to fail.

No material mutation may silently retain the authority of an earlier sealed study.
