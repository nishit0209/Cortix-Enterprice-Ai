"""Isolation-matrix factories for the Conversational BI routes (see backend/testing/isolation.py)."""

from __future__ import annotations


def _dataset_id(world) -> str:
    from apps.governance.tests.isolation import dataset_id_for

    return dataset_id_for(world)


def _conversation(world):
    from apps.bi.models import Conversation

    conv = world.resources.get("bi_conversation")
    if conv is None:
        ds = _dataset_id(world)
        with world.tenant():
            conv = Conversation.objects.create(user_id=world.owner.pk, dataset_id=ds, title="Isolation probe")
        world.resources["bi_conversation"] = conv
    return conv


def _conversation_kwargs(world) -> dict[str, str]:
    return {"pk": str(_conversation(world).id)}


def _message(world) -> dict[str, str]:
    from apps.bi.models import Message

    msg = world.resources.get("bi_message")
    if msg is None:
        conv = _conversation(world)
        with world.tenant():
            msg = Message.objects.create(conversation=conv, role="assistant", status="ok", question="probe",
                                         content="probe", sql_raw="SELECT 1", sql_source="builder")
        world.resources["bi_message"] = msg
    return {"pk": str(msg.id)}


def _saved_query(world) -> dict[str, str]:
    from apps.bi.models import SavedQuery

    sq = world.resources.get("bi_saved_query")
    if sq is None:
        ds = _dataset_id(world)
        with world.tenant():
            sq = SavedQuery.objects.create(owner_id=world.owner.pk, dataset_id=ds, name="Isolation probe",
                                           sql="SELECT 1", is_shared=True)
        world.resources["bi_saved_query"] = sq
    return {"pk": str(sq.id)}


ISOLATION_FACTORIES = {
    "bi-conversation-detail": _conversation_kwargs,
    "bi-conversation-messages": _conversation_kwargs,
    "bi-message-feedback": _message,
    "bi-saved-query-detail": _saved_query,
    "bi-saved-query-run": _saved_query,
}
