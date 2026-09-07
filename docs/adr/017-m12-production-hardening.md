# ADR-017: Production is a configuration the system refuses to run badly

Status: Accepted for M12. Baseline `10930c4` (committed M11). Preserves ADRs 010–016. The final
planned milestone.

## The refusal replaces a refusal

Before M12, `Settings` contained a validator that rejected `environment=production` outright with
"production security is not implemented". That was honest while it was true. M12's job was not to
delete it but to *earn* its removal: it is now an enumeration of the controls that exist, and
production still refuses to start when any of them is unmet.

Expressing this as a typed validator rather than a startup script or a warning is the whole point.
A script can be bypassed by importing the app directly; a warning can be ignored; a log line is
read after the incident. A `Settings` object that cannot be constructed cannot serve a request.

Every rule names the setting that is wrong and none prints a value, so the failure is actionable
without becoming a disclosure.

## Development authentication gets two barriers, not one

The static development tokens have no expiry, no revocation and no issuer. Carrying them into
production is the single most damaging configuration mistake this system can make, so it is
refused twice: once by the settings validator, and again by `build_auth_provider` when handed a
production configuration selecting the development adapter.

The second barrier exists because the first can be bypassed — `model_construct` skips validation,
and test helpers and future refactors use it. One barrier that is usually sufficient is not the
same as a control.

## Vendor-neutral identity, and what the token is not allowed to choose

OIDC is configured by issuer, audience and JWKS URI. No code knows whether the issuer is Entra ID,
Auth0 or Okta, and the claims carrying tenant and roles are configuration.

The decisions that matter are the ones the token does not get to make. Verification algorithms come
from configuration, never from the token header, so `alg: none` and an RS256→HS256 downgrade are
both unreachable. Only asymmetric algorithms are supported at all: accepting a symmetric one would
mean this service holds a key capable of *minting* tokens, which turns a verifier into an issuer.
Signature verification has no disable flag, because a flag that disables it is the vulnerability.
A missing tenant claim is refused rather than defaulted — defaulting would place every unmapped
user in one shared dataset, which is precisely the cross-tenant failure the architecture exists to
prevent. A token carrying several mapped roles resolves to the narrowest, so adding a group to a
user can never quietly widen what a mapped role already allowed.

Rejections return one opaque message. Distinguishing "expired" from "wrong audience" tells an
attacker which half of a forgery succeeded; the specific reason is logged server-side instead.

## Rate limiting is honest about being per-replica

The limiter is in-process. A Redis-backed one here would have been easy and would have implied a
guarantee it could not keep: N replicas would each allow the full budget while the code suggested a
global ceiling. What exists is a real per-replica bound that stops one client saturating a worker or
spending a provider budget, and the deployment documentation states the multiplication plainly.
Global limiting belongs at the ingress, where the whole fleet's traffic is visible.

It defaults **off** outside production and is *required* on in production. That combination came
from a real finding: enabling it by default immediately broke the test suite's own fixtures, which
upload many documents as one principal. A harness driving fixtures is not abuse, and a control that
obstructs legitimate local work is one that gets disabled. Mandatory where it matters, absent where
it would only get in the way.

## Prompt injection: the prompt is the weaker half

Both the generator and verifier policies now declare their inputs untrusted quoted data, evidence
is fenced with markers a document cannot forge or close, and the operator's instruction is repeated
after the untrusted region so the last thing read is the operator's.

Neutralisation deliberately touches *only* the fence markers. A sanitiser that rewrote evidence
text would break grounding in order to prevent injection — numbers, units and doses must reproduce
exactly, and trading that away would defeat the thing being protected.

But the prompt-level defence is the weaker half and is documented as such. Even a fully obeyed
injection cannot release an answer: M8's numeric, negation, citation-identity and provenance checks
are deterministic code, not model judgement, and M9 returns an answer only on a PASS. An injected
instruction can at most cause an abstention. Prompt hardening reduces noise; the structure is what
provides the guarantee.

## Usage accounting is deliberately outside the safety path

M11 recorded that cost was unreportable because the adapters discarded the provider `usage` block.
M12 captures it — and does so through an injected sink rather than by threading it into
`GroundedDraft`, which is a frozen safety object. Recording never raises, so telemetry cannot fail a
verified answer, and a response without usage increments an explicit `usage_absent` counter rather
than being counted as zero.

No price is hardcoded. Rates change, and a constant compiled into a report is worse than no figure;
`estimated_cost` returns None when the deployment has supplied no rate table, because reporting
0.0 would read as "this was free".

## What M12 did not do, and why that is the right outcome

No fast path, no verification-disabled mode, no provider-direct answering, no fallback to
pretrained knowledge. The pipeline is unchanged: hardening added barriers around it and removed
none from it. A test asserts the codebase contains no `skip_verification`, `bypass_gate`,
`allow_unverified` or `fast_path`.

Several requirements were met with documentation rather than code, and each is recorded as a gap
rather than described as built: malware scanning, global rate limiting, container resource limits,
digest-pinned base images, backup scheduling, off-host audit shipping, and a complete deletion
workflow. The deletion case is the clearest example — the design is written out, including the
unresolved conflict between erasing a source and preserving a verified answer that cites it, and
the conclusion is that it is a policy decision that must be made before code is written. Writing
the workflow without that decision would have produced a feature that deletes the wrong thing.

## Consequences

One dependency was added: `pyjwt[crypto]`, as a core requirement rather than an extra. Production
authentication is not optional, and an extra a deployment forgets to install is exactly how
authentication silently falls back to the development adapter.

Two existing tests were rewritten rather than deleted. `test_unhardened_production_cannot_start`
asserted a blanket refusal that M12 replaced with itemised rules, so it now asserts the specific
rules; `test_liveness_does_not_require_infrastructure` matched an exact response body that gained
operator-facing identity, so it now asserts the invariant it was written for — liveness reports
alive while a dependency is down and says nothing about dependency state — more precisely than the
exact match did.

Every known quality limitation from M11 survives untouched: EvidenceSet coverage below 1, the
generator and verifier being the same model, the lexically-distant conflict gap, visual
interpretation being unavailable, synthetic-only evaluation, and no expert review. Production
hardening does not erase quality limitations, and no claim of HIPAA compliance, certification,
clinical validation or absence of hallucination is made anywhere.
