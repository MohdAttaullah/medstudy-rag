# M11 evaluation report

**M11 engineering evaluation is not clinical validation.** Every dataset below is synthetic, none is expert-reviewed, and no number here is evidence about clinical performance. There is deliberately no single accuracy score: the layers measure different things and are not commensurable.

## Run identity

| Field | Value |
|---|---|
| Run id | `f00ec809e72140df875a0943b87921d1` |
| Mode | live |
| Commit | `e0ee0ad5e970409543712907dbfe4bdf92bf2af1` (dirty) |
| Timestamp | 2026-09-07T13:27:50.285368+00:00 |
| Configuration revision | — |
| Dataset manifest | `47af1814b27f8198` |
| Generator | gpt-5.6-sol |
| Verifier | not configured — M8 reuses the generator (gpt-5.6-sol) |
| Verifier independent of generator | no |

## Datasets

| Dataset | Version | Layer | Independence | Review |
|---|---|---|---|---|
| parsing-gold | `parsing-gold-m2-v1` | PARSING | IMPLEMENTATION_ADJACENT | ENGINEER_REVIEWED |
| chunking-gold | `chunk-gold-m3-v1` | CHUNKING | IMPLEMENTATION_ADJACENT | ENGINEER_REVIEWED |
| embedding-gold | `embedding-gold-m4-v1` | INDEXING | IMPLEMENTATION_ADJACENT | ENGINEER_REVIEWED |
| retrieval-gold | `retrieval-gold-m5-v1` | RETRIEVAL | FROZEN_REGRESSION | ENGINEER_REVIEWED |
| context-gold | `m6-context-gold-v1` | EVIDENCE | IMPLEMENTATION_ADJACENT | ENGINEER_REVIEWED |
| sufficiency-gold | `m7-sufficiency-gold-v1` | SUFFICIENCY | IMPLEMENTATION_ADJACENT | ENGINEER_REVIEWED |
| verification-gold | `m8-verification-gold-v1` | VERIFICATION | IMPLEMENTATION_ADJACENT | ENGINEER_REVIEWED |
| bootstrap-cases | `synthetic-contract-v1` | END_TO_END | IMPLEMENTATION_ADJACENT | UNREVIEWED |
| m11-heldout | `m11-heldout-v1` | END_TO_END | HELD_OUT | ENGINEER_REVIEWED |

7 implementation-adjacent, 1 frozen regression, 1 held out, 0 expert-reviewed.

## Layers

### Parsing

Dataset `parsing-gold` · independence **IMPLEMENTATION_ADJACENT**

| Metric | Value |
|---|---|
| cases evaluated | 9 |
| passed cases | 9 |
| checks | 142 |
| failure count | 0 |
| parser | docling 2.126.0 |

### Chunking

Dataset `chunking-gold` · independence **IMPLEMENTATION_ADJACENT**

| Metric | Value |
|---|---|
| cases evaluated | 14 |
| checks | 94 |
| passed cases | 14 |
| failure count | 0 |

### Embedding / index

Dataset `embedding-gold` · independence **IMPLEMENTATION_ADJACENT**

| Metric | Value |
|---|---|
| cases evaluated | 9 |
| passed cases | 9 |
| checks | 180 |
| failure count | 0 |
| dimension | 768 |
| pooling | CLS |
| distance | DOT |
| vector space fingerprint | 9f291d23639dded37d6633fe4db4a10ac26e284a664f343a28d9789f55deb8c3 |

### Retrieval

Dataset `retrieval-gold` · independence **FROZEN_REGRESSION**

| Metric | Value |
|---|---|
| cases evaluated | 22 |
| candidate pool depth | 20 |
| candidate pool recall | 1.000 |
| first stage miss rate | 0.000 |
| hybrid recall at 5 | 1.000 |

Per lane, reported separately:

| Lane | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@5 |
|---|---|---|---|---|---|
| BM25 | 0.659 | 0.871 | 0.955 | 0.927 | 0.912 |
| DENSE | 0.750 | 0.977 | 1.000 | 1.000 | 0.979 |
| HYBRID_RRF | 0.750 | 0.977 | 1.000 | 1.000 | 0.981 |

### Reranking

Dataset `retrieval-gold` · independence **FROZEN_REGRESSION**

| Metric | Value |
|---|---|
| cases evaluated | 24 |
| candidate pool depth | 20 |
| candidate pool recall | 1.000 |
| baseline mrr | 1.000 |
| reranked mrr | 1.000 |
| baseline ndcg at 5 | 0.981 |
| reranked ndcg at 5 | 0.997 |
| first stage miss count | 0 |
| reranker regression count | 0 |
| reranker no gain count | 0 |
| inference mean ms | 1436.534 |

### EvidenceSet

Dataset `context-gold` · independence **IMPLEMENTATION_ADJACENT**

| Metric | Value |
|---|---|
| effective policy | neighbours-1-budget-4096 |
| element coverage | 0.933 |
| character coverage | 0.897 |
| noise ratio | 0.033 |

### Evidence sufficiency

Dataset `sufficiency-gold` · independence **IMPLEMENTATION_ADJACENT**

| Metric | Value |
|---|---|
| cases evaluated | 14 |
| agreement | 1.000 |
| false allow count | 0 |
| unnecessary abstention count | 0 |
| abstention rate | 0.714 |

### Generation

Dataset `sufficiency-gold` · independence **IMPLEMENTATION_ADJACENT**

| Metric | Value |
|---|---|
| cases evaluated | 14 |
| generation attempted | 4 |
| generation suppressed | 10 |
| schema valid rate | 1.000 |
| citation valid rate | 1.000 |
| invented citation count | 0 |
| rank or score leaked to provider | no |

### Claim verification

Dataset `verification-gold` · independence **IMPLEMENTATION_ADJACENT**

| Metric | Value |
|---|---|
| cases evaluated | 19 |
| agreement | 1.000 |
| false pass count | 0 |
| unsupported claim detection rate | 1.000 |
| citation failures detected | 2 |
| contradiction cases detected | 2 |
| repair attempted | 2 |
| repair success rate | 0.500 |
| post repair unsupported | 1 |
| repair cap respected | yes |

### End-to-end Ask

Dataset `m11-heldout` · independence **HELD_OUT**

| Metric | Value |
|---|---|
| cases evaluated | 25 |
| gate agreement | 1.000 |
| outcome agreement | 1.000 |
| abstention rate | 0.760 |
| answered when unanswerable count | 0 |
| answer without verified count | 0 |
| refused when answerable count | 0 |
| layer attribution agreement | 1.000 |
| repair cap respected | yes |

Failures attributed upstream: EVIDENCE: 2, GENERATION: 1, SUFFICIENCY: 10, VERIFICATION: 6

### Security

Dataset `—` · independence **N/A**

| Metric | Value |
|---|---|
| checks run | 13 |
| violations | none |

### Live provider

Dataset `m11-heldout` · independence **HELD_OUT**

| Metric | Value |
|---|---|
| cases evaluated | 5 |
| provider | openai |
| model id | gpt-5.6-sol |
| verifier model id | gpt-5.6-sol |
| verifier independent | no |
| schema valid | yes |
| citations valid | yes |
| invented citations | 0 |
| median latency ms | 4120.600 |
| suppressed before provider | 2 |
| token usage available | no |
| cost note | Cost cannot be reported: the provider adapter does not capture the response usage block, so no token counts exist. Latency is the only operational figure available, and it is a development-host measurement, not an SLO. |

## Quality gates

| Gate | Kind | Observed | Bound | Status |
|---|---|---|---|---|
| `verification.no_false_pass` | HARD_SAFETY_INVARIANT | 0 | ≤ 0 | PASSED |
| `sufficiency.no_false_allow` | HARD_SAFETY_INVARIANT | 0 | ≤ 0 | PASSED |
| `generation.no_invented_citations` | HARD_SAFETY_INVARIANT | 0 | ≤ 0 | PASSED |
| `end_to_end.no_answer_without_verification` | HARD_SAFETY_INVARIANT | 0 | ≤ 0 | PASSED |
| `end_to_end.no_unsupported_answer_on_insufficient` | HARD_SAFETY_INVARIANT | 0 | ≤ 0 | PASSED |
| `security.no_cross_tenant_access` | HARD_SAFETY_INVARIANT | 0 | ≤ 0 | PASSED |
| `taxonomy.every_code_attributed` | HARD_SAFETY_INVARIANT | 0 | ≤ 0 | PASSED |
| `retrieval.hybrid_recall_at_5` | ENGINEERING_REGRESSION_THRESHOLD | 1.000 | ≥ 0.8 | PASSED |
| `evidence.required_element_coverage` | ENGINEERING_REGRESSION_THRESHOLD | 0.933 | ≥ 0.9 | PASSED |
| `chunking.no_expectation_failures` | ENGINEERING_REGRESSION_THRESHOLD | 0 | ≤ 0 | PASSED |
| `verification.repair_cap_respected` | ENGINEERING_REGRESSION_THRESHOLD | 0 | ≤ 0 | PASSED |
| `sufficiency.unnecessary_abstention_count` | OBSERVATIONAL_METRIC | 0 | none asserted | RECORDED |
| `end_to_end.abstention_rate` | OBSERVATIONAL_METRIC | 0.760 | none asserted | RECORDED |
| `retrieval.first_stage_miss_rate` | OBSERVATIONAL_METRIC | 0.000 | none asserted | RECORDED |
| `reranking.regression_count` | OBSERVATIONAL_METRIC | 0 | none asserted | RECORDED |
| `end_to_end.answer_correctness` | UNCALIBRATED | — | none asserted | NOT_MEASURED |
| `generation.latency_ms` | UNCALIBRATED | — | none asserted | NOT_MEASURED |

Hard safety invariants: **7/7 upheld**. Failed gates: none.

## Boundary

No claim of clinical validation, medical certification, guaranteed accuracy or absence of hallucination is made or implied by anything above. Establishing medical quality requires expert-reviewed datasets that this repository does not have.