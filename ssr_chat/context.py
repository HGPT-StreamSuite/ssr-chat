from __future__ import annotations

from uuid import uuid4

from .models import ChatMessage, ConversationSnapshot
from .stores import ConversationEventStore


class ConversationContext:
    def __init__(self, store: ConversationEventStore, window_size: int = 8) -> None:
        self.store = store
        self.window_size = window_size

    def snapshot_for(self, message: ChatMessage) -> ConversationSnapshot:
        messages = self.store.prior_messages(
            message.conversation_id,
            excluding_message_id=message.message_id,
            limit=self.window_size,
        )
        return ConversationSnapshot(
            snapshot_id=f"snapshot-{uuid4().hex}",
            conversation_id=message.conversation_id,
            messages=messages,
        )
