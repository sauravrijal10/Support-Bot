from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    question: str
    answer: str
    sources: list[str]
    tools_used: list[str]
    created_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[ConversationMessageOut]
