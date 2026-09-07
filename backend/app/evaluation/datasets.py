"""What each evaluation dataset is, and what it is therefore allowed to be evidence *of*.

Most of this repository's gold files were written during the milestone they measure. That is a
reasonable way to build a pipeline and a poor way to claim it generalises: a fixture authored
alongside the policy it scores is a statement that the implementation does what its author
intended, not that it works on cases nobody anticipated. The distinction is invisible once results
reach a table, so it is recorded here in code, carried into every report, and printed next to the
numbers rather than buried in a caveats section.

`Provenance` says where the cases came from. `Independence` says what may be concluded from them.
They are separate axes on purpose: a synthetic dataset can still be held out, and a dataset derived
from real sources can still be implementation-adjacent.
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Provenance = Literal["SYNTHETIC", "REAL_SOURCE_DERIVED"]
Independence = Literal["IMPLEMENTATION_ADJACENT", "FROZEN_REGRESSION", "HELD_OUT"]
Review = Literal["UNREVIEWED", "ENGINEER_REVIEWED", "EXPERT_REVIEWED"]

_INDEPENDENCE_MEANING: dict[Independence, str] = {
    "IMPLEMENTATION_ADJACENT": (
        "Authored while implementing the behaviour it measures. Evidence that the implementation "
        "matches its author's intent; not evidence of generalisation."
    ),
    "FROZEN_REGRESSION": (
        "Frozen after its milestone and used to detect regression. Evidence that behaviour has "
        "not changed; not evidence that the original behaviour was correct."
    ),
    "HELD_OUT": (
        "Authored after the policy under test was committed, and evaluated without tuning that "
        "policy afterwards. The only class here that speaks to unanticipated cases."
    ),
}

_REVIEW_MEANING: dict[Review, str] = {
    "UNREVIEWED": "No structured review recorded.",
    "ENGINEER_REVIEWED": (
        "Reviewed by an engineer for structural correctness. Not a medical review."
    ),
    "EXPERT_REVIEWED": "Reviewed by an appropriately qualified clinician.",
}


@dataclass(frozen=True)
class Dataset:
    """One evaluation dataset and the claims it can support."""

    dataset_id: str
    version: str
    path: str
    layer: str
    provenance: Provenance
    independence: Independence
    review: Review
    milestone: str
    description: str
    #: Commit at which the policy this dataset measures was already frozen. Only meaningful for
    #: HELD_OUT datasets, where it is the whole basis of the claim.
    policy_frozen_at: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def independence_meaning(self) -> str:
        return _INDEPENDENCE_MEANING[self.independence]

    @property
    def review_meaning(self) -> str:
        return _REVIEW_MEANING[self.review]

    @property
    def clinically_validated(self) -> bool:
        """Always false. Present so a report cannot omit the question by accident."""
        return False

    def fingerprint(self, root: Path) -> str:
        """SHA-256 of the dataset bytes. Changing a gold label changes this."""
        return file_fingerprint(root / self.path)

    def describe(self, root: Path) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "version": self.version,
            "path": self.path,
            "layer": self.layer,
            "provenance": self.provenance,
            "independence": self.independence,
            "independence_meaning": self.independence_meaning,
            "review": self.review,
            "review_meaning": self.review_meaning,
            "milestone": self.milestone,
            "description": self.description,
            "policy_frozen_at": self.policy_frozen_at,
            "clinically_validated": self.clinically_validated,
            "fingerprint": self.fingerprint(root),
            "notes": list(self.notes),
        }


def file_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_SYNTHETIC_NOTE = (
    "Content was written for this repository. It contains no real clinical guidance and no "
    "copyrighted medical corpus."
)

#: Every evaluation dataset in the repository. Adding a gold file without registering it here is
#: caught by `test_every_gold_file_is_governed`, so this list cannot quietly fall behind.
REGISTRY: tuple[Dataset, ...] = (
    Dataset(
        dataset_id="parsing-gold",
        version="parsing-gold-m2-v1",
        path="docs/evals/parsing-gold.json",
        layer="PARSING",
        provenance="SYNTHETIC",
        independence="IMPLEMENTATION_ADJACENT",
        review="ENGINEER_REVIEWED",
        milestone="M2",
        description="Hand-specified extraction fidelity over ten synthetic PDFs.",
        notes=(
            _SYNTHETIC_NOTE,
            "M2 documented that Docling semantic labels vary across platforms; label identity is "
            "not treated as deterministic ground truth.",
        ),
    ),
    Dataset(
        dataset_id="chunking-gold",
        version="chunk-gold-m3-v1",
        path="backend/tests/fixtures/chunking/gold.json",
        layer="CHUNKING",
        provenance="SYNTHETIC",
        independence="IMPLEMENTATION_ADJACENT",
        review="ENGINEER_REVIEWED",
        milestone="M3",
        description="Fourteen normalized parse datasets with expected chunk structure.",
        notes=(_SYNTHETIC_NOTE,),
    ),
    Dataset(
        dataset_id="embedding-gold",
        version="embedding-gold-m4-v1",
        path="backend/tests/fixtures/embedding/gold.json",
        layer="INDEXING",
        provenance="SYNTHETIC",
        independence="IMPLEMENTATION_ADJACENT",
        review="ENGINEER_REVIEWED",
        milestone="M4",
        description="Thirteen encoder inputs plus two ordering expectations.",
        notes=(
            _SYNTHETIC_NOTE,
            "Measures encoder and input-builder behaviour, not retrieval relevance.",
        ),
    ),
    Dataset(
        dataset_id="retrieval-gold",
        version="retrieval-gold-m5-v1",
        path="backend/tests/fixtures/retrieval/gold.json",
        layer="RETRIEVAL",
        provenance="SYNTHETIC",
        independence="FROZEN_REGRESSION",
        review="ENGINEER_REVIEWED",
        milestone="M5",
        description="Five documents, 83 chunks and 25 graded retrieval cases.",
        notes=(
            _SYNTHETIC_NOTE,
            "The corpus is small enough that Recall@10 and deeper barely discriminate; shallow "
            "metrics carry the signal.",
        ),
    ),
    Dataset(
        dataset_id="context-gold",
        version="m6-context-gold-v1",
        path="backend/tests/fixtures/reranking/context-gold.json",
        layer="EVIDENCE",
        provenance="SYNTHETIC",
        independence="IMPLEMENTATION_ADJACENT",
        review="ENGINEER_REVIEWED",
        milestone="M6",
        description="Required source elements per anchor over the frozen M3 parse fixtures.",
        notes=(_SYNTHETIC_NOTE,),
    ),
    Dataset(
        dataset_id="sufficiency-gold",
        version="m7-sufficiency-gold-v1",
        path="backend/tests/fixtures/sufficiency/gold.json",
        layer="SUFFICIENCY",
        provenance="SYNTHETIC",
        independence="IMPLEMENTATION_ADJACENT",
        review="ENGINEER_REVIEWED",
        milestone="M7",
        description="Fourteen gate decisions built over the frozen M5 retrieval corpus.",
        notes=(_SYNTHETIC_NOTE,),
    ),
    Dataset(
        dataset_id="verification-gold",
        version="m8-verification-gold-v1",
        path="backend/tests/fixtures/verification/gold.json",
        layer="VERIFICATION",
        provenance="SYNTHETIC",
        independence="IMPLEMENTATION_ADJACENT",
        review="ENGINEER_REVIEWED",
        milestone="M8",
        description="Nineteen claim-verification cases over ten evidence blocks.",
        notes=(_SYNTHETIC_NOTE,),
    ),
    Dataset(
        dataset_id="bootstrap-cases",
        version="synthetic-contract-v1",
        path="docs/evals/bootstrap-cases.json",
        layer="END_TO_END",
        provenance="SYNTHETIC",
        independence="IMPLEMENTATION_ADJACENT",
        review="UNREVIEWED",
        milestone="M0",
        description="Ten invented-symbol contract questions from the bootstrap milestone.",
        notes=(
            _SYNTHETIC_NOTE,
            "Written before most of the pipeline existed; it exercises contracts, not quality.",
        ),
    ),
    Dataset(
        dataset_id="m11-heldout",
        version="m11-heldout-v1",
        path="backend/tests/fixtures/evaluation/heldout.json",
        layer="END_TO_END",
        provenance="SYNTHETIC",
        independence="HELD_OUT",
        review="ENGINEER_REVIEWED",
        milestone="M11",
        policy_frozen_at="e0ee0ad",
        description=(
            "End-to-end cases authored in M11 against the committed M10 pipeline, covering every "
            "required category including conflict, outdated source, ambiguity and no-answer."
        ),
        notes=(
            _SYNTHETIC_NOTE,
            "Held out in the one sense this repository can honestly support: every policy these "
            "cases exercise was committed at e0ee0ad before the cases were written, so no case "
            "could have influenced a threshold. It is not a held-out sample of a real population, "
            "and it was authored by the same engineer who ran it.",
        ),
    ),
)

BY_ID: dict[str, Dataset] = {dataset.dataset_id: dataset for dataset in REGISTRY}


def describe_registry(root: Path) -> list[dict[str, Any]]:
    return [dataset.describe(root) for dataset in REGISTRY]


def manifest(root: Path) -> dict[str, Any]:
    """The frozen dataset manifest, fingerprinted as a whole.

    The aggregate fingerprint covers every dataset's identity *and* bytes, so a silently edited
    gold label changes the manifest fingerprint and any report that quotes it.
    """
    datasets = describe_registry(root)
    payload = json.dumps(
        [
            {k: d[k] for k in ("dataset_id", "version", "path", "independence", "fingerprint")}
            for d in datasets
        ],
        sort_keys=True,
    )
    return {
        "manifest_version": "m11-dataset-manifest-v1",
        "clinically_validated": False,
        "boundary": (
            "Engineering evaluation only. No dataset here is expert-reviewed, and none of it is "
            "clinical validation."
        ),
        "fingerprint": hashlib.sha256(payload.encode()).hexdigest(),
        "counts": {
            "total": len(datasets),
            **{
                level: sum(d["independence"] == level for d in datasets)
                for level in ("IMPLEMENTATION_ADJACENT", "FROZEN_REGRESSION", "HELD_OUT")
            },
            "expert_reviewed": sum(d["review"] == "EXPERT_REVIEWED" for d in datasets),
        },
        "datasets": datasets,
    }
