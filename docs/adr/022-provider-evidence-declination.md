# ADR-022: A provider may decline, and declining is a semantic abstention

Status: accepted. Post-M12 acceptance hardening; no milestone was created. Extends the M7 provider
contract in ADR-012. Nothing in ADR-012 is reversed — in particular, no retrieval, fusion or
reranker score reaches the gate, and `retrieval_scores_permitted` remains `Literal[False]`.

## Context

An out-of-corpus cardiology question — "What are the diagnostic criteria for heart failure with
preserved ejection fraction?" — reached a `SUFFICIENT` gate against a microbiology corpus, called
the provider, and ended as `GENERATION_SCHEMA_VIOLATION` → `FAILED`. No answer was released, so
fail-closed behaviour held, but the reader was told the service had broken when what had actually
happened was that the corpus did not cover the question.

The stored turn shows the sequence: `sufficiency_status` is `None` on the failed turn, so the gate
passed and generation broke.

### Why the gate passed

Retrieval is unconditional top-k — `dense_top_k=40`, `sparse_top_k=40`, fused to 20, reranked to
`final_top_k=5` — with **no score threshold anywhere**. The reranker orders candidates; it cannot
reject them. The gate then checks counts, authority, artifacts and completeness, all properties of
the evidence, none a relation between question and evidence. Every question therefore receives five
structurally well-formed anchors, whatever it asked about.

### Why the generation broke

`ProviderDraft` required `answer` (`min_length=1`) **and** `claims` (`min_length=1`), and every
`DraftClaim` required at least one `evidence_id`. `evidence_gap` was optional and additive.

**There was no schema-valid way to decline.** A provider that correctly concluded "this evidence is
about something else" had to fabricate a claim or break the schema. It broke the schema. The
semantic judgement was already happening and was already correct; the contract had nowhere to put
it.

## Alternatives considered

**A. A deterministic lexical relevance prerequisite — rejected on measurement, not on taste.**

Two variants were measured across all 26 gold questions.

*Overlap between the question and the selected evidence*, using the repository's own analyzer:

| Class | union coverage |
|---|---|
| supported (n=11) | 0.50 – 1.00 |
| context (n=5) | 0.64 – 1.00 |
| unsupported (n=5) | 0.00 – 0.69 |

The ranges overlap. U2 scores **0.692** — higher than S7 (0.50), S10 (0.571), S4 (0.625) and C3
(0.50). Any threshold rejecting U2 also rejects five answerable questions, two of which are
currently correctly `VERIFIED`.

*Corpus-level term absence*, via `sparse_terms` (exact document frequency, already summed for IDF):

```
U2  unsupported  content=7  absent=1 (0.143)  ['ejection']
X2  supported    content=9  absent=1 (0.111)  ['case-fatality']
```

Identical absolute count. The fractions differ only because the questions differ in length, so a
threshold between them would be tuned to these 26 questions. The two misses are not even
comparable in kind: `case-fatality` is an analyzer artefact whose parts are both present
(`case` df=358, `fatality` df=7), while `ejection` is genuinely absent — but U2's other six terms
are all present in a microbiology textbook (`heart` df=84, `failure` df=93, `criteria` df=10,
`fraction` df=4, `preserved` df=3) because they are ordinary English words.

**Vocabulary presence is not topical coverage, and lexical machinery cannot see the difference.**

**B. A dedicated semantic relevance model before generation — rejected.** A third provider call per
question, a new model in the critical path, and any calibrated relevance *score* reintroduces
exactly what ADR-012 banned. Its judgement also duplicates one the generator already makes.

**C. M8 alone — insufficient.** M8 never ran for U2. When it does run, its refusal surfaces as
`UNVERIFIED` — "a supported answer could not be verified" — which is a statement about
verification, not corpus coverage. It also spends two provider calls to reach a conclusion
available before the first.

## Decision — make declination representable

`ProviderResult` is a discriminated union on `outcome`, wrapped in an object because both
transports carry a top-level JSON object (an OpenAI `json_schema` response format and an Anthropic
tool `input_schema`):

- `AnswerDraft` — `answer`, `claims`, optional `evidence_gap`.
- `DeclinedDraft` — `declination: Literal["EVIDENCE_DOES_NOT_ADDRESS_QUESTION"]`, optional
  `explanation`. **No `answer`, no `claims`.**

A union rather than a `declined: bool`, so a response that both answers and declines is
*unrepresentable* rather than merely discouraged, and a declination has nothing in it for a
verifier to check because there is nothing being asserted. The declination reason is a closed
vocabulary: a provider cannot invent a new reason to refuse and have it recorded as declared.

A declination produces `INSUFFICIENT_EVIDENCE` with `EVIDENCE_DOES_NOT_ADDRESS_QUESTION` appended
to the gate's own reason codes. No answer, no citations, and **claim verification is not run** —
there is no draft to verify. `GENERATION_SCHEMA_VIOLATION` remains for genuine malformation,
including a `DECLINED` result missing its declination.

The prompt gained rule 3a telling the provider that declining is a correct, expected outcome, and
distinguishing it from rule 3: partial coverage is still an answer grounded in what is present with
the remainder named in `evidence_gap`. Without that instruction the branch would be unreachable.

### Why this is not "the generator as relevance judge"

The union is a **one-way valve**. A provider may refuse, and that is the whole of the new power it
is given. It cannot assert that evidence is relevant, cannot mark evidence sufficient, cannot
reopen or bypass the gate, cannot reach a reader without M8, and cannot release an unverified
answer. The only new capability points at *more* abstention, which is the direction this
architecture already prefers.

M7 still gates entry: a question the gate refuses never reaches a provider at all, so a provider
cannot decline its way around an abstention that already happened. M8 still gates exit, unchanged,
and remains the defence against a provider that fabricates instead of declining.

### The repair path

M8's single repair uses the same union, so there is one wire shape for every structured generation
call. A repair that declines has produced nothing releasable, which is exactly the repair-failed
condition the loop already abstains on, so it raises into that existing path rather than adding a
second way to end the loop. Relevance was settled at generation; a repair is a narrowing of an
answer that already exists.

## Consequences

U2 becomes a semantic abstention instead of a technical failure, if and only if the provider
recognises the mismatch. Nothing forces it to: a provider that answers anyway is caught by M8
exactly as before. That is the intended division — the contract makes the correct behaviour
*expressible*, and M8 remains what makes it *safe*.

No threshold, no score and no new model was introduced. The measurements above are recorded here
because "we chose not to add a relevance threshold" is only a defensible decision if the evidence
that one would not work is written down with it.
