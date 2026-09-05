---
name: medical-grounding
description: Implement or review evidence sufficiency, claim verification, citations, conflicts and abstention.
---

# Medical Grounding

Read docs/architecture/grounding.md and ADR-004. Trace every proposed answer path from authorized active evidence through sufficiency and claim verification.

Check that material claims in visible text all belong to the verified claim set and resolve to original source provenance. Reject generated metadata as sole support. Exercise missing evidence, invalid citation IDs/pages, authoritative conflict, incorrect assessment keys and unavailable original images.

Stream progress before verification; release medical text only after verification. No more than one policy-authorized regeneration precedes abstention. Keep patient-specific advice out of the educational scope and never substitute raw model confidence for evidence calibration.
