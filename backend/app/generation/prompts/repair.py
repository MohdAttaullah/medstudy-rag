"""The single controlled repair instruction.

A repair may only narrow an answer. It gets the same evidence, never more, and is told exactly
which statements failed and why. It is not an invitation to try again for a better answer — it is
an instruction to remove what the evidence does not support.
"""

from collections.abc import Sequence

from app.generation.prompts.grounded import SYSTEM_POLICY
from app.verification.model import ClaimVerification

REPAIR_POLICY = (
    SYSTEM_POLICY
    + """

This is a repair of a previous draft that failed verification.

Additional rules for this attempt:
8. Remove or correct every statement listed as failing below. Correct one only if the supplied
   evidence already states the correction; otherwise remove the statement entirely.
9. Do not add any new statement that was not supported by this same evidence. You have no new
   evidence, and none is coming.
10. A shorter answer that says less is the correct outcome. Removing an unsupported statement is
    success, not failure. If nothing survives, say so in evidence_gap and keep the answer minimal.
11. Do not restate a removed claim in weaker words. It must be gone, not rephrased.\
"""
)


def repair_message(question: str, evidence: str, failures: Sequence[ClaimVerification]) -> str:
    lines = []
    for failure in failures:
        reasons = ", ".join(failure.reason_codes)
        lines.append(f'- "{failure.claim_text}" — {failure.verdict}: {reasons}')
    return (
        "Question:\n"
        + question
        + "\n\nEvidence blocks (unchanged; this is still the only permitted source):\n"
        + evidence
        + "\n\nStatements from your previous draft that failed verification:\n"
        + "\n".join(lines)
        + "\n\nProduce a corrected draft using only the evidence above."
    )
