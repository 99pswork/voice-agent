"""
MongoDB connection helper.

MongoDB is OPTIONAL. It persists agents, call records/transcripts, knowledge
bases and campaigns. If MONGO_URL is unset/blank or the server is unreachable,
the app still runs: the agent config is loaded from files (see
utils.agent_store) and transcript/record persistence is skipped. Turning Mongo
on later (set a reachable MONGO_URL) restores persistence with no code changes.
"""
import os
import logging
from motor.motor_asyncio import AsyncIOMotorClient
from typing import Optional

logger = logging.getLogger(__name__)

_client: Optional[AsyncIOMotorClient] = None
_db = None


async def init_db():
    """Connect to Mongo if configured & reachable; otherwise run without it."""
    global _client, _db

    mongo_url = os.getenv("MONGO_URL", "").strip()
    if not mongo_url:
        logger.warning("MONGO_URL not set — running WITHOUT a database "
                       "(agents load from files, transcripts not persisted).")
        _db = None
        return

    db_name = os.getenv("MONGO_DB", "voice_agent")
    try:
        # 10s: Atlas (mongodb+srv) needs time for DNS SRV resolution + TLS on a
        # cold start; 3s was too tight and caused a false "unreachable" fallback.
        timeout_ms = int(os.getenv("MONGO_TIMEOUT_MS", "10000"))
        _client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=timeout_ms)
        # Force an actual connection check so we fail fast, not on first query.
        await _client.admin.command("ping")
        _db = _client[db_name]

        await _db.agents.create_index("id", unique=True)
        await _db.knowledge_bases.create_index("id", unique=True)
        await _db.documents.create_index("id", unique=True)
        await _db.documents.create_index("kb_id")
        await _db.calls.create_index("id", unique=True)
        await _db.calls.create_index("agent_id")
        await _db.calls.create_index("started_at")
        logger.info(f"Connected to MongoDB at {mongo_url} (db={db_name})")
    except Exception as e:
        logger.warning(f"MongoDB unreachable ({e}) — running WITHOUT a database. "
                       "Agents load from files; transcripts not persisted.")
        _client = None
        _db = None


def db_available() -> bool:
    """True when a live Mongo connection is in use."""
    return _db is not None


async def get_db():
    return _db


def get_db_instance():
    return _db
