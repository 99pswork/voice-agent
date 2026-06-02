"""
SIPCallManager - drives outbound calls over the direct-SIP (pjsua2) client.

Exposes the interface the API/dialer rely on:
    originate_call(call_id, destination, agent_config, caller_id, trunk, variables) -> channel_id
    hangup(channel_id)
    transfer(channel_id, destination)

Here a "channel_id" is just the call_id (pjsua2 has no channels); we keep the
name because the API/DB records use it.
"""
import os
import asyncio
import logging
from typing import Dict, Optional
from datetime import datetime

from agent.voice_agent_config import VoiceAgentConfig
from sip.sip_call_session import SIPCallSession

logger = logging.getLogger(__name__)


class SIPCallManager:
    def __init__(self, sip_client):
        self.sip = sip_client
        self.sessions: Dict[str, SIPCallSession] = {}  # call_id -> session
        # Captured on the main asyncio loop; pjsua2 callbacks (on native
        # threads) marshal back onto it via run_coroutine_threadsafe.
        self.loop = asyncio.get_running_loop()

    async def originate_call(
        self,
        call_id: str,
        destination: str,
        agent_config: Dict,
        caller_id: Optional[str] = None,
        trunk: Optional[str] = None,  # unused in direct-SIP; PBX does routing
        variables: Optional[Dict] = None,
    ) -> str:
        if not self.sip.is_registered:
            # Give registration a brief chance (e.g. right after startup).
            if not self.sip.wait_until_registered(timeout=10):
                raise RuntimeError(
                    "SIP account is not registered; cannot place call"
                )

        config = VoiceAgentConfig(**{
            k: v for k, v in agent_config.items()
            if k in VoiceAgentConfig.__dataclass_fields__
        })

        session = SIPCallSession(
            call_id=call_id,
            sip_client=self.sip,
            agent_config=config,
            variables=variables or {},
            loop=self.loop,
        )
        self.sessions[call_id] = session

        # PJSIP calls must be created on a PJSIP-registered thread.
        self.sip.register_thread()
        call = self.sip.make_call(
            destination=destination,
            on_connected=session.on_connected,
            on_disconnected=lambda code, cid=call_id: self._on_call_ended(cid, code),
            on_inbound_pcm=session.on_inbound_pcm,
        )
        session.call = call

        logger.info(f"Originated SIP call {call_id} -> {destination}")
        return call_id  # used as channel_id by the API layer

    def _on_call_ended(self, call_id: str, status_code: int):
        session = self.sessions.get(call_id)
        if session:
            session.on_disconnected(status_code)
        # Persist outcome on the asyncio loop.
        self.loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._finalize(call_id))
        )

    async def _finalize(self, call_id: str):
        session = self.sessions.pop(call_id, None)
        if not session:
            return
        await session.stop()

        ended = datetime.utcnow()
        transcript = session.engine.get_transcript()

        # Persist the completed call + transcript when a DB is configured.
        from utils.db import get_db_instance
        db = get_db_instance()
        duration = None
        if db is not None:
            call = await db.calls.find_one({"id": call_id})
            if call and call.get("started_at"):
                duration = (ended - call["started_at"]).seconds
                await db.calls.update_one(
                    {"id": call_id},
                    {"$set": {
                        "status": "completed",
                        "ended_at": ended,
                        "duration_seconds": duration,
                        "transcript": transcript,
                        "outcome": session.outcome,
                    }},
                )
        else:
            duration = (ended - session.started_at).seconds
            logger.info(
                f"Call {call_id} ended (no DB): {len(transcript)} turns, "
                f"{duration}s, outcome={session.outcome}"
            )

        # Webhook fires regardless of DB — a lightweight way to capture results.
        if session.config.webhook_url:
            from utils.webhook import fire_webhook
            await fire_webhook(
                session.config.webhook_url,
                {
                    "event": "call.completed",
                    "call_id": call_id,
                    "duration_seconds": duration,
                    "transcript": transcript,
                    "outcome": session.outcome,
                },
            )

    async def hangup(self, channel_id: str):
        session = self.sessions.get(channel_id)
        if session and session.call:
            self.sip.register_thread()
            self.sip.hangup(session.call)

    async def transfer(self, channel_id: str, destination: str):
        session = self.sessions.get(channel_id)
        if session:
            await session._transfer(destination)
