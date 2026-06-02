"""
Inbound webhooks - lets you integrate the voice agent with your CRM.
For example, you can hit /api/v1/webhooks/trigger-call from your CRM
when a new lead is created.
"""
from typing import Dict, Any
from fastapi import APIRouter, Request, Depends
from pydantic import BaseModel

from utils.db import get_db

router = APIRouter()


class TriggerCallWebhook(BaseModel):
    agent_id: str
    destination: str
    variables: Dict[str, Any] = {}
    metadata: Dict[str, Any] = {}


@router.post("/trigger-call")
async def trigger_call(payload: TriggerCallWebhook, request: Request, db=Depends(get_db)):
    """Trigger an outbound call from your CRM/ERP."""
    from api.routes.calls import OutboundCallRequest, make_outbound_call
    req = OutboundCallRequest(
        agent_id=payload.agent_id,
        destination=payload.destination,
        variables=payload.variables,
        metadata=payload.metadata,
    )
    return await make_outbound_call(req, request, db)
