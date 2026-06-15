"""
Call routes - Originate outbound calls via the direct SIP (PJSIP) backend.
"""
from typing import Optional, List, Dict, Any
from uuid import uuid4
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field

from utils.db import get_db
from sip.sip_call_manager import SIPCallManager

import logging
logger = logging.getLogger(__name__)

router = APIRouter()


class OutboundCallRequest(BaseModel):
    agent_id: str = Field(..., description="ID of voice agent to use")
    destination: str = Field(
        ...,
        description="Phone (E.164: +919xxxxxxxxx) or full SIP URI (sip:user@host)",
    )
    caller_id: Optional[str] = Field(None, description="Caller ID to present")
    trunk: Optional[str] = Field(
        None,
        description="Ignored for the direct-SIP backend; the PBX handles routing. Kept for API compatibility.",
    )
    variables: Dict[str, Any] = Field(
        default_factory=dict,
        description="Template variables for initial_message and instructions (e.g. {customer_name: 'Rahul'})",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Free-form data attached to the call record"
    )
    max_retries: int = Field(0, description="Auto-retry on no-answer/busy")


class CallResponse(BaseModel):
    # Records store the id under "id" in Mongo; accept that on input but always
    # expose it as "call_id" on output (stable contract for the frontend).
    model_config = {"populate_by_name": True}
    call_id: str = Field(..., validation_alias="id", serialization_alias="call_id")
    channel_id: Optional[str] = None
    agent_id: str
    agent_name: Optional[str] = None
    destination: str                       # the phone number
    caller_id: Optional[str] = None
    direction: Optional[str] = "outbound"
    status: str  # initiated | ringing | answered | completed | failed
    started_at: datetime
    ended_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    turn_count: Optional[int] = None
    transcript: Optional[List[Dict[str, Any]]] = None  # [{role, content, at}]
    outcome: Optional[str] = None
    variables: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None


@router.post("/outbound", response_model=CallResponse, status_code=201)
async def make_outbound_call(payload: OutboundCallRequest, request: Request, db=Depends(get_db)):
    """
    Originate an outbound call. The agent (registered as a SIP extension) dials
    the destination through the PBX and streams audio to the AI pipeline
    (STT -> LLM -> TTS).

    Example:
    ```bash
    curl -X POST /api/v1/calls/outbound -d '{
      "agent_id": "agent_abc123",
      "destination": "+919812345678",
      "variables": {"customer_name": "Rahul", "order_id": "ORD-998"}
    }'
    ```
    """
    # Agent comes from Mongo when available, else from a config file.
    if db is not None:
        agent = await db.agents.find_one({"id": payload.agent_id})
    else:
        from utils.agent_store import get_agent
        agent = get_agent(payload.agent_id)
    if not agent:
        raise HTTPException(404, f"Agent {payload.agent_id} not found")

    call_id = f"call_{uuid4().hex[:12]}"
    call_manager: SIPCallManager = request.app.state.telephony.call_manager

    record = {
        "id": call_id,
        "channel_id": None,
        "agent_id": payload.agent_id,
        "agent_name": agent.get("name"),          # denormalized for UI lists
        "destination": payload.destination,        # the phone number called
        "caller_id": payload.caller_id,
        "trunk": payload.trunk,
        "variables": payload.variables,
        "metadata": payload.metadata,
        "direction": "outbound",
        "status": "initiated",
        "started_at": datetime.utcnow(),
        "ended_at": None,
        "duration_seconds": None,
        "transcript": [],
        "turn_count": 0,
        "outcome": None,
    }
    # Persist FIRST so even a failed dial attempt shows up in call history.
    if db is not None:
        await db.calls.insert_one(record)

    try:
        channel_id = await call_manager.originate_call(
            call_id=call_id,
            destination=payload.destination,
            caller_id=payload.caller_id,
            trunk=payload.trunk,
            agent_config=agent,
            variables=payload.variables,
        )
        record["channel_id"] = channel_id
        if db is not None:
            await db.calls.update_one(
                {"id": call_id}, {"$set": {"channel_id": channel_id}}
            )
    except Exception as e:
        # Mark the attempt as failed (kept in history) and report the reason.
        if db is not None:
            await db.calls.update_one(
                {"id": call_id},
                {"$set": {"status": "failed", "outcome": "originate_failed",
                          "error": str(e), "ended_at": datetime.utcnow()}},
            )
        raise HTTPException(500, f"Failed to originate call: {e}")
    return CallResponse(**record)


@router.post("/bulk", status_code=202)
async def bulk_calls(
    agent_id: str,
    targets: List[Dict[str, Any]],
    request: Request,
    rate_per_second: int = 5,
    db=Depends(get_db),
):
    """
    Trigger many calls at once. `targets` is a list of:
    `[{"destination": "+91...", "variables": {...}}, ...]`
    Throttled by rate_per_second to respect SIP trunk concurrency.
    """
    from sip.dialer import BulkDialer
    dialer = BulkDialer(request.app.state.telephony.call_manager, rate_per_second)
    job_id = await dialer.start(agent_id, targets, db)
    return {"job_id": job_id, "queued": len(targets)}


# Call history endpoints need the database; they 503 cleanly without it.
_NO_DB = "Call history requires a database (set MONGO_URL)."


@router.get("/{call_id}", response_model=CallResponse)
async def get_call(call_id: str, db=Depends(get_db)):
    if db is None:
        raise HTTPException(503, _NO_DB)
    doc = await db.calls.find_one({"id": call_id})
    if not doc:
        raise HTTPException(404, "Call not found")
    return CallResponse(**doc)


@router.get("", response_model=List[CallResponse])
async def list_calls(
    db=Depends(get_db),
    agent_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
):
    if db is None:
        raise HTTPException(503, _NO_DB)
    query = {}
    if agent_id:
        query["agent_id"] = agent_id
    if status:
        query["status"] = status
    cursor = db.calls.find(query).sort("started_at", -1).limit(limit)
    out = []
    async for d in cursor:
        try:
            out.append(CallResponse(**d))
        except Exception:
            # Skip any malformed/legacy record rather than failing the whole list.
            logger.warning(f"Skipping unparseable call record {d.get('id')}")
    return out


@router.post("/{call_id}/hangup")
async def hangup_call(call_id: str, request: Request, db=Depends(get_db)):
    # channel_id == call_id for the SIP backend, so no DB lookup is needed.
    call_manager = request.app.state.telephony.call_manager
    await call_manager.hangup(call_id)
    return {"call_id": call_id, "status": "hangup_requested"}


@router.post("/{call_id}/transfer")
async def transfer_call(call_id: str, destination: str, request: Request, db=Depends(get_db)):
    """Warm/cold transfer to a human agent."""
    call_manager = request.app.state.telephony.call_manager
    await call_manager.transfer(call_id, destination)
    return {"call_id": call_id, "transferred_to": destination}


@router.get("/{call_id}/recording")
async def get_recording_url(call_id: str, db=Depends(get_db)):
    if db is None:
        raise HTTPException(503, _NO_DB)
    doc = await db.calls.find_one({"id": call_id})
    if not doc:
        raise HTTPException(404, "Call not found")
    if not doc.get("recording_path"):
        raise HTTPException(404, "No recording available")
    return {"url": f"/recordings/{doc['recording_path']}"}
