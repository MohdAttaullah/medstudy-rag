"""Deterministic citation binding.

M7 checks that every citation names an evidence block that was actually supplied. That is a
contract check, not a support check: it proves the model did not invent a source, and says nothing
about whether the cited block actually supports the sentence. Establishing that is M8's claim
verification; calling this "verified" would collapse a distinction the architecture depends on.
"""

from uuid import UUID

from app.generation.errors import GenerationError
from app.generation.grounding.model import AnswerDraft


def bind(draft: AnswerDraft, approved: list[UUID]) -> tuple[list[UUID], list[UUID]]:
    """Return the approved ids the draft cited and those it did not.

    Raises on any id outside the supplied set, including one belonging to a real block of another
    tenant or another request: the only acceptable citation is one from this EvidenceSet.
    """
    permitted = set(approved)
    cited: list[UUID] = []
    for claim in draft.claims:
        for evidence_id in claim.evidence_ids:
            if evidence_id not in permitted:
                raise GenerationError(
                    "GENERATION_UNKNOWN_CITATION",
                    "The draft cited evidence that was not supplied to it.",
                )
            if evidence_id not in cited:
                cited.append(evidence_id)
    if not cited:
        raise GenerationError(
            "GENERATION_MISSING_CITATION", "The draft bound no statement to any evidence."
        )
    order = {value: index for index, value in enumerate(approved)}
    return (
        sorted(cited, key=lambda value: order[value]),
        [value for value in approved if value not in set(cited)],
    )
