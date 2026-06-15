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
import os
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
        self._responding = False  # a turn (LLM+TTS) is in flight
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

        # Settle delay after answer so the RTP path is flowing AND the callee
        # has the phone to their ear before the greeting starts (without enough
        # delay the opener plays too early and is missed). Tunable via env.
        await asyncio.sleep(float(os.getenv("OPENER_DELAY_SEC", "1.5")))

        # Speak the opener and connect STT CONCURRENTLY. The opener text is
        # fixed and doesn't depend on STT, so there's no reason to make the
        # caller wait for the Deepgram WebSocket to connect before they hear it.
        opener = self.engine.get_initial_message()
        speak_task = asyncio.create_task(self._speak(opener)) if opener else None

        try:
            await self.stt.start()
        except Exception:
            logger.exception(f"SIP call {self.call_id}: STT failed to start; ending call")
            self.outcome = "stt_init_failed"
            if speak_task:
                await speak_task
            await self._end_call_gracefully("Sorry, we're having a technical issue. Goodbye.")
            return

        if speak_task:
            await speak_task

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
        # Serialize turns: if the agent is already responding, treat this as a
        # barge-in (stop talking) and let the new input through. This prevents
        # the overlapping/duplicate replies seen with eager transcripts.
        if self._is_speaking:
            await self._interrupt_speech()
        if self._responding:
            logger.debug(f"SIP call {self.call_id}: dropping overlapping input")
            return
        self._responding = True
        try:
            logger.info(f"SIP call {self.call_id} USER: {user_text}")

            if self.engine.should_end_call(user_text):
                self.outcome = "user_ended"
                await self._end_call_gracefully("Thank you, have a great day. Goodbye.")
                return

            if self.engine.should_transfer(user_text) and self.config.transfer_number:
                await self._speak("Sure, transferring you to a human agent now. Please hold.")
                await asyncio.sleep(0.5)
                self.outcome = "transferred"
                await self._transfer(self.config.transfer_number)
                return

            await self._speak_streaming(user_text)
        except Exception:
            # One bad turn must never kill the call.
            logger.exception(f"SIP call {self.call_id}: error handling user input")
        finally:
            self._responding = False

    async def _speak(self, text: str):
        self._is_speaking = True
        try:
            async for chunk in self.tts.synthesize(text):
                if self._stopped:
                    break
                self._send(chunk)
        except Exception:
            logger.exception(f"SIP call {self.call_id}: TTS error during _speak")
        finally:
            self._is_speaking = False
        logger.info(f"SIP call {self.call_id} AGENT: {text}")

    async def _speak_streaming(self, user_text: str):
        self._is_speaking = True
        buffer = ""
        full_reply = ""
        sentence_endings = (".", "!", "?", "।")
        try:
            import time as _t
            t0 = _t.monotonic()
            first_token_logged = False
            first_audio_logged = False
            first_flush = True
            async for token in self.engine.stream_response(user_text):
                if self._stopped or not self._is_speaking:
                    break
                if not first_token_logged:
                    logger.info(f"SIP call {self.call_id} LATENCY: LLM first token +{(_t.monotonic()-t0)*1000:.0f}ms")
                    first_token_logged = True
                buffer += token
                full_reply += token
                stripped = buffer.rstrip()
                # Latency win: speak the FIRST chunk as soon as a clause is ready
                # (sentence end, or a comma after enough words) instead of waiting
                # for a full sentence — the agent starts talking ~1-2s sooner.
                flush = any(stripped.endswith(e) for e in sentence_endings)
                if first_flush and not flush:
                    flush = stripped.endswith((",", ";", ":")) and len(stripped.split()) >= 4
                if flush:
                    sentence = buffer.strip()
                    buffer = ""
                    first_flush = False
                    async for chunk in self.tts.synthesize(sentence):
                        if self._stopped or not self._is_speaking:
                            break
                        if not first_audio_logged:
                            logger.info(f"SIP call {self.call_id} LATENCY: TTS first audio +{(_t.monotonic()-t0)*1000:.0f}ms")
                            first_audio_logged = True
                        self._send(chunk)
            if buffer.strip() and self._is_speaking:
                async for chunk in self.tts.synthesize(buffer.strip()):
                    if self._stopped:
                        break
                    self._send(chunk)
        except Exception:
            logger.exception(f"SIP call {self.call_id}: LLM/TTS error during reply")
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
