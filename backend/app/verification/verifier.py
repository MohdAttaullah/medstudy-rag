"""Semantic claim verification behind a provider-neutral abstraction.

The verifier sees one claim and the evidence blocks that claim cites. It has no corpus handle, no
retrieval, no web search and no licence to supply a missing fact from what it already knows. It can
only ever *fail* a claim that the deterministic layer passed; it can never rescue one the
deterministic layer failed.
"""

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import ModelSelection, Settings
from app.core.generation_config import GroundingConfig
from app.evidence.model import EvidenceBlock
from app.generation.errors import GenerationError
from app.generation.providers.base import LLMProvider
from app.generation.providers.factory import build_named_provider
from app.verification.model import Verdict, VerificationError, VerifierSpec

PROMPT_VERSION = "claim-verification-v1"
SCHEMA_VERSION = "claim-verification-schema-v1"

SYSTEM_POLICY = """\
You are checking whether a single statement is supported by the evidence supplied with it. You are
not answering a question and you are not correcting the statement.

Pretrained model knowledge is not evidence. If the supplied evidence does not establish the
statement, it is not supported, no matter how plausible or well known you believe it to be.

Decide exactly one verdict:
- SUPPORTED: the evidence states the whole statement, including every number, unit, qualifier and
  negation in it.
- CONTRADICTED: the evidence states something incompatible with the statement.
- INSUFFICIENT_EVIDENCE: the evidence is about the right subject but does not settle the statement.
- UNSUPPORTED: the evidence does not establish the statement.

Rules:
1. Judge the whole statement. If a statement joins two propositions and the evidence establishes
   only one, it is not SUPPORTED.
2. A qualified source does not support an unqualified claim. "may be associated with" does not
   support "causes".
3. Numbers, units and negations must match exactly. Do not treat 5 mg and 50 mg as equivalent, and
   do not overlook a "not".
4. A question bank or answer key records what an examiner marked. It is not a medical reference.
5. Cite only the evidence_id values supplied to you. Never invent one.

Return only the required structured object.\
"""


class VerifiableClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    claim_id: UUID
    text: str
    evidence: list[EvidenceBlock]


class VerifierVerdict(BaseModel):
    """Exactly what a verifier may return. Anything else is malformed output."""

    model_config = ConfigDict(extra="forbid")
    verdict: Verdict
    supporting_evidence_ids: list[UUID] = Field(default_factory=list)
    contradicting_evidence_ids: list[UUID] = Field(default_factory=list)
    # Free prose is recorded for a reviewer and never used as the machine decision.
    rationale: str = Field(default="", max_length=2000)


class ClaimVerifier(Protocol):
    @property
    def specification(self) -> VerifierSpec: ...

    async def verify(self, claim: VerifiableClaim) -> VerifierVerdict: ...


def render(claim: VerifiableClaim) -> str:
    parts = []
    for block in claim.evidence:
        parts.append(
            f"evidence_id={block.evidence_id}\n"
            f"  source: {block.document_title} ({block.source_type}, "
            f"authority {block.authority_level})\n"
            f"  text: {block.text}"
        )
    return "\n\n".join(parts) or "(no evidence supplied)"


class ModelClaimVerifier:
    """Wraps an `LLMProvider`. No vendor detail here; adapters stay the only place it lives."""

    def __init__(self, provider: LLMProvider, independent: bool) -> None:
        self._provider = provider
        self._independent = independent

    @property
    def specification(self) -> VerifierSpec:
        spec = self._provider.specification
        return VerifierSpec(
            verifier="model",
            provider=spec.provider,
            model_id=spec.model_id,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            independent_of_generator=self._independent,
        )

    async def verify(self, claim: VerifiableClaim) -> VerifierVerdict:
        supplied = {block.evidence_id for block in claim.evidence}
        try:
            produced = await self._provider.generate_structured(
                system_policy=SYSTEM_POLICY,
                question="Statement to check:\n" + claim.text,
                evidence=render(claim),
                schema=VerifierVerdict,
            )
        except GenerationError as exc:
            # Fail closed. A verifier that could not answer has not approved anything.
            raise VerificationError(
                "VERIFIER_FAILED", f"Verifier unavailable ({exc.code})."
            ) from None
        except ValidationError:
            raise VerificationError("VERIFIER_MALFORMED_OUTPUT") from None
        cited = set(produced.supporting_evidence_ids) | set(produced.contradicting_evidence_ids)
        if not cited <= supplied:
            # A verifier introducing evidence nobody gave it is not a verdict to act on.
            raise VerificationError("VERIFIER_UNKNOWN_EVIDENCE")
        return produced


class FakeClaimVerifier:
    """Deterministic double so the whole M8 flow is testable without a key or a network."""

    def __init__(
        self,
        decide: Callable[[VerifiableClaim], object] | None = None,
        model_id: str = "fake-verifier-1",
    ) -> None:
        self.decide = decide or (lambda claim: VerifierVerdict(verdict="SUPPORTED"))
        self.model_id = model_id
        self.seen: list[VerifiableClaim] = []

    @property
    def specification(self) -> VerifierSpec:
        return VerifierSpec(
            verifier="deterministic-fake",
            provider="fake",
            model_id=self.model_id,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            independent_of_generator=True,
        )

    async def verify(self, claim: VerifiableClaim) -> VerifierVerdict:
        self.seen.append(claim)
        result = self.decide(claim)
        if isinstance(result, BaseException):
            raise result
        try:
            return VerifierVerdict.model_validate(result)
        except ValidationError:
            # Mirror the real adapter exactly. A double that fails differently from the thing it
            # stands in for would test a path that does not exist in production.
            raise VerificationError("VERIFIER_MALFORMED_OUTPUT") from None


def build_verifier(settings: Settings) -> ClaimVerifier:
    """Prefer a verifier distinct from the generator, and say so honestly when it is not.

    A verifier that is the same model as the generator shares its blind spots, so a claim the
    generator was confident about is exactly the one it is least likely to fail. That is a real
    limitation rather than a reason to require two paid accounts, so it is configurable and
    recorded in the spec instead of being enforced.
    """
    selection: ModelSelection | None = settings.verifier or settings.generator
    if selection is None:
        raise VerificationError("VERIFIER_FAILED", "No verifier or generator model is configured.")
    independent = settings.verifier is not None and settings.verifier != settings.generator
    provider = build_named_provider(settings, selection, GroundingConfig())
    return ModelClaimVerifier(provider, independent)
