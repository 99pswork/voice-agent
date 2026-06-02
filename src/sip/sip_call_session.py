"""
SIPCallSession - real-time AI conversation loop for ONE direct-SIP (pjsua2) call.

Audio flows through the pjsua2 _AgentCall:
   remote party --(PCM 16k)--> on_inbound_pcm --> STT
   TTS --(PCM 16k)--> call.queue_outbound --> remote party

Conversation logic: STT -> LLM/RAG -> TTS, with barge-in interruption and
end/transfer intent detection, accumulating a transcript on the call record.
"""
import asyncio
import logging
from typing import Optional
from datetime import datetime

from agent.conversation_engine import ConversationEngine
from agent.voice_agent_config import VoiceAgentConfig
from agent.stt_provider import STTProvider
from agent.tts_provider import TTSProvider

logger = logging.getLogger(__name__)


class SIPCallSession:
    def __init__(
        self,
        call_id: str,
        sip_client,
        agent_config: VoiceAgentConfig,
        variables: dict,
        loop: asyncio.AbstractEventLoop,
    ):
        self.call_id = call_id
        self.sip_client = sip_client
        self.config = agent_config
        self.loop = loop

        self.engine = ConversationEngine(agent_config, variables)
        self.stt = STTProvider.create(agent_config.stt_provider, agent_config.language)
        self.tts = TTSProvider.create(agent_config.tts_provider, agent_config.voice)

        self.call = None  # _AgentCall, set by manager after make_call
        self.outcome: Optional[str] = None
        self.started_at = datetime.utcnow()
        self._stopped = False
        self._is_speaking = False
        self._max_duration_task: Optional[asyncio.Task] = None
        self._connected = asyncio.Event()

    # ---- callbacks invoked from the PJSIP media/worker threads ----

    def on_inbound_pcm(self, pcm: bytes):
        """PJSIP media thread -> feed STT on the asyncio loop."""
        # feed_audio is sync and itself schedules onto its own loop; safe to
        # call directly, but guard against post-stop frames.
        if not self._stopped:
            try:
                self.stt.feed_audio(pcm)
            except Exception:
                logger.exception("feed_audio failed")

    def on_connected(self):
        """PJSIP thread: media is active. Kick off the conversation."""
        self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self._start()))

    def on_disconnected(self, status_code: int):
        if self.outcome is None:
            self.outcome = f"sip_disconnected_{status_code}"
        self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self.stop()))

    # ---- conversation lifecycle (asyncio loop) ----

    async def _start(self):
        if self._connected.is_set():
            return
        self._connected.set()

        self.stt.on_partial = self._on_partial_transcript
        self.stt.on_final = self._on_final_transcript
        await self.stt.start()

        opener = self.engine.get_initial_message()
        if opener:
            await self._speak(opener)

        self._max_duration_task = asyncio.create_task(self._duration_watchdog())
        logger.info(f"SIP call {self.call_id} session started")

    async def stop(self):
        if self._stopped:
            return
        self._stopped = True
        if self._max_duration_task:
            self._max_duration_task.cancel()
        await self.stt.stop()
        await self.tts.stop()
        logger.info(f"SIP call {self.call_id} session stopped")

    async def _duration_watchdog(self):
        await asyncio.sleep(self.config.max_call_duration)
        logger.info(f"SIP call {self.call_id} hit max duration")
        self.outcome = "max_duration_reached"
        await self._end_call_gracefully("Thank you for your time. Goodbye.")

    def _on_partial_transcript(self, text: str):
        if self.config.interruption_enabled and self._is_speaking and len(text) > 3:
            asyncio.run_coroutine_threadsafe(self._interrupt_speech(), self.loop)

    async def _interrupt_speech(self):
        if self._is_speaking:
            await self.tts.stop_current()
            if self.call:
                self.call.flush_outbound()
            self._is_speaking = False
            logger.debug(f"SIP call {self.call_id}: interrupted by user")

    def _on_final_transcript(self, text: str):
        if not text.strip():
            return
        asyncio.run_coroutine_threadsafe(self._handle_user_input(text), self.loop)

    async def _handle_user_input(self, user_text: str):
        logger.info(f"SIP call {self.call_id} USER: {user_text}")

        if self.engine.should_end_call(user_text):
            self.outcome = "user_ended"
            await self._end_call_gracefully("Thank you, have a great day. Goodbye.")
            return

        if self.engine.should_transfer(user_text) and self.config.transfer_number:
            await self._speak("Sure, transferring you to a human agent now. Please hold.")
            await asyncio.sleep(0.5)
            # Direct SIP transfer = blind REFER to the transfer target.
            self.outcome = "transferred"
            await self._transfer(self.config.transfer_number)
            return

        await self._speak_streaming(user_text)

    async def _speak(self, text: str):
        self._is_speaking = True
        try:
            async for chunk in self.tts.synthesize(text):
                if self._stopped:
                    break
                self._send(chunk)
        finally:
            self._is_speaking = False
        logger.info(f"SIP call {self.call_id} AGENT: {text}")

    async def _speak_streaming(self, user_text: str):
        self._is_speaking = True
        buffer = ""
        full_reply = ""
        sentence_endings = (".", "!", "?", "।")
        try:
            async for token in self.engine.stream_response(user_text):
                if self._stopped or not self._is_speaking:
                    break
                buffer += token
                full_reply += token
                if any(buffer.rstrip().endswith(e) for e in sentence_endings):
                    sentence = buffer.strip()
                    buffer = ""
                    async for chunk in self.tts.synthesize(sentence):
                        if self._stopped or not self._is_speaking:
                            break
                        self._send(chunk)
            if buffer.strip() and self._is_speaking:
                async for chunk in self.tts.synthesize(buffer.strip()):
                    if self._stopped:
                        break
                    self._send(chunk)
        finally:
            self._is_speaking = False
        logger.info(f"SIP call {self.call_id} AGENT: {full_reply.strip()}")

    def _send(self, pcm: bytes):
        """Queue TTS PCM (16 kHz) to the SIP call's outbound buffer."""
        if self.call and not self._stopped:
            self.call.queue_outbound(pcm)

    async def _end_call_gracefully(self, farewell: str):
        await self._speak(farewell)
        await asyncio.sleep(0.3)
        if self.call:
            self.sip_client.register_thread()
            self.sip_client.hangup(self.call)

    async def _transfer(self, destination: str):
        try:
            import pjsua2 as pj  # local import; only present in SIP backend
            self.sip_client.register_thread()
            num = destination.lstrip("+")
            uri = f"sip:{num}@{self.sip_client.host}:{self.sip_client.server_port}"
            self.call.xfer(uri, pj.CallOpParam(True))
        except Exception:
            logger.exception("SIP transfer failed")
