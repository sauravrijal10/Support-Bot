from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import TicketStatus


class TicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)


class TicketStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TicketStatus


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: int
    subject: str
    description: str
    status: TicketStatus
    created_at: datetime
    updated_at: datetime
