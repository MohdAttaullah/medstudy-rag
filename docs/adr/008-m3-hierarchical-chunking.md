# ADR-008: Deterministic hierarchical chunks over normalized source

Status: Accepted; implemented in M3. Date: 2026-09-06. See
[document chunking](../architecture/document-chunking.md) for the implemented contract and
[the M3 report](../verification/m3.md) for what was verified.

M3 consumes only active, successfully validated M2 ParseRuns. Raw Docling JSON is an inspection
artifact, not a second chunking input. Source text, identifiers, reading order, page geometry,
reliable ancestry and explicit artifact relationships determine construction. Borderline caption/
paragraph and margin labels alone do not determine boundaries or content identity.

Use the bundled MedCPT Article Encoder WordPiece tokenizer for local token measurement only.
Pin its revision, SHA-256 and tokenizers runtime. Do not download model weights, embed, or retrieve.
Keep exact normalized source slices separate from deterministic heading/table retrieval context.
Do not decode token IDs to construct source text: token offsets preserve symbols and capitalization.

Version ChunkRun policies by full content fingerprint. Parent/child, source-element spans, source
pages, artifact associations, question/options and neighbors have relational identities. Hash
immutable input/policy/source slices rather than random run UUIDs. Forced runs get new row IDs but
identical content hashes for identical input. Old runs remain inspectable.

Tables use canonical cells and row-span-aware groups with repeated headers. Formula and question
stems/options remain atomic, even above target sizes; excessive atomic units route to review.
Figure chunks reference source crops/captions without interpreting images. Deterministic question
recognition marks inferred grouping separately from verbatim answers; absent answers stay absent.

Chunk dispatch uses the existing transactional outbox with a distinct CHUNKING task kind.
Persist complete chunks/mappings/findings and active-run selection atomically after validation.
Only one validated current dataset is eligible for future M4; reparsing invalidates prior selection.
Cancellation, expired leases and changed active parses fence completion. M3 stops at
READY_FOR_EMBEDDING and does not make a document searchable.

Validation reports structural counts and quality findings, never retrieval accuracy. Evaluate
identical normalized fixtures on Windows and the canonical Linux worker. Preserve M2's measured
parser-label variation as a separate upstream fact.

M3 outcome: implemented as decided. Two points are worth recording. First, the source-coverage rule
became a *text* rule rather than an identity rule: an element that is still referenced but no longer
fully covered by its spans is source loss, and a fragment that only a parent still carries is a
split defect. Second, the reuse rule was tightened: matching the parse run and the policy
fingerprint is an identity argument, so a completed dataset is reused only after its normalized
input is re-fingerprinted and compared. Determinism was verified for identical normalized input on
both the Windows host and the Linux worker; the upstream M2 parser-label variation between those
environments is unchanged and is not hidden by this decision.
