"""
Agent CRUD endpoints - Create voice agents with custom instructions and knowledge base
"""
from typing import Optional, List
from uuid import uuid4
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from utils.db import get_db
from agent.voice_agent_config import VoiceAgentConfig

router = APIRouter()


class AgentCreate(BaseModel):
    name: str = Field(..., description="Agent display name")
    base_instructions: str = Field(
        ...,
        description="System prompt / persona / behavior rules for the agent",
        min_length=10,
    )
    voice: str = Field("alloy", description="TTS voice: alloy, echo, fable, onyx, nova, shimmer, or custom")
    language: str = Field("en-US", description="BCP-47 language code")
    knowledge_base_ids: List[str] = Field(default_factory=list, description="Linked KB collection IDs")
    llm_model: str = Field("gpt-4o-mini", description="LLM backend model")
    stt_provider: str = Field("whisper", description="STT: whisper, deepgram, google")
    tts_provider: str = Field("openai", description="TTS: openai, elevenlabs, azure")
    max_call_duration: int = Field(600, description="Max call length in seconds")
    interruption_enabled: bool = Field(True, description="Allow user to interrupt agent")
    initial_message: Optional[str] = Field(
        None, description="First sentence agent says when call connects"
    )
    end_call_phrases: List[str] = Field(
        default_factory=lambda: ["goodbye", "bye", "hang up", "end call"],
        description="User phrases that trigger call end",
    )
    transfer_number: Optional[str] = Field(
        None, description="SIP URI / phone for human handoff"
    )
    webhook_url: Optional[str] = Field(
        None, description="Post-call webhook for transcript & outcome"
    )


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    base_instructions: Optional[str] = None
    voice: Optional[str] = None
    language: Optional[str] = None
    knowledge_base_ids: Optional[List[str]] = None
    initial_message: Optional[str] = None
    transfer_number: Optional[str] = None
    webhook_url: Optional[str] = None


class AgentResponse(BaseModel):
    id: str
    name: str
    base_instructions: str
    voice: str
    language: str
    knowledge_base_ids: List[str]
    llm_model: str
    stt_provider: str
    tts_provider: str
    max_call_duration: int
    interruption_enabled: bool
    initial_message: Optional[str]
    end_call_phrases: List[str]
    transfer_number: Optional[str]
    webhook_url: Optional[str]
    created_at: datetime
    updated_at: datetime


@router.post("", response_model=AgentResponse, status_code=201)
async def create_agent(payload: AgentCreate, db=Depends(get_db)):
    """
    Create a new voice agent.

    Example:
    ```json
    {
      "name": "Sales Bot - Premium Plan",
      "base_instructions": "You are Riya, a polite sales agent for ABC Corp...",
      "voice": "nova",
      "language": "en-IN",
      "knowledge_base_ids": ["kb_pricing_v2"],
      "initial_message": "Hi, am I speaking with {customer_name}?"
    }
    ```
    """
    # Without a DB, agents are managed as files in config/agents/ (see agent_store).
    if db is None:
        raise HTTPException(
            503,
            "No database configured. Create agents as JSON files in "
            "config/agents/<id>.json (or set MONGO_URL to use the API).",
        )

    agent_id = f"agent_{uuid4().hex[:12]}"
    now = datetime.utcnow()

    record = {
        "id": agent_id,
        **payload.model_dump(),
        "created_at": now,
        "updated_at": now,
    }
    await db.agents.insert_one(record)
    return AgentResponse(**record)


@router.get("", response_model=List[AgentResponse])
async def list_agents(db=Depends(get_db), limit: int = 50, offset: int = 0):
    if db is None:
        from utils.agent_store import list_agents as file_list
        return [AgentResponse(**a) for a in file_list()[offset:offset + limit]]
    cursor = db.agents.find({}).skip(offset).limit(limit)
    return [AgentResponse(**doc) async for doc in cursor]


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: str, db=Depends(get_db)):
    if db is None:
        from utils.agent_store import get_agent as file_get
        doc = file_get(agent_id)
    else:
        doc = await db.agents.find_one({"id": agent_id})
    if not doc:
        raise HTTPException(404, f"Agent {agent_id} not found")
    return AgentResponse(**doc)


@router.patch("/{agent_id}", response_model=AgentResponse)
async def update_agent(agent_id: str, payload: AgentUpdate, db=Depends(get_db)):
    if db is None:
        raise HTTPException(503, "No database configured. Edit config/agents/<id>.json directly.")
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    updates["updated_at"] = datetime.utcnow()

    result = await db.agents.find_one_and_update(
        {"id": agent_id}, {"$set": updates}, return_document=True
    )
    if not result:
        raise HTTPException(404, f"Agent {agent_id} not found")
    return AgentResponse(**result)


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, db=Depends(get_db)):
    if db is None:
        raise HTTPException(503, "No database configured. Delete config/agents/<id>.json directly.")
    result = await db.agents.delete_one({"id": agent_id})
    if result.deleted_count == 0:
        raise HTTPException(404, f"Agent {agent_id} not found")


@router.post("/{agent_id}/test")
async def test_agent(agent_id: str, message: str, db=Depends(get_db)):
    """Send a text message to the agent and get its reply (no actual call)."""
    if db is None:
        from utils.agent_store import get_agent as file_get
        doc = file_get(agent_id)
    else:
        doc = await db.agents.find_one({"id": agent_id})
    if not doc:
        raise HTTPException(404, "Agent not found")

    config = VoiceAgentConfig(**{
        k: v for k, v in doc.items() if k in VoiceAgentConfig.__dataclass_fields__
    })
    from agent.conversation_engine import ConversationEngine
    engine = ConversationEngine(config)
    reply = await engine.generate_response(message, history=[])
    return {"agent_id": agent_id, "user": message, "reply": reply}
