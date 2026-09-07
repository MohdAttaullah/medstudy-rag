"""M9 Ask policy.

Small on purpose. M9 adds no new decision about whether a question can be answered — M7 and M8
already made that, and duplicating any part of it here would create a second place where the answer
rule lives and a second place for it to drift.
"""

from typing import Literal

from pydantic import Field

from app.core.reranking_config import Policy


class AskConfig(Policy):
    version: Literal["ask-m9-v1"] = "ask-m9-v1"
    # Pinned by type. An answer reaches a reader only behind an M8 PASS; there is no configuration
    # value, feature flag or environment that relaxes this, because a boolean that could would be
    # the single point at which the whole safety chain becomes optional.
    requires_verified_pass: Literal[True] = True
    # Streaming, when it exists, may carry stage names only. Streaming draft tokens would put
    # unverified medical text in front of a reader, which no amount of later correction undoes.
    stream_answer_tokens: Literal[False] = False
    stream_progress_stages: bool = True
    persist_conversations: bool = True
    # A failed draft is never stored. Only a verified answer is written where an answer is read.
    persist_unverified_drafts: Literal[False] = False
    max_question_chars: int = Field(default=2000, ge=1, le=8000)
    conversation_title_chars: int = Field(default=120, ge=16, le=200)
    max_turns_per_conversation: int = Field(default=200, ge=1, le=1000)
