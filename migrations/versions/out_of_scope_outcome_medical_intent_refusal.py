"""Out-of-scope outcome: a turn refused on medical intent, before anything was retrieved.

Post-M12 acceptance hardening, not a milestone.

`conversation_turns.outcome` is text with a CHECK rather than a native enum precisely so that
adding an outcome is a migration rather than a type rewrite. This is that migration, and it is
the only schema change the intent policy needs: `OUT_OF_SCOPE` carries no answer and no citations,
so the two safety CHECKs that tie `answer_text` and `verified` to `VERIFIED` already hold for it
unchanged and are deliberately left alone.

The column is `String(24)` and `OUT_OF_SCOPE` is twelve characters, so no width change is needed —
`CONFLICTING_EVIDENCE` at twenty remains the longest member.

The downgrade is safe only while no refused turn exists. A stored `OUT_OF_SCOPE` row cannot satisfy
the older constraint, and rewriting one to a different outcome would put a policy refusal on record
as a statement about the evidence, which is exactly the conflation this outcome exists to end. So
the downgrade refuses instead, and `MEDRAG_ALLOW_CONVERSATION_LOSS=1` acknowledges the loss — the
same opt-in the M9 migration uses to protect question history, honoured here for the same reason
and by the same name, since both guard rows in `conversation_turns`.

Revision ID: out_of_scope_outcome
Revises: parse_review_decisions
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "out_of_scope_outcome"
down_revision = "parse_review_decisions"
branch_labels = None
depends_on = None

# Bare name: the metadata naming convention prefixes `ck_conversation_turns_`.
CONSTRAINT = "turn_outcome"
OUTCOMES = (
    "VERIFIED",
    "INSUFFICIENT_EVIDENCE",
    "CONFLICTING_EVIDENCE",
    "UNVERIFIED",
    "FAILED",
    "OUT_OF_SCOPE",
)
PRIOR = OUTCOMES[:-1]


def _outcome_check(values: tuple[str, ...]) -> None:
    op.drop_constraint(CONSTRAINT, "conversation_turns", type_="check")
    op.create_check_constraint(
        CONSTRAINT,
        "conversation_turns",
        "outcome IN (" + ", ".join(f"'{value}'" for value in values) + ")",
    )


def upgrade() -> None:
    _outcome_check(OUTCOMES)


def downgrade() -> None:
    if os.environ.get("MEDRAG_ALLOW_CONVERSATION_LOSS") == "1":
        # The loss is acknowledged, so the refused turns are removed rather than relabelled: the
        # narrower constraint cannot be applied while a row still violates it, and rewriting one
        # to another outcome is the misrepresentation this migration exists to avoid. The
        # integration harness proves every migration is reversible on a schema it created seconds
        # earlier, and those turns are fixtures rather than anyone's history.
        op.get_bind().execute(
            sa.text("DELETE FROM conversation_turns WHERE outcome = 'OUT_OF_SCOPE'")
        )
        _outcome_check(PRIOR)
        return
    refused = (
        op.get_bind()
        .execute(
            sa.text("SELECT count(*) FROM conversation_turns WHERE outcome = 'OUT_OF_SCOPE'")
        )
        .scalar_one()
    )
    if refused:
        raise RuntimeError(
            f"{refused} turn(s) were refused as OUT_OF_SCOPE. Downgrading would require "
            "relabelling them as a statement about the evidence, which they are not. Remove or "
            "archive those turns deliberately before downgrading, or set "
            "MEDRAG_ALLOW_CONVERSATION_LOSS=1 to acknowledge the loss."
        )
    _outcome_check(PRIOR)
