"""The grounding contract sent to a provider, and the evidence rendering it may read.

The prompt is versioned because it is part of the decision record: a draft is only reproducible if
the exact instruction that produced it is recoverable from the trace.
"""

from app.core.generation_config import GroundingConfig
from app.evidence.model import EvidenceBlock

SYSTEM_POLICY = """\
You are assisting an accuracy-first medical evidence workspace. You are not a medical authority and
you are not the source of the answer.

Pretrained model knowledge is not valid evidence for this answer.

Rules, all mandatory:
1. Use ONLY the numbered evidence blocks supplied in this request. They are the entire corpus you
   may draw on. You have no other source.
2. Every statement you make must be bound to the evidence block or blocks it came from, by their
   exact evidence_id. Never invent, guess, abbreviate or reformat an evidence_id, a document id, a
   page number or a source identifier.
3. If the evidence does not cover part of the question, say so in evidence_gap and leave it out of
   the answer. Do not complete it from what you already know. An incomplete grounded answer is
   correct behaviour; a fluent unsupported one is not.
4. Do not infer a medical fact that the evidence does not state. Do not resolve a disagreement
   between sources. Do not treat a question-bank answer or an answer key as established fact.
5. Do not describe or interpret an image. A caption tells you a figure exists; it does not tell you
   what the figure shows.
6. Reproduce numbers, units, doses and identifiers exactly as the evidence states them.
7. Answer in plain clinical-educational prose. Do not address an individual patient and do not give
   personal medical advice.

Return only the required structured object.\
"""


def render_evidence(blocks: list[EvidenceBlock], config: GroundingConfig) -> str:
    """Render the approved blocks. Only source text and identity; no scores and no ranks.

    Rank and score are retrieval diagnostics. Showing them to a generator would invite it to treat
    the top-ranked block as the most true one, which is exactly the substitution this architecture
    exists to prevent.
    """
    parts = []
    for index, block in enumerate(blocks[: config.max_evidence_blocks], 1):
        header = (
            f"[{index}] evidence_id={block.evidence_id}\n"
            f"    source: {block.document_title} ({block.source_type}, "
            f"authority {block.authority_level})\n"
            f"    type: {block.chunk_type}; pages: "
            f"{', '.join(str(p) for p in block.pages) or 'unlocated'}"
        )
        if block.hierarchy:
            header += "\n    section: " + " > ".join(h.text for h in block.hierarchy)
        if block.requires_visual_evidence:
            header += "\n    note: original figure not interpreted; caption text only"
        if block.question:
            header += "\n    note: assessment material; a recorded key is not established fact"
        parts.append(header + "\n    text: " + block.text.replace("\n", "\n           "))
    return "\n\n".join(parts)


def user_message(question: str, evidence: str) -> str:
    return (
        "Question:\n"
        + question
        + "\n\nEvidence blocks (the only permitted source):\n"
        + evidence
        + "\n\nAnswer strictly from the blocks above, citing evidence_id values that appear there."
    )
