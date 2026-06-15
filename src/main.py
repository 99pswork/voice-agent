"""
Voice Calling Agent - Main Application
Registers as a SIP extension (PJSIP/pjsua2) and places outbound AI calls.
"""
import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from api.routes import agents, calls, knowledge_base, campaigns, webhooks
from sip.sip_backend import SIPBackend
from kb.vector_store import VectorStoreManager
from utils.db import init_db
from utils.logger import setup_logging

load_dotenv()
setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and tear down resources."""
    logger.info("Starting Voice Calling Agent service...")

    # Initialize database
    await init_db()

    # Validate configuration against the agents that are actually defined,
    # so missing keys surface at startup (not mid-call).
    from utils.config_check import log_startup_config
    from utils.db import db_available, get_db_instance
    if db_available():
        agent_dicts = [a async for a in get_db_instance().agents.find({})]
    else:
        from utils.agent_store import list_agents
        agent_dicts = list_agents()
    log_startup_config(agent_dicts)

    # Initialize vector store for knowledge base
    app.state.vector_store = VectorStoreManager()
    await app.state.vector_store.initialize()

    # Connect to the telephony backend (direct SIP client via PJSIP/pjsua2:
    # registers as a SIP extension and places calls directly, no Asterisk).
    logger.info("Connecting SIP telephony backend (pjsua2)...")
    app.state.telephony = SIPBackend()
    await app.state.telephony.connect()

    logger.info("Voice Calling Agent service started successfully")
    yield

    logger.info("Shutting down Voice Calling Agent service...")
    await app.state.telephony.disconnect()


app = FastAPI(
    title="Voice Calling Agent API",
    description="AI-powered voice agent for outbound calls via SIP (PJSIP)",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(agents.router, prefix="/api/v1/agents", tags=["agents"])
app.include_router(knowledge_base.router, prefix="/api/v1/knowledge-base", tags=["knowledge-base"])
app.include_router(calls.router, prefix="/api/v1/calls", tags=["calls"])
app.include_router(campaigns.router, prefix="/api/v1/campaigns", tags=["campaigns"])
app.include_router(webhooks.router, prefix="/api/v1/webhooks", tags=["webhooks"])


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "voice-calling-agent"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", 8000)),
        reload=os.getenv("ENV") == "development",
    )
