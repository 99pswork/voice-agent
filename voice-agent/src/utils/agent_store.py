"""
File-based agent store — used when MongoDB is not configured.

Agents are plain JSON files under AGENTS_DIR (default: config/agents/). Each
file is one agent; its `id` is the filename stem (e.g. config/agents/demo.json
-> id "demo"). This lets you place a call with zero database:

    config/agents/demo.json
    {
      "name": "Demo Agent",
      "base_instructions": "You are a friendly assistant on a test call...",
      "initial_message": "Hi! This is an AI test call. Can you hear me?"
    }

The dicts returned here have the same shape the DB documents would, so the
call path (which builds a VoiceAgentConfig from the dict) is identical whether
the agent came from Mongo or a file.
"""
import os
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Defaults so a minimal agent file (just name + base_instructions) still
# satisfies VoiceAgentConfig and the AgentResponse model.
_DEFAULTS = {
    "voice": "alloy",
    "language": "en-US",
    "knowledge_base_ids": [],
    "llm_model": "gpt-4o-mini",
    "stt_provider": "whisper",
    "tts_provider": "openai",
    "max_call_duration": 600,
    "interruption_enabled": True,
    "initial_message": None,
    "end_call_phrases": ["goodbye", "bye", "hang up", "end call"],
    "transfer_number": None,
    "webhook_url": None,
}


def _agents_dir() -> Path:
    return Path(os.getenv("AGENTS_DIR", "config/agents")).resolve()


def _load_file(path: Path) -> Dict:
    with open(path) as f:
        data = json.load(f)
    # id defaults to the filename stem if not set inside the file
    data.setdefault("id", path.stem)
    for k, v in _DEFAULTS.items():
        data.setdefault(k, v)
    # Timestamps from the file's mtime so AgentResponse validates.
    ts = datetime.utcfromtimestamp(path.stat().st_mtime)
    data.setdefault("created_at", ts)
    data.setdefault("updated_at", ts)
    return data


def get_agent(agent_id: str) -> Optional[Dict]:
    """Load one agent by id (filename stem). Returns None if not found."""
    path = _agents_dir() / f"{agent_id}.json"
    if not path.is_file():
        return None
    try:
        return _load_file(path)
    except Exception as e:
        logger.error(f"Failed to load agent file {path}: {e}")
        return None


def list_agents() -> List[Dict]:
    d = _agents_dir()
    if not d.is_dir():
        return []
    out = []
    for path in sorted(d.glob("*.json")):
        try:
            out.append(_load_file(path))
        except Exception as e:
            logger.warning(f"Skipping bad agent file {path}: {e}")
    return out
