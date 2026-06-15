"""
Startup configuration validation.

Runs once at boot and surfaces missing/placeholder settings as clear log lines
BEFORE a call is ever placed — so you find out "DEEPGRAM_API_KEY is missing"
at startup, not when the agent goes silent mid-call.

It inspects the agents that are actually configured (file-based or, later, DB)
and only warns about keys those agents need. Returns a list of problems; the
caller decides whether to warn or refuse to start.
"""
import os
import logging
from typing import List

logger = logging.getLogger(__name__)

_PLACEHOLDERS = {"", "change-me", "sk-...", "replace-with-strong-random-string", "..."}


def _missing(name: str) -> bool:
    return os.getenv(name, "").strip() in _PLACEHOLDERS


def validate_startup_config(agent_dicts: List[dict]) -> List[str]:
    """Return a list of human-readable configuration problems (empty = all good)."""
    problems: List[str] = []

    # --- SIP (always required) ---
    for key in ("SIP_DOMAIN", "SIP_USERNAME", "SIP_PASSWORD"):
        if _missing(key):
            problems.append(f"{key} is not set — SIP registration will fail.")

    # --- AI providers: only check what the configured agents actually use ---
    stt_used = {a.get("stt_provider", "whisper") for a in agent_dicts} or {"whisper"}
    tts_used = {a.get("tts_provider", "openai") for a in agent_dicts} or {"openai"}

    # LLM is always OpenAI in this app.
    if _missing("OPENAI_API_KEY"):
        problems.append("OPENAI_API_KEY is not set — the LLM (and Whisper/OpenAI TTS) won't work.")

    if "deepgram" in stt_used and _missing("DEEPGRAM_API_KEY"):
        problems.append("An agent uses Deepgram STT but DEEPGRAM_API_KEY is not set.")
    if "elevenlabs" in tts_used and _missing("ELEVENLABS_API_KEY"):
        problems.append("An agent uses ElevenLabs TTS but ELEVENLABS_API_KEY is not set.")

    return problems


def log_startup_config(agent_dicts: List[dict]) -> bool:
    """Validate and log. Returns True if config is usable, False if SIP/LLM broken."""
    problems = validate_startup_config(agent_dicts)
    if not problems:
        logger.info("Startup config check: OK")
        return True

    for p in problems:
        logger.warning(f"CONFIG: {p}")

    # Hard blockers: without SIP creds or the OpenAI key, no call can work.
    fatal = any(
        ("SIP_" in p or "OPENAI_API_KEY" in p) for p in problems
    )
    if fatal:
        logger.error("CONFIG: required SIP/LLM settings are missing — calls will not work.")
    return not fatal
