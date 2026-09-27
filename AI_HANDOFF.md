# AI HANDOFF

## Current state

**M12 COMPLETE and verified. This is the final planned milestone; M13 was not created.**

Baseline main `10930c4` (committed M11), clean at start. All M12 work is uncommitted; nothing
staged; no history rewritten. M0–M11 were not restarted or redesigned.

**The safety pipeline is unchanged.** M12 added barriers around it and removed none: no fast path,
no verification-disabled mode, no provider-direct answering, no fallback to pretrained knowledge. A
test asserts the codebase contains no such switch. M9 (64), M10 (48) and M11 (92) all pass unchanged.

See `docs/verification/m12.md` and `docs/adr/017-m12-production-hardening.md`.

## Deployment requirements

Production is a configuration the system **refuses to run badly**. With
`MEDRAG_ENVIRONMENT=production`, `Settings` will not construct unless all of the following hold,
and the error names each unmet rule without printing a value:

OIDC auth mode · no development principals · rate limiting enabled · explicit HTTPS CORS origins ·
database/Redis/object-store credentials present and not development defaults · backing services not
on localhost · `MEDRAG_EMBEDDING__OFFLINE` and `MEDRAG_QUERY_ENCODER__OFFLINE` true · a configured
provider has its key.

```bash
uv run python scripts/production_preflight.py            # read-only, no provider call
uv run python scripts/production_preflight.py --check-provider   # opt-in live call
```

Full detail: `docs/architecture/production-deployment.md`.

## Production authentication

Vendor-neutral OIDC — issuer, audience, JWKS URI and claim names are configuration; no code knows
the provider. Signature verification has no disable flag; algorithms come from configuration not the
token header; asymmetric only; a missing tenant claim is refused rather than defaulted; unmapped
roles grant nothing; several mapped roles resolve to the narrowest.

Development auth has **two** independent barriers: the settings validator and `build_auth_provider`.

```bash
MEDRAG_AUTH__MODE=oidc
MEDRAG_AUTH__ISSUER=https://login.example.com/v2.0
MEDRAG_AUTH__AUDIENCE=api://medical-rag
MEDRAG_AUTH__JWKS_URI=https://login.example.com/discovery/v2.0/keys
MEDRAG_AUTH__ROLE_MAPPING='{"MedRag.Reader":"reader","MedRag.Admin":"admin"}'
```

## Secrets

Development: ignored `.env`. Production: mount files and use `<NAME>_FILE`, which every managed
store provides and which keeps values out of the process environment. A direct value wins; only
known names resolve; an empty or unreadable file fails startup naming the path, never the value.

## Services and migration

Nine long-running services: postgres, redis, qdrant, minio, api, frontend, worker, retrieval,
dispatcher. Only frontend and API are externally reachable. **Health, readiness and metrics are not
proxied through the public origin** — they are internal surfaces.

Run Alembic as a **pre-deploy job**, never from application startup; replicas would race. Never
downgrade a production schema automatically. **M12 added no migration**, which is correct for a
hardening milestone; the two post-M12 migrations are `parse_review_decisions` and
`out_of_scope_outcome`, which is the current head.

## Model provisioning

Three MedCPT models pinned by revision and SHA-256, loaded offline. Provision before serving with
`scripts/provision_{embedding,query,reranker}_model.py`. Production refuses to start unless the
encoders are offline-pinned, so a forgotten variable cannot cause a silent revision change.

## Backups

`pg_dump` for PostgreSQL, `aws s3 sync` for originals. Qdrant is **rebuilt, not restored**.
Verify every restore into a disposable target:

```bash
uv run python scripts/verify_restore.py --database "$RESTORE_URL"
```

It refuses to target the configured live database. A real restore was exercised: 7/7 checks passed.
No scheduler ships. No RPO/RTO is claimed. See `docs/architecture/backup-and-recovery.md`.

## Commands

```bash
# tests — fresh process per group (Docling crashes natively under memory pressure)
python .local/m12-regression-run.py                    # 878 across 11 groups
uv run pytest backend/tests/test_m12_units.py -q

# static
uv run ruff check backend workers scripts && uv run mypy backend/app

# evaluation (offline, no API key, blocking in CI)
uv run python scripts/evaluate_m11.py

# operations
uv run python scripts/smoke_m12.py                     # 33 checks against the live stack
uv run python scripts/load_test.py --reads 200         # deterministic by default
uv run python scripts/load_test.py --ask 12 --live-provider    # opt-in, costs money
```

## Post-M12 remediation (no new milestone)

Three pieces of work sit after M12 and are **not** a milestone. M13 was not created.

Committed: `0a52258` clean development workspace utility, `74ccd42` large-PDF upload limits
(application 512 MiB, proxy 520 MiB, size-scaled validation budget).

**Uncommitted: large-document parsing memory.** A real 153 MiB / 932-page medical textbook now
uploads and validates, but parsing killed the Celery child with SIGKILL at 6.2 GB
(`memory.max_usage_in_bytes` 6209466368, `oom_kill 1`, the container itself surviving) and the
document sat in `PARSING`. Three causes, all measured rather than inferred:

| Cause | Measurement | Change |
|---|---|---|
| Docling retains per-page state for a whole `convert()` call | 44 MiB/page on real textbook content; a 50-page call peaked 2442 MiB | convert in 25-page windows joined by absolute page number |
| The source text layer was read for the whole book up front | ~1.3 GiB before Docling converted a page | read it one window at a time |
| glibc kept freed arenas instead of reusing them | RSS ratcheted ~6 MiB/page while retained output grew ~0.2 MiB/page | trim between windows; `MALLOC_ARENA_MAX=2` |

Result on the same book: **932/932 pages, 18,888 elements, 263 tables, 1,208 figures, peak worker
4967182336 bytes (4.74 GiB) against the previous 6209466368, parse 4042 s.** Peak is now bounded by
`page_window_size`, not by book length — settled RSS over eight windows went from climbing
2267 → 2842 MiB to flat 1248 → 1321 MiB.

The book parsed to **NEEDS_REVIEW**: 2 pages of 932 raised `PAGE_CONTENT_LOST`. That is the
fail-closed gate working, and neither finding is a windowing artefact. Page 44 is a section-divider
page that yields the same three elements at every conversion size. Page 517 carries only the tail of
a paragraph that begins on page 516, which Docling anchors to the paragraph's first page — the text
is present in the parse, on page 516. Both are provenance-precision limits, not content loss; see
`docs/architecture/document-parsing.md`.

**Uncommitted: reviewed parse acceptance** (ADR-018), which closed the gap that left this document
stuck. A flagged parse keeps `validation_result = NEEDS_REVIEW` and every finding forever; an
authorized curator's acceptance is an append-only `parse_review_decisions` row bound to one exact
parse run, and the run becomes `ParseRunStatus.REVIEWED_ACCEPTED`. The job moves
`NEEDS_REVIEW -> READY_FOR_CHUNKING` **without consuming a retry**, because nothing is reprocessed.
Permission `ingestion:accept`, curator and admin only. Migration `parse_review_decisions`
(down_revision `m10_configuration`) moves six enforcement points together — the status vocabulary
and column width, `ck_parse_runs_active_run_usable`, `m1_job_guard`, **both** clauses of
`m3_run_guard`, and the new `parse_review_guard`. The `m3_run_guard` activation clause is the
subtle one: it keys on `validation_result`, so leaving it behind would let chunking finish and then
refuse to activate its own dataset. Anything gating on "is this parse usable" must consult run
**status**, never `validation_result` alone.

The real book was accepted through this flow on 2026-09-11: run `3ebd8b90` is `REVIEWED_ACCEPTED`
and active, `validation_result` still `NEEDS_REVIEW`, all 10 findings unchanged, `retry_count`
still 2/3, chunking ran and produced 4,253 chunks.

**Committed since: `40cb745` embedding contract.** Embedding first failed with
`EMBEDDING_INPUT_TOO_LONG`. `max_input_tokens` is `Literal[512]` — pinned to the model, not tunable
— and the embedder refuses to truncate by design, so nothing was silently dropped. The real defect
was narrow: **1 chunk of 3,635 embedder inputs** exceeded the limit, a `TABLE_PART` whose splitter
re-added a suffix after the budget was computed (440 + 105 = 545). `TEXT_PARENT` chunks are context
containers and are never embedded, so the 618 oversized parents were never inputs — an earlier
count of "619 of 4,253" measured dataset chunks rather than embedder inputs and was wrong. Fixes:
the splitter now renders through one `rendered(indexes)` helper so budget and emit cannot diverge;
`table_max_tokens` 450 → 384; and a `chunking_fits_the_encoder` cross-config validator pins
384 + 128 reserve = 512. The book was rechunked through the supported path (retry 3 not consumed)
and reached `RETRIEVAL_READY`.

**Committed since: `a4ef769` sufficiency scope (ADR-019) and `b09b765` parent completion.**
Sufficiency's completeness checks were global while every other check was scoped to the reranked
anchors, which made a real corpus unanswerable — 0 of 26 questions, with the defining passage at
rank 1 and **zero** deficiencies in the evidence actually selected. Warnings are now classified
default-deny: advisory requires positive proof of EXPANSION tier and no reranker selection;
anything untiered or unknown blocks. Separately, chunking builds parents to ~1280 tokens while a
required expansion may admit 512, so 45 of 46 required parents were offered and lost to the budget.
Assembly now computes the **minimum uncovered completion fragment** — the text after the anchor, cut
at the first sentence end — in a three-pass order that reserves budget for required completions
before optional context. Budget-dropped required parents: 45 → 0. Nothing is truncated mid-sentence
and no continuation text is invented.

**Uncommitted: unusable anchors are excluded, not flagged** (ADR-020). One anchor whose required
completion could not be admitted still invalidated the whole question, so eleven questions abstained
with complete, independently sufficient anchors in the set. Such an anchor — and every expansion
belonging to it — is now removed from `evidence_blocks`, `anchors` and `expansions` and recorded in
`excluded_anchors` / `excluded_blocks`. This is safer, not merely more permissive: generation,
citation binding and verification all read `evidence_blocks`, so what used to be kept out of the
prompt only by the refusal is now structurally absent. **Conflict detection still runs over the full
assembled set**, and a required dependency on a *kept* anchor still blocks, so a filtering failure
cannot open the gate.

Measured on the real book, 26 questions: 12 questions had an exclusion, 16 anchors and 20 blocks
removed, identical across two runs. Supported/context outcomes went 3 → **5 VERIFIED**, 2 → 4
UNVERIFIED, 11 → 7 INSUFFICIENT. Every changed outcome had at least one excluded anchor; no
question with zero exclusions changed. The remaining abstentions are
`VISUAL_INTERPRETATION_UNAVAILABLE` and `TABLE_STRUCTURE_INCOMPLETE`, both untouched here.

**Two findings this exposed, neither fixed:** the gate has no ambiguity or personal-advice check, so
A3 ("What should I do about the infection?") now returns a grounded, cited, general answer instead
of abstaining — its previous abstention was an accident of a required-parent warning, not a policy.
And U2, an out-of-corpus question, now reaches the provider and returns
`GENERATION_SCHEMA_VIOLATION` (fail-closed, no answer released) rather than abstaining at the gate.

**Uncommitted: medical intent policy (ADR-021) and provider declination (ADR-022).** The
acceptance run exposed two policy gaps, both of which had previously been covered by accident.

*Intent.* "What should I do about the infection?" returned a grounded, cited answer. There was no
grounding failure — the answer was general educational prose — but the only intent-aware artefact
in the repository was rule 7 of the provider prompt, a probabilistic instruction on the untrusted
side of the boundary. `app/sufficiency/intent.py` (`question-intent-v1`) now classifies seven
categories from the question alone, before retrieval. A refusal requires a **conjunction** —
personal framing *and* a clinical action or risk object — because "treatment", "dose" and
"dangerous" are ordinary textbook vocabulary and a classifier firing on those would make the corpus
unusable. An epistemic verb keeps a first-person question academic. Refused questions get the new
`OUT_OF_SCOPE` outcome with `PERSONAL_MEDICAL_ADVICE_REQUESTED`: no index searched, no provider
called, no answer, no citations. Migration `out_of_scope_outcome` widens the
`ck_conversation_turns_turn_outcome` CHECK; its downgrade refuses while refused turns exist unless
`MEDRAG_ALLOW_CONVERSATION_LOSS=1`.

*Relevance.* An out-of-corpus cardiology question reached `SUFFICIENT` and ended as
`GENERATION_SCHEMA_VIOLATION`. The cause was the contract, not the model: `ProviderDraft` required
an answer *and* a claim bound to evidence, so a provider concluding "this is about something else"
had to fabricate or break the schema. `ProviderResult` is now a discriminated union —
`AnswerDraft | DeclinedDraft` — and a declination carries no answer and no claims, so answering and
declining is unrepresentable. It maps to `INSUFFICIENT_EVIDENCE` +
`EVIDENCE_DOES_NOT_ADDRESS_QUESTION`, and claim verification is not run because there is no draft.
The union is a **one-way valve**: a provider may refuse and nothing else. M7 still gates entry, M8
still gates exit unchanged.

**A lexical relevance threshold was measured and rejected**, and the numbers are in ADR-022 because
"we chose not to add one" is only defensible with the evidence written down. Overlap with selected
evidence does not separate: U2 scores 0.692, above S7 (0.50), S10 (0.571) and S4 (0.625). Corpus
term-absence does not separate either: U2 and X2 each have exactly **one** absent content term, and
U2's other six are present in a microbiology corpus because they are ordinary words. Vocabulary
presence is not topical coverage.

Gold set, 26 questions: exactly three changed. A3 → `OUT_OF_SCOPE` (0.0 s, no provider call), U2 →
`INSUFFICIENT_EVIDENCE`, and C3 moved VERIFIED → UNVERIFIED, which re-running C3 three times
(VERIFIED 3/3) places inside ordinary provider variance — generation temperature is deliberately
the provider default, not pinned. No academic question was blocked by intent classification.

**Uncommitted: figure dependence is a property of the question (ADR-023).** `classify` fell through
to `if kinds & FIGURE_CHUNKS` over all anchors, so one `FIGURE_CONTEXT` among five made the whole
question visual — and since `vision_analysis_available` is `Literal[False]`, permanently
unanswerable. Measured: **11 of 26 questions classified FIGURE_DEPENDENT, 0 of them from the
question's own words**, all 11 blocked *solely* by `visual_interpretation`, and 0 of the 19 figure
blocks in those EvidenceSets were textless. ADR-012 says "a figure **question**"; the
implementation had widened it to "a question near a figure chunk".

The rule is now `asked & FIGURE_CUES` **or** every anchor visual *and* unreadable — no score, no
threshold, no caption length, no count of figure anchors. `visual()` reuses M8's own disjunction so
the two layers cannot disagree. Cues gained in-image deixis (`arrow`, `label`, `panel`, `inset`,
`circled`, `indicated`, …); `imaging` was deliberately **not** added, because "presents on imaging"
asks what the literature describes. `CLASSIFIER_VERSION` is `question-kind-v2`.

**M8 is untouched and is what makes this safe.** A claim citing a figure block still fails
`VISUAL_INTERPRETATION_REQUIRED`, and that was observed for real: S6 reached generation, the model
cited the caption, and M8 refused it. No released answer in the 26-question run cites a figure.

Gold set: FIGURE_DEPENDENT **11 → 0**, questions reaching generation 11 → 22, VERIFIED **4 → 7**
(S3, S5, S9, C5 recovered). U1/U3/U4/M1 now reach the provider and abstain semantically via
ADR-022 declination. S2 and S4 flip between runs at the provider layer; both have zero figure
anchors and unchanged classification, so neither is attributable to this change.

**Correction to an earlier note: S3 is not a retrieval miss.** Its anchors contain, verbatim,
"The infection recruits the release of immature band forms from the bone marrow described as a
'left shift'". The gold file's `expected_evidence` names a different passage stating the same fact,
which is why earlier probes scored it absent.

Two pre-existing defects were found and deliberately left alone: `"cell"` is a `TABLE_CUES` member,
so "Describe prokaryotic cell structure." classifies TABLE_DEPENDENT (it did before this change
too); and the structural floor has no positive instance in this corpus, so its correctness rests on
unit tests rather than observation.

**Uncommitted: table dependence (ADR-024) and page-text-loss measurement (ADR-025).**

*Tables.* `cell` was a `TABLE_CUES` member. In a medical corpus that is subject vocabulary — 920 of
3,939 indexed chunks contain it, against at most 3 for every other cue — and it refused five of six
ordinary microbiology questions with `TABLE_STRUCTURE_INCOMPLETE` on evidence holding no table at
all. The structural fallback produced 7 of the 8 real TABLE_DEPENDENT verdicts. Both removed:
`TABLE_DEPENDENT` is now exactly `asked & TABLE_CUES` over the seven remaining cues, and
`CLASSIFIER_VERSION` is `question-kind-v3`. Tables get **no** structural floor (unlike figures)
because the builder renders caption, headers and cells into the chunk's own text, so a headerless
table is a readable label/value layout rather than unreadable evidence. M8 still refuses any claim
citing a table whose artifact lacks header rows — unchanged, and what keeps this safe.
`table_anchors_present` added as a diagnostic. On the gold set TABLE_DEPENDENT drops 8 → 1, and
only A2 changes outcome. C1 stays blocked and is correctly classified: it names a table, and
retrieval returned none — a retrieval miss, not a classification error.

*Page text.* The char rule could not tell loss from cross-page anchoring, and on the real book both
of its ERRORs were false. `ingestion/validation/text_recovery.py` now asks the direct question — is
this page's source text in the parse? — comparing material words (two characters or more, as a
set), against the page itself and against its immediate neighbours. Three outcomes replace two:
`PAGE_TEXT_BELOW_FLOOR` (furniture), `PAGE_CONTENT_ANCHORED_ELSEWHERE` (cross-page), and
`PAGE_CONTENT_LOST` (ERROR, unchanged). **Unmeasured fails closed.** Source text is re-read only
for suspect pages — 2 of 932 — from the workspace file, so no reparse and no unbounded memory.
Replayed read-only over the stored parse: page 44 own=1.000 → `PAGE_TEXT_BELOW_FLOOR`, page 517
own=0.046 nbhd=1.000 → `PAGE_CONTENT_ANCHORED_ELSEWHERE`, **page-loss ERRORs 0**. Not exercised by
a fresh ingestion of that book; see the ADR's limitations.

*Operations.* compose now carries `restart: unless-stopped` on all nine services and memory limits
on the four application services, derived from measurement rather than guessed: worker 6G against
the measured 4967182336-byte (4.74 GiB) peak, retrieval 4G (two resident MedCPT models), API 2G,
dispatcher 512M. This closes the "no container resource limits" item in Known risks below.

The lease now heartbeats between windows (`PARSE_WINDOW_COMPLETED`), which is what lets a
multi-hour parse keep a lease sized for liveness. Recovery was observed for real: a worker lost at
13:10 was reaped at 13:40 to `FAILED` / `PARSER_LEASE_EXPIRED` and the job retried successfully.

Time budgets moved with it: one conversion call 900 s, whole document 900 s + 12 s/page capped at
14400 s, Celery soft/hard 15600 s / 15900 s. A validator enforces the ordering.

**No claim of 300–500 MB support is made.** Only this 153 MiB / 932-page book has been run.

**Uncommitted: FIGURE_CONTEXT is embeddable by construction.** The embedding contract committed as
`40cb745` covered tables and prose but not figures. The figure branch of `Builder.artifact` emitted
the figure element, its caption and its linked neighbours as **one chunk with no budget at all**,
while tables split at `table_max_tokens` and prose packed at `child_target_tokens`. A real 22-page
atlas chapter ("Cerebellum and Fourth Ventricle", 233 elements, 18 figures, 0 tables, parse clean)
produced a single `FIGURE_CONTEXT` of **641 tokens against the 384-token retrieval budget** — the
whole of it one 2,810-character figure legend, with **no neighbour prose involved**. Anatomy atlases
carry legends that long, so this was never about one document.

A second, latent half: chunk validation measured `token_count`, the source text, while the encoder's
input body is `retrieval_text`. The hierarchy prefix and a figure's no-text placeholder are embedded
but belong to no source text, so the check could call a body embeddable that was not. Both now
measure the retrieval representation.

Fix: a long legend is **split**, as a large table is, into several `FIGURE_CONTEXT` chunks carrying
`part_number` / `part_count`; an unsplit figure gains no part metadata, so ordinary figures keep
their exact chunk identity. Splitting rather than bounding is forced by ownership — the caption
element is `consumed` by the figure chunk, so text outside a bounded representation would sit in no
retrieval unit at all, whereas a linked *neighbour* stays retrievable as its own `TEXT_CHILD`. Every
part keeps the figure element, the artifact id, the complete caption in metadata and exact source
offsets; boundaries fall on sentence boundaries where they exist and tokenizer boundaries otherwise.
`Builder.representation` is now the single renderer for retrieval text, so a budget and an emit
cannot diverge — the same reason `table.rendered` exists. No configuration value changed, no encoder
limit moved, and `CHUNK_OVERSIZED` remains an ERROR.

`CHUNK_FIGURE_NO_TEXT` is unchanged and was **not** a defect: of the parse's 18 figures only 7 have
a caption element, so 11 chunks are `visual_only` with `image_available` true. `visual_only` is
decided for the figure as a whole, never per part.

Live regression on the fresh document (tenant `ba3d8361`, one retry of three consumed): chunk run
`82e14324` **PASS_WITH_WARNINGS**, 93 chunks, the 641-token figure now 345 + 296, **0
CHUNK_OVERSIZED**, the same 11 warnings, caption reconstructing to 2,810 of 2,810 characters from
the two parts' spans. It reached **RETRIEVAL_READY**: 77 embeddings, worst encoder input 477 of 512
tokens, none truncated, dense index VERIFIED, sparse VERIFIED with 7,183 postings, 77 Qdrant points.

**Remaining construction paths that are fail-closed but not yet provable by construction.** Neither
is a regression and neither is touched here; both now surface at chunk validation, which names the
chunk, rather than at embedding two stages away.

* `FORMULA` budgets nothing either. Its neighbour context is not consumed, so bounding it would lose
  nothing retrievable — unlike a caption — but splitting an expression across parts would make
  `metadata["expression"]` mean something different. That choice deserves its own decision.
* `generic` packs prose against the **source** budget, so a deep hierarchy can push a full-size
  `TEXT_CHILD`'s retrieval body past 384. Measured across the whole local corpus the prefix costs at
  most 11 tokens and **zero** existing chunks exceed the budget, but the guarantee is arithmetic
  luck, not construction. Closing it means subtracting the prefix cost from the pack budget, which
  moves every chunk boundary in every document with headings — an ADR and a full rechunk, not a
  tuning pass.

**Uncommitted: product acceptance on a real chapter.** A clearly supported question —
"What is the tentorial surface of the cerebellum?" against a corpus whose page reads "The tentorial
surface faces and conforms to the lower surface of the tentorium" — abstained UNVERIFIED **10 times
out of 10**. Retrieval was never at fault: the sentence ranked 1 dense, 3 BM25, 1 fused. Four
defects in claim handling, each found by tracing one request end to end:

| Defect | Why it fired | Fix |
|---|---|---|
| Coordination split on a term count taken from the **index** analyzer, which emits a capitalised word twice for IDF | "Midline anterior" counted 3 terms, so a noun phrase split across its own subject and the remainder matched no declared claim → `CLAIM_NOT_CITED` | count distinct words; split only where **both halves are declared propositions**, so splitting can never manufacture an uncited claim |
| A citation marker written into the prose became its own sentence | its hex groups counted as content, so `[<uuid>]` was verified as a material medical claim | markers are not propositions; the generator is also told to write none |
| Sentence-level negation polarity | source "is smooth **and not** marked by deep fissures"; a claim quoting the positive half was `NEGATION_REVERSED` → CONTRADICTED | polarity read per clause, with the whole sentence still a candidate so "not A and B" still matches |
| "respectively" split, and pronoun subjects | "...accommodate the brainstem and falx cerebelli, respectively" cut in half says both incisurae hold the brainstem — which the verifier correctly called CONTRADICTED, though the draft never said it. "It contains the vermis..." was refused for having no antecedent | never split a distributive coordination; carry the sentence **and the one before it** as verifier context, labelled not-evidence |

Prompts moved to `grounded-draft-v2` (no citation markers in prose; write self-contained
statements) and `claim-verification-v2` (resolve an elided subject from the context, judge only the
statement). **Nothing was relaxed**: the whole-statement rule, every deterministic check, the
citation requirement and single-repair-then-abstain are unchanged, and undeclared text is still
verified inside the sentence that carries it.

Measured on the live workspace, same question, 10 trials each: **0/10 → 5/10 → 9/10 VERIFIED**, the
one failure a genuine `SEMANTICALLY_CONTRADICTED`. All four supported questions verify; an
out-of-corpus question abstains `INSUFFICIENT_EVIDENCE` with `EVIDENCE_DOES_NOT_ADDRESS_QUESTION`;
a personal-advice question is `OUT_OF_SCOPE` in 51 ms with no provider call.

**Ask conversations survive navigation.** The open conversation was `useState` inside the routed
page, so Ask → Library → Ask discarded it and the next question opened a second conversation over
history the server had been keeping. `GET /conversations` and `GET /conversations/{id}` already
existed and were never called. The id now lives in the session (cleared on sign-out) and in the
address; turns always come from the server, and no conversation content is put in browser storage.

**Retrieval inspector is diagnostic only, and now says so.** Its mode is a field of one request:
`/retrieval/search` (dense / BM25 / RRF), `/retrieval/rerank`, `/retrieval/draft`,
`/retrieval/answer`. `AskRequest` has no mode field at all, so Ask cannot inherit one; a test pins
that. Each mode now states what it runs and whether it calls a provider.

**Latency, measured (warm p50 of five, one cold call separately).** Warm 12.6 s; cold 21.3 s, the
difference almost entirely the first provider call. The dominant stage is **MedCPT cross-encoder
reranking at 7.7 s — 61% of a warm request** — 20 candidates × 512 tokens on CPU, models already
warm in the retrieval sidecar. Retrieval itself is 45 ms (dense 9, sparse 18). Generation 1.9 s,
verification ~2.3 s. Nothing was tuned: reducing `candidate_top_k` or batch size is an accuracy
change and needs a benchmark first. The claim fixes did cut verification on the failing question
from 58 s to ~2.3 s by removing a repair round and its re-verification.

The reader-facing Timing row was also wrong: `pipeline_total_ms` was labelled "Total" while
verification ran *after* it, so a 70-second answer reported 12 seconds. Subtotals are now excluded
and Total is measured at the request boundary.

**Uncommitted: live stage progress for Ask.** A twelve-to-thirty-second request showed a fixed
list of four stage names for its whole duration, so a reader could not tell which stage was running,
which had finished, or whether the machine was alive. `AskConfig.stream_progress_stages` had existed
as configuration since M9 with nothing reading it.

`POST /ask` is now content-negotiated: `Accept: text/event-stream` gets `stage` frames while the
request runs and then one `result` frame carrying exactly the JSON body the default form returns.
No new endpoint, no new state store, no background task, no WebSocket — the existing request streams
its own progress, and a client that does not negotiate keeps the old contract byte for byte, which
is why all 139 frontend tests and the M9 suites passed unchanged.

Seven stages — PREPARING, RETRIEVAL, RERANK, EVIDENCE, GENERATION, VERIFICATION, FINALIZE — each
announced by the code that performs it, at boundaries the orchestrators already measured for
`durations_ms`. `app/services/progress.py` carries them out through a `ContextVar`, so no stage
signature changed and the worker thread the synchronous M5/M6 stages run in reports to the same
request. **A stage event has no field that can hold content**: request id, stage, state, sequence,
timings, and nothing else, so the channel cannot widen what M8 decided a reader may see. Nothing is
simulated — no timer advances a stage, no percentage, no ETA; a backend test greps the frontend for
exactly those shortcuts and proves the grep catches them.

Live on the real stack (tenant `ba3d8361`, "Cerebellum and Fourth Ventricle"):

| Question | Timeline |
|---|---|
| tentorial surface | PREPARING 0 ms · RETRIEVAL 618 ms · **RERANK 8.1 s** · EVIDENCE 229 ms · GENERATION 6.0 s · **VERIFICATION 15.3 s** · FINALIZE 44 ms → VERIFIED at 30.6 s |
| personal advice | PREPARING 0 ms · five stages SKIPPED · FINALIZE 16 ms → OUT_OF_SCOPE at 33 ms, no provider call |
| out of corpus | retrieval → generation ran, VERIFICATION SKIPPED (provider declined) → INSUFFICIENT_EVIDENCE at 9.7 s |

Two timing defects surfaced and were fixed while doing it. M8's own measurements lived on the
verification report and never reached the response, so the largest stage of a long request appeared
in the total and in no row. And the out-of-scope path reported `Total: 0 ms` for a request that took
46 ms, because the clock started after the ownership and replay checks and stopped before the turn
was recorded; the boundary now covers both, and `persistence_ms` is its own row.

No cancel control is offered: the pipeline has no cancellation mechanism, and a button that only
dropped the connection while the provider call continued would be a lie about what it does.

**Observation, not addressed** (the brief ruled the page layout out of scope): the conversation list
sits above the composer and the stepper, so on a workspace with many conversations the progress
panel lands below the fold. Placing progress adjacent to the composer, or collapsing the list, is
the obvious follow-up.

**Uncommitted: the Ask page is a conversation, and answers can show their source figures.**

*The page.* The conversation index used to render above the chat, so on a workspace with any
history the composer and the live progress sat below a list that grew without bound. The index
moved to the rail; the centre now shows the selected conversation only — question bubbles, the
answer, and the progress stepper under the turn being answered. The rail carries Ask / Library /
Settings, New conversation, the recent conversations (scrolling independently), and an **Advanced
tools** disclosure whose entries are filtered by permission, so a reader is no longer shown four
inspector pages they cannot open. It collapses to icons (labels stay for screen readers and
tooltips) and becomes a drawer with a scrim under 860px. The collapse preference lives in this
browser and touches nothing else. A finished request collapses to one line — `✓ Verified · 30.6 s ·
View processing details` — that expands to the same backend stage record.

*Citations* are evidence cards: source label, document, page, provenance metadata, and the exact
stored excerpt M8 verified against, read back rather than reconstructed.

*Figures.* `app/services/figures.py` resolves the figures a verified answer may display, from
provenance only: a citation that *is* a figure (`CITED_EVIDENCE`), or a citation whose verified
text names a figure whose caption declares that label (`CITED_TEXT_REFERENCE`). Same page, same
document and subject similarity are all explicitly not links. Ranges are expanded only across one
major number, compound references — "( Figs. 1.2 and 1.7 )" — are read in full, a figure without a
stored image is never offered, and at most six are listed. **Nothing is interpreted**; the M8
visual rules are untouched, and a figure supports no claim. `AskResponse.figures` and the stored
turn view call the same resolver over the same stored citations, so a figure survives a
conversation reload; no migration was needed.

*Images* reuse the existing M2 route under `document:read`, tenant-scoped, with the figure required
to belong to the parse run in the path. **A defect the live run caught:** the browser cannot send
an `Authorization` header from an `<img src>`, so the first implementation produced a 401 and a
broken picture. The bytes are now fetched like any other request and rendered from an object URL —
which keeps the one authorized path and still exposes no object-store key.

Live on tenant `ba3d8361`: "What is the petrosal surface of the cerebellum?" → VERIFIED in 27.9 s,
two evidence cards, and **Figure 1.7** linked because the cited text reads "( Figs. 1.2 and 1.7 )";
"roof of the fourth ventricle" → **Figure 1.9**; both survived the conversation reload. The image
served 200 `image/png` (570 KB) to its own tenant, 404 to another tenant's admin, 401 anonymously.

**Truthfully reported non-result:** the tentorial question shows *no* figure. Its evidence names
"( Figs. 1.2-1.4 )", and the parse attached no caption to figures 1.2–1.4 — they are among the
eleven `CHUNK_FIGURE_NO_TEXT` figures — so no label can be matched and nothing is forced.

**Also unchanged by design:** "show me the relevant figure" and "what structure is indicated by the
arrow" both abstain `INSUFFICIENT_EVIDENCE` with `VISUAL_INTERPRETATION_UNAVAILABLE`. The
sufficiency gate classifies a request to see a picture as visual-dependent (ADR-023), and an
abstention carries no figures by contract. Changing that is a sufficiency decision, out of scope
here.

## Interface design system (post-M12, uncommitted)

A presentation-only pass. **No RAG behaviour changed**: retrieval, fusion, reranking, evidence,
sufficiency, generation, verification, citation semantics, figure-selection semantics, SSE progress
semantics, conversation persistence, authentication, tenant isolation and every API contract are
untouched. No backend file was modified. See `docs/architecture/design-system.md` for the tokens,
the principles and the measured accessibility results.

**What changed.** The stylesheet was reorganised from one 293-line file of accumulated literals
into `frontend/src/styles/{tokens,base,shell,ask,components}.css`, with every colour, space, radius
and type step named once in `tokens.css` and nothing outside it writing a literal colour. The rail,
the header and the Ask surface were redesigned around those tokens. `lucide-react` was added and
the rail's typographic glyphs (a diamond, a gear character, box-drawing marks) were replaced by one
outline icon set, all `aria-hidden` with the text label supplying the accessible name.

**Defects fixed, each verified in the running application:**

- The question bubble rendered muted grey on deep teal at roughly 1.5:1, because a generic
  `article p` rule overrode the bubble's own colour. It is white on the accent at **7.82:1**.
- The conversation list forced a horizontal scrollbar into the rail and scrolled its titles
  sideways, so entries read as `?` and `n?`. A grid child without `min-inline-size: 0` was sizing
  the column to the longest untruncated title.
- The composer's focus ring was a pale tint measuring **1.1:1**; it is now the application's amber
  ring on the wrapping box.
- The scrollable source excerpt is focusable because it scrolls, and carried only the browser's
  default 1px outline. It now has an explicit tab stop, an accessible name and the application ring.
- `M10 / Configuration` — an internal milestone marker from ADR-015 — was removed from the product
  shell. The header now names the page; the workspace, the role and sign-out moved from a loose
  line above the page content into an account menu. `AccessGate` no longer emits that line, which
  also removes the duplicate it produced on Operations (two gates mount there).
- The educational-use notice was consuming roughly a third of the first screen. It is a compact
  persistent row with the full wording behind a disclosure; the safety meaning is unchanged.
- The finished progress record was repeating the answer's own status line directly above it. It now
  renders after the answer, where a record of how the answer was produced belongs.

**Measured results:** 25/25 text pairs meet WCAG 2.1 AA; 22/22 keyboard stops paint a visible
indicator; no sideways scrolling at 150% or 200% zoom; no horizontal overflow at 390px.

**Three stale e2e assertions were repaired, none of them introduced here.** Each names the commit
that broke it:

- `e2e/progress.spec.ts` asserted a heading `How this answer was produced`, which stopped existing
  at `e4bd3d3` when the finished stepper became a disclosure row. It now asserts the record row and
  opens it before checking the stages.
- `e2e/workspace.spec.ts` asserted a marketing heading `Evidence comes first.`, removed at `73eb049`
  when Ask became a conversation. It now asserts the shell a signed-out visitor actually lands on,
  and gained an assertion that no account control is offered when there is no principal.
- `e2e/retrieval.spec.ts` clicked `Retrieval inspector` as a top-level rail entry; `73eb049` folded
  it under the Advanced tools disclosure. It now opens the disclosure first.

Neither spec had been touched between `73eb049` and this work, so both had been failing since that
commit. Anything that reported those runs as green was reading the exit status of a pipeline whose
last stage was `tail`, not Playwright's.

**Frontend tests 174/174 pass; `tsc -b` and `npm run build` clean.** Test updates were confined to
the places where presentation intentionally changed: seven sign-in helpers that waited for the
principal's name now wait for the header's account control, and the outcome labels moved from
shouted constants (`SERVICE PROBLEM`) to concise sentence case (`Technical failure`). No
functionality assertion was weakened; the outcome-severity test was strengthened, asserting that
each of the six outcomes carries a distinct non-colour marker rather than one glyph per severity.

## RAG v1 freeze (post-M12)

Retrieval, reranking, sufficiency, generation and verification are **frozen**. The acceptance gates
below passed; further changes to those five layers need a new decision, not a tuning pass.

**Release gates, measured on the 26-question real-book set (8 VERIFIED):**

| Gate | Result |
|---|---|
| Unsupported user-visible answers | **0** |
| Answers released without VERIFIED | **0** |
| VERIFIED answers with zero citations | **0** |
| VERIFIED answers citing a figure block | **0** |
| VERIFIED answers on unsupported-class questions | **0** |
| VERIFIED answers on ambiguous/personal questions | **0** |
| Assessment-only citations in a released answer | **0** |
| Ingestion silently losing material body text | **0** (ADR-025) |

**Repeatability, 10 trials each.** S1 7 VERIFIED / 3 UNVERIFIED · S9 8 / 2 · U2 10/10
`INSUFFICIENT_EVIDENCE` + `EVIDENCE_DOES_NOT_ADDRESS_QUESTION` · A3 10/10 `OUT_OF_SCOPE` · S2 10/10
`FAILED`. Across 15 released answers: **0** cited a figure and **0** lacked citations. Supported
questions vary 20-30% between VERIFIED and UNVERIFIED, always in the safe direction — M8 refusing a
draft, never releasing one. Generation temperature was **not** changed in response.

**S2 is refused by the provider, not by this system.** `Provider rejected the request (400):
bio_policy.` — deterministic, 10 of 10, and unique to that question among the 26. The provider's
biosecurity filter refuses a question about culturing Legionella. No answer is released and the
reader is told it is a technical failure rather than a statement about the evidence, which is
accurate. **This is an operator decision** (model or policy tier), not a code defect, and no retry
can help a deterministic 400.

## Known risks

Security: no malware scanning; rate limiting is **per replica, not global**; base images pinned by
tag not digest; no token-revocation check; audit tamper-evidence stops at the database boundary;
`MEDRAG_ENVIRONMENT` can be wrongly set to `development`; prompt-layer injection defence is
probabilistic (the structural guarantee is M8's deterministic checks).

Operational: no backup scheduling, PITR or cross-region replication; no RPO/RTO; no complete
deletion workflow (its central policy conflict is undecided); no CI config or Kubernetes manifests
ship; no OTel exporter or alerting deployed; worker is one parse per pod — scale by adding workers,
never by raising concurrency. Container memory limits and restart policies **are** now set from
measured peaks (worker 6G against 4.74 GiB observed). Four
`GENERATION_SCHEMA_VIOLATION` outcomes were observed under concurrent vague questions with no
pre-M12 baseline for comparison.

Quality — **unchanged from M11 and not hidden by hardening**: EvidenceSet coverage 0.933 not 1.0;
verifier is the same model as the generator so verification is not independent; the conflict
detector misses disagreements phrased with different label windows; visual interpretation
unavailable; evaluation is synthetic-only and nothing is expert-reviewed.

## Boundary

**No claim of HIPAA compliance, HIPAA certification, clinical validation, production certification
or absence of hallucination is made anywhere.** The controls support future compliance work; they do
not constitute it.

**M12 is the final planned milestone. Do not create M13.** Remaining gaps are classified in
`docs/verification/m12.md` §32 as production blockers, known operational limitations, future
enhancements, or clinical-validation requirements. No automatic commit.
