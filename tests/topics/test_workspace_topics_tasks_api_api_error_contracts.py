"""Api error contracts contracts for topics."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from conftest import auth_headers
from organiser.api.schemas import (
    EventContextInput,
    StrictEventContextInput,
    TopicCreate,
)
from organiser.services.domain import list_events_for_entity
from support.workspace_topics_tasks_api import (
    harness as harness,
    ROUTES as ROUTES,
    topic_file_for as topic_file_for,
)


@pytest.mark.parametrize(
    "payload",
    [
        b"# Topic\n\n## Notizen\n\n\xff\xfe kaputt\n",
        "# Topic\n\n## Notizen\n\nn\n\n## notizen\n\n\u00e4\n",
    ],
)
def test_a_defective_topic_file_answers_422_and_never_503(harness, payload):
    client, ids, _, _, _ = harness
    path = topic_file_for(harness, ids)
    path.write_bytes(payload if isinstance(payload, bytes) else payload.encode("utf-8"))
    before = path.read_bytes()

    update = client.post(
        f"/topics/{ids['topic']}/knowledge", json={"text": "Lernnotiz"}, headers=auth_headers()
    )

    assert update.status_code == 422
    assert update.json()["detail"] == {"code": "topic_knowledge_content_invalid"}
    assert "needs_review" not in update.json()["detail"]
    assert path.read_bytes() == before


def test_the_content_code_is_stable_for_e_and_d(harness):
    """One code, one meaning: a human has to edit the file, no retry will help."""
    client, ids, _, _, _ = harness
    path = topic_file_for(harness, ids)
    path.write_bytes(b"# Topic\n\n## Notizen\n\n\xff\xfe kaputt\n")
    response = client.post(
        f"/topics/{ids['topic']}/knowledge", json={"percent": 50}, headers=auth_headers()
    )
    assert (
        response.status_code == 422
        and response.json()["detail"]["code"] == "topic_knowledge_content_invalid"
    )


# -- secondpass: the ensure body must reject a silently ignored spelling (F6) ----


@pytest.mark.parametrize("operation", ["context", "topic"])
@pytest.mark.parametrize(
    "body",
    [
        {"event_context": {"source_id": "proposal-42"}},  # the other C bodies nest it
        {"source_id": "p", "unknown": "x"},
        {"event_context": {"source_id": "p"}, "source_id": "q"},
    ],
)
def test_ensure_body_rejects_anything_but_the_documented_flat_spelling(harness, operation, body):
    """A dropped provenance is worse than a rejected request.

    ``EventContextInput`` ignores unknown keys, so the nested spelling used by the
    other C bodies used to answer ``200`` while auditing ``source_id IS NULL``: the
    caller believed the operation was attributed to their proposal when it was not.
    """
    client, ids, _, store, _ = harness
    path = (
        f"/contexts/{ids['context']}/knowledge/ensure"
        if operation == "context"
        else f"/topics/{ids['topic']}/knowledge/ensure"
    )

    response = client.post(path, json=body, headers=auth_headers())

    assert response.status_code == 422
    # A refused schema must not have created anything.
    assert not store.root.exists()


@pytest.mark.parametrize("operation", ["context", "topic"])
def test_ensure_body_still_accepts_the_flat_spelling_and_omission(harness, operation):
    """Direct-body compatibility is preserved in both directions."""
    client, ids, factory, store, _ = harness
    path = (
        f"/contexts/{ids['context']}/knowledge/ensure"
        if operation == "context"
        else f"/topics/{ids['topic']}/knowledge/ensure"
    )
    entity_type, entity_id = (
        ("context", ids["context"]) if operation == "context" else ("topic", ids["topic"])
    )

    assert (
        client.post(path, json={"source_id": "flat-1"}, headers=auth_headers()).status_code == 200
    )
    with factory() as session:
        first = [
            e
            for e in list_events_for_entity(session, entity_type=entity_type, entity_id=entity_id)
            if "KNOWLEDGE" in e.event_type
        ]
    assert first and all(event.source_id == "flat-1" for event in first)

    assert client.post(path, headers=auth_headers()).status_code == 200
    with factory() as session:
        later = [
            e
            for e in list_events_for_entity(session, entity_type=entity_type, entity_id=entity_id)
            if "KNOWLEDGE" in e.event_type
        ]
    assert len(later) > len(first)
    assert all(event.source_id is None for event in later[len(first) :])


@pytest.mark.parametrize("operation", ["context", "topic"])
def test_ensure_accepts_the_three_measured_ways_to_send_no_provenance(harness, operation):
    """``Body(default=None)`` is optional in the strong sense, and only there.

    Measured, because the docs now state the boundary: omitting the body entirely,
    an empty body with a JSON content type, an explicit JSON ``null`` and ``{}``
    all mean "no provenance" and answer ``200``. What is refused is anything that
    is not a JSON *object*: a JSON scalar or array, malformed JSON, and body bytes
    without a JSON content type.
    """
    client, ids, factory, store, _ = harness
    path = (
        f"/contexts/{ids['context']}/knowledge/ensure"
        if operation == "context"
        else f"/topics/{ids['topic']}/knowledge/ensure"
    )
    entity_type, entity_id = (
        ("context", ids["context"]) if operation == "context" else ("topic", ids["topic"])
    )
    json_headers = {**auth_headers(), "Content-Type": "application/json"}

    for body in (None, b"", b"null", b"{}"):
        headers = json_headers if body is not None else auth_headers()
        assert client.post(path, content=body, headers=headers).status_code == 200

    with factory() as session:
        events = [
            e
            for e in list_events_for_entity(session, entity_type=entity_type, entity_id=entity_id)
            if "KNOWLEDGE" in e.event_type
        ]
    assert events and all(event.source_id is None for event in events)

    # Not an object -> refused, not silently dropped.
    assert client.post(path, content=b'"null"', headers=json_headers).status_code == 422
    assert client.post(path, json=[1, 2], headers=auth_headers()).status_code == 422
    assert client.post(path, content=b"{not json", headers=json_headers).status_code == 422
    # Body bytes without a JSON content type are NOT the same as "no body".
    assert (
        client.post(path, content=b'{"source_id": "a"}', headers=auth_headers()).status_code == 422
    )


def test_strict_event_context_is_a_distinct_model_not_a_silent_widening():
    """The strict model must not weaken the permissive one used elsewhere."""
    assert issubclass(StrictEventContextInput, EventContextInput)
    assert StrictEventContextInput.model_config.get("extra") == "forbid"
    assert "extra" not in EventContextInput.model_config
    flat = StrictEventContextInput(source_id="p", source_type="context_action_proposal")
    assert flat.model_dump() == {
        "source_type": "context_action_proposal",
        "source_id": "p",
        "correlation_id": None,
        "causation_event_id": None,
    }
    for body in ({"event_context": {"source_id": "p"}}, {"nope": 1}):
        with pytest.raises(ValidationError):
            StrictEventContextInput(**body)


def test_the_nested_event_context_still_drops_unknown_keys_on_purpose():
    """Known, deliberate limitation -- pinned so it is never mistaken for a fix.

    ``extra="forbid"`` on the outer request model does not reach a nested model, so
    ``TopicCreate``/``TopicUpdate``/``TopicStatusUpdate`` still silently drop an
    unknown key inside ``event_context``. Only the two ensure routes, whose body is
    nothing *but* the event context, were tightened. Widening it later is a
    documented contract change, not a drive-by fix -- this test is where it has to be
    made deliberately.
    """
    request = TopicCreate(title="Neu", event_context={"source_id": "p", "typo": "y"})
    assert request.event_context.model_dump() == {
        "source_type": None,
        "source_id": "p",
        "correlation_id": None,
        "causation_event_id": None,
    }


# -- secondpass: 503 is not a retry permission -------------------------------


def test_503_is_still_ambiguous_state_not_an_open_door_to_retry(harness, monkeypatch):
    """A 503 from the legacy context ensure can follow a *partial* filesystem write.

    So the status must not read as "the same request will succeed later". It is
    documented as requiring deliberate recovery; this test pins that the partial
    write really does happen behind that code.
    """
    client, ids, _, store, _ = harness
    real_write = store._filesystem.atomic_write
    written = []

    def partial(*args, **kwargs):
        written.append(1)
        if len(written) == 2:  # core file on disk, topic/context step fails
            raise PermissionError("private data: " + str(store.root))
        return real_write(*args, **kwargs)

    monkeypatch.setattr(store._filesystem, "atomic_write", partial)
    response = client.post(f"/topics/{ids['topic']}/knowledge/ensure", headers=auth_headers())

    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "knowledge_operation_failed"}
    # Something *was* written before the failure: the outcome is genuinely unknown,
    # which is exactly why no code here may authorise an automatic retry.
    assert written
    assert list(store.root.rglob("*.md"))


# -- the 503 answer itself carries no retry permission --------------------------


def test_the_503_answer_carries_no_retry_permission(harness, monkeypatch):
    """The body, not just the doc, must not invite a retry.

    ``503`` is the code clients read as "later". Since the outcome behind it is
    genuinely unknown, the answer may only carry the code -- no ``needs_review``,
    no ``retry`` flag, no hint that repeating the call is safe.
    """
    client, ids, _, store, _ = harness

    def partial(*args, **kwargs):
        raise PermissionError("private data: " + str(store.root))

    monkeypatch.setattr(store._filesystem, "atomic_write", partial)
    response = client.post(f"/topics/{ids['topic']}/knowledge/ensure", headers=auth_headers())

    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "knowledge_operation_failed"}}
    serialised = response.text.lower()
    assert "retry" not in serialised and "wiederhol" not in serialised
    assert "needs_review" not in response.json()["detail"]


def test_the_ensure_route_also_answers_422_for_a_defective_file(harness):
    """The content classification is not limited to the apply route.

    ``ensure_topic_knowledge`` reads the same file before writing, so a defective
    file must be the same non-transient 422 there -- never the 503 of the apply test
    above, and never a partially rewritten file.
    """
    client, ids, _, store, _ = harness
    assert (
        client.post(f"/topics/{ids['topic']}/knowledge/ensure", headers=auth_headers()).status_code
        == 200
    )
    path = store.root / "contexts" / ids["context"] / "topics" / f"topic-{ids['topic']}.md"
    path.write_bytes(b"# Topic\n\n## Notizen\n\n\xff\xfe kaputt\n")
    before = path.read_bytes()

    response = client.post(f"/topics/{ids['topic']}/knowledge/ensure", headers=auth_headers())

    assert response.status_code == 422
    assert response.json()["detail"] == {"code": "topic_knowledge_content_invalid"}
    assert path.read_bytes() == before


def test_a_422_never_carries_a_retry_permission_either(harness):
    """The non-transient answer must be equally free of retry wording."""
    client, ids, _, store, _ = harness
    assert (
        client.post(f"/topics/{ids['topic']}/knowledge/ensure", headers=auth_headers()).status_code
        == 200
    )
    path = store.root / "contexts" / ids["context"] / "topics" / f"topic-{ids['topic']}.md"
    path.write_text("# Topic\n\n## Notizen\n\nn\n\n## notizen\n\nä\n", encoding="utf-8")
    before = path.read_bytes()

    response = client.post(
        f"/topics/{ids['topic']}/knowledge", json={"text": "Lernnotiz"}, headers=auth_headers()
    )

    assert response.status_code == 422
    serialised = response.text.lower()
    assert "retry" not in serialised and "wiederhol" not in serialised
    assert path.read_bytes() == before


def test_the_strict_body_rejects_a_nested_context_before_any_side_effect(harness):
    """A refused body must not create a knowledge directory either.

    The strict model runs before the handler, so the refusal cannot be a
    "validated but ignored" event that also did the filesystem work.
    """
    client, ids, _, store, _ = harness

    refused = client.post(
        f"/topics/{ids['topic']}/knowledge/ensure",
        json={"event_context": {"source_id": "p"}},
        headers=auth_headers(),
    )
    accepted = client.post(f"/topics/{ids['topic']}/knowledge/ensure", headers=auth_headers())

    assert refused.status_code == 422
    assert accepted.status_code == 200
    assert list(store.root.rglob("*.md"))
