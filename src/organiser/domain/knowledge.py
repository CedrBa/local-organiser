"""Validated intents for the knowledge store; a model never chooses a filesystem path itself."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


KnowledgeAction = Literal[
    "update_overview", "update_progress", "append_note", "create_topic", "update_topic"
]

TopicStatus = Literal["not_started", "in_progress", "done", "blocked"]


class KnowledgeUpdate(BaseModel):
    """A single, already validated knowledge change.

    This is the hand-over point between a (later) proposal layer and the writer.
    A model may only fill in this structure; the store decides the real paths.
    """

    model_config = ConfigDict(extra="forbid")

    action: KnowledgeAction
    context_path: str = Field(min_length=1, max_length=512)
    text: str | None = Field(default=None, max_length=20_000)
    heading: str | None = Field(default=None, max_length=200)
    topic: str | None = Field(default=None, max_length=200)
    status: TopicStatus | None = None
    percent: int | None = Field(default=None, ge=0, le=100)
    recorded_at: datetime | None = None

    @model_validator(mode="after")
    def validate_fields(self) -> "KnowledgeUpdate":
        if self.action in {"create_topic", "update_topic"} and not self.topic:
            raise ValueError(f"{self.action} benoetigt topic")
        if (
            self.action == "update_progress"
            and self.text is None
            and self.status is None
            and self.percent is None
        ):
            raise ValueError("update_progress benoetigt mindestens text, status oder percent")
        if (
            self.action == "update_topic"
            and self.text is None
            and self.status is None
            and self.percent is None
        ):
            raise ValueError("update_topic benoetigt mindestens text, status oder percent")
        if self.action in {"update_overview", "append_note"} and not (self.text or "").strip():
            raise ValueError(f"{self.action} benoetigt text")
        if self.percent is not None and self.action not in {"update_progress", "update_topic"}:
            raise ValueError("percent ist nur fuer update_progress und update_topic erlaubt")
        return self


class ContextKnowledgeUpdate(BaseModel):
    """A knowledge change addressed by context ID instead of by path.

    The caller states *what* should change to a context's markdown; the service
    resolves the context, reads its bound ``knowledge_path`` from the database and
    builds the internal :class:`KnowledgeUpdate` itself. A ``context_path``, an
    absolute path or a free filename is therefore not expressible at all, and
    ``extra="forbid"`` rejects unknown fields instead of ignoring them.

    The action semantics and their consistency rules are identical to
    :class:`KnowledgeUpdate`; only the addressing differs.
    """

    model_config = ConfigDict(extra="forbid")

    context_id: str = Field(min_length=1, max_length=64)
    action: KnowledgeAction
    text: str | None = Field(default=None, max_length=20_000)
    heading: str | None = Field(default=None, max_length=200)
    topic: str | None = Field(default=None, max_length=200)
    status: TopicStatus | None = None
    percent: int | None = Field(default=None, ge=0, le=100)
    recorded_at: datetime | None = None

    @model_validator(mode="after")
    def validate_fields(self) -> "ContextKnowledgeUpdate":
        if self.action in {"create_topic", "update_topic"} and not self.topic:
            raise ValueError(f"{self.action} benoetigt topic")
        if (
            self.action == "update_progress"
            and self.text is None
            and self.status is None
            and self.percent is None
        ):
            raise ValueError("update_progress benoetigt mindestens text, status oder percent")
        if (
            self.action == "update_topic"
            and self.text is None
            and self.status is None
            and self.percent is None
        ):
            raise ValueError("update_topic benoetigt mindestens text, status oder percent")
        if self.action in {"update_overview", "append_note"} and not (self.text or "").strip():
            raise ValueError(f"{self.action} benoetigt text")
        if self.percent is not None and self.action not in {"update_progress", "update_topic"}:
            raise ValueError("percent ist nur fuer update_progress und update_topic erlaubt")
        return self

    def to_knowledge_update(self, context_path: str) -> KnowledgeUpdate:
        """Build the internal store update for an already validated context path.

        Only the service calls this: it has resolved the path from the database
        and re-validated it, so the caller never gets to choose it.
        """
        return KnowledgeUpdate(
            action=self.action,
            context_path=context_path,
            text=self.text,
            heading=self.heading,
            topic=self.topic,
            status=self.status,
            percent=self.percent,
            recorded_at=self.recorded_at,
        )
