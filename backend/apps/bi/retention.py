"""BI retention targets (registered with apps.governance.retention in BiConfig.ready()).

    bi.conversations  delete: conversations (and their messages) whose last activity is older than the cutoff;
                      archive: keep the conversation, questions, answers, SQL and charts but drop the stored
                      RESULT ROWS of its messages (`result.rows = []`, `result.archived = true`).

The BI query audit (`BiQueryAudit`) is append-only evidence and is never a retention target.
"""

from __future__ import annotations

from datetime import datetime

from django.db.models.functions import Coalesce

from .models import Conversation, Message


def old_conversations(cutoff: datetime):
    return Conversation.objects.annotate(active_at=Coalesce("last_message_at", "created_at")).filter(
        active_at__lt=cutoff)


def archivable_messages(cutoff: datetime):
    # Only messages that still hold at least one result row. (An `exclude(rows=[] | archived=True)` matched NOTHING
    # when the `archived` key is absent: NULL comparisons make the NOT(...) NULL in SQL.)
    return (Message.objects.filter(conversation__in=old_conversations(cutoff).values("id"))
            .filter(result__isnull=False, result__rows__0__isnull=False))


def count(cutoff: datetime, action: str) -> int:
    if action == "archive":
        return archivable_messages(cutoff).count()
    return old_conversations(cutoff).count()


def apply(cutoff: datetime, action: str) -> int:
    if action == "archive":
        n = 0
        for msg in archivable_messages(cutoff).only("id", "result").iterator(chunk_size=500):
            result = dict(msg.result or {})
            result["rows"] = []
            result["archived"] = True
            Message.objects.filter(id=msg.id).update(result=result)
            n += 1
        return n
    ids = list(old_conversations(cutoff).values_list("id", flat=True))
    Conversation.objects.filter(id__in=ids).delete()  # messages cascade
    return len(ids)


def register() -> None:
    from apps.governance.retention import RetentionTarget, register_target

    register_target(RetentionTarget(
        key="bi.conversations", label="Conversational BI history",
        description="Conversations whose last message is older than the cutoff. delete removes them with their "
                    "messages; archive keeps the questions and answers but drops the stored result rows.",
        actions=("delete", "archive"), count=count, apply=apply,
    ))
