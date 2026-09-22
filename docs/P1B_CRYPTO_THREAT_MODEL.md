# P1B cryptographic integrity threat model

## Scope

This threat model covers the content-addressed evidence introduced by P1B.

It concerns accidental or opportunistic research-evidence mutation.

It does not claim security against a fully compromised host or malicious researcher with unrestricted access to code, filesystem, Git history, and execution environment.

---

## Assets

P1B protects the integrity relationships among:

* frozen historical datasets;
* temporal partition declarations;
* candidate configuration sets;
* development selection;
* code and implementation identities;
* sealed-study identity;
* final evaluation manifest;
* final descriptive result;
* final-release artifact.

---

## Threats in scope

### Dataset mutation

An observation or metadata field is modified after freezing.

Expected response:

```text
SHA-256 mismatch
→ reject
```

### Path substitution

Valid bytes are moved beneath a path naming another content identity.

Expected response:

```text
path identity != actual content digest
→ reject
```

### Metadata substitution

The caller presents a FrozenBarDatasetRef whose metadata does not correspond to the canonical stored artifact.

Expected response:

```text
reference != reconstructed reference
→ reject
```

### Temporal partition substitution

Train, development, or final references are swapped, reused, overlapped, or reordered.

Expected response:

```text
partition contract violation
→ reject
```

### Candidate substitution

A configuration not declared in the StudyPlan is selected or evaluated as final.

Expected response:

```text
candidate ∉ candidate set
→ reject
```

### Candidate-set identity mismatch

The candidate-set identifier disagrees with the canonical candidate fingerprints.

Expected response:

```text
candidate_set_id != fingerprint(candidate_fingerprints)
→ reject
```

### Development/final substitution

A development manifest is presented as final evidence or vice versa.

Expected response:

```text
manifest dataset identity != required partition
→ reject
```

### Implementation substitution

The evaluation was produced under another:

* code revision;
* feature implementation;
* simulation implementation;
* evaluation implementation.

Expected response:

```text
manifest/version identity disagreement
→ reject
```

P1A exposes these fields in its manifest. It does not independently expose
strategy or execution implementation versions. For those declarations, P1B
requires equality with the currently executable local constants rather than
claiming manifest-based provenance.

### Final-result mutation

Persisted count, net result, disposition, selected candidate, criterion, manifest, or dataset reference is edited after release.

Expected response:

```text
stored artifact != deterministically reconstructed artifact
→ reject
```

### Release overwrite

A process attempts to replace an existing final release for the same sealed study.

Expected response:

```text
canonical path already exists
→ reject without modification
```

---

## Threats partially mitigated

### Accidental filesystem corruption

Digest and canonical reconstruction checks detect many forms of mutation.

P1B does not provide filesystem redundancy or recovery.

### Concurrent final release

Exclusive creation prevents two successful canonical final artifacts from being created at the same target path.

The implementation must preserve filesystem-level non-overwrite semantics.

### Researcher forgetting prior trials

Candidate declaration provides local study accounting.

It cannot establish that no experiments occurred outside the declared study.

A future committed trial ledger may strengthen this boundary.

---

## Threats explicitly out of scope

### Malicious repository maintainer

A maintainer able to change:

```text
source code
tests
Git history
artifacts
and validation logic
```

can construct a different software system.

P1B does not defend against this attacker.

### Cryptographic key compromise

P1B currently uses no signing keys.

There is therefore no key-compromise claim.

### Authentic historical timestamp

A SHA-256 digest proves content identity, not when the content first existed.

A future transparency log, timestamping authority, signed commit, or independently witnessed ledger would be required for stronger temporal attestation.

### Confidentiality

Artifacts may contain research evidence.

P1B does not encrypt them.

### Malicious provider

P1B preserves supplied provenance but cannot prove a provider's market data was true.

### Statistical manipulation

Cryptographic integrity cannot determine whether:

* the hypothesis is economically meaningful;
* candidate generation was data mined;
* the chosen metric is appropriate;
* a result is statistically significant;
* a backtest represents future performance.

Those are separate research-method questions.

---

## Trust assumptions

P1B currently assumes:

1. SHA-256 is computationally collision resistant for this application.
2. Canonicalization code executes as reviewed.
3. The local Python process is not maliciously modified during execution.
4. The filesystem correctly implements the required exclusive-creation primitive.
5. Repository version identifiers supplied by existing manifests retain their documented meaning.
6. P0.1 validation semantics remain authoritative for frozen datasets.

---

## Security posture

P1B should be described as:

```text
content-addressed
tamper-evident
fail-closed
deterministic integrity
internally self-consistent
```

It should not be described as:

```text
tamper-proof
cryptographically authenticated
signed
externally attested
trusted-timestamped
hostile-process secure
```

---

## Future hardening

Potential later milestones may investigate:

* append-only trial ledgers;
* signed study manifests;
* Git commit/object verification;
* transparency logs;
* external timestamping;
* independent replication;
* remote artifact storage;
* Merkleized evidence collections.

None are required for P1B.
