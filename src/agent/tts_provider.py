"""
TTS Providers - Text-to-Speech streaming abstraction.
ElevenLabs (best quality, low latency) and OpenAI (cheap, decent).
"""
import os
import asyncio
import logging
import json
from typing import AsyncGenerator
from abc import ABC, abstractmethod
import aiohttp
import websockets

logger = logging.getLogger(__name__)


class TTSProvider(ABC):
    @abstractmethod
    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]: ...

    @abstractmethod
    async def stop_current(self): ...

    @abstractmethod
    async def stop(self): ...

    @staticmethod
    def create(provider: str, voice: str) -> "TTSProvider":
        provider = provider.lower()
        if provider == "elevenlabs":
            return ElevenLabsTTS(voice)
        elif provider == "openai":
            return OpenAITTS(voice)
        elif provider == "azure":
            return AzureTTS(voice)
        raise ValueError(f"Unknown TTS provider: {provider}")


class ElevenLabsTTS(TTSProvider):
    """
    Streaming TTS via ElevenLabs WS. Returns 16kHz PCM chunks for direct
    feeding into RTP.
    """

    def __init__(self, voice_id: str):
        self.voice_id = voice_id
        self.api_key = os.getenv("ELEVENLABS_API_KEY")
        self.model = os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5")
        self._cancel_event = asyncio.Event()

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        self._cancel_event.clear()
        if not self.api_key:
            raise RuntimeError("ELEVENLABS_API_KEY not configured")

        # optimize_streaming_latency=4 (max) for the lowest first-audio latency;
        # paired with the Flash model this is the fastest ElevenLabs config for
        # live phone turns.
        url = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream-input"
            f"?model_id={self.model}&output_format=pcm_16000"
            f"&optimize_streaming_latency=4"
        )
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({
                "text": " ",
                "voice_settings": {"stability": 0.4, "similarity_boost": 0.7},
                "xi_api_key": self.api_key,
            }))
            await ws.send(json.dumps({"text": text + " ", "try_trigger_generation": True}))
            await ws.send(json.dumps({"text": ""}))  # signal end

            try:
                while not self._cancel_event.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    msg = json.loads(raw)
                    if msg.get("audio"):
                        import base64
                        yield base64.b64decode(msg["audio"])
                    if msg.get("isFinal"):
                        break
            except asyncio.TimeoutError:
                logger.warning("ElevenLabs stream timeout")
            except websockets.ConnectionClosed:
                pass

    async def stop_current(self):
        self._cancel_event.set()

    async def stop(self):
        self._cancel_event.set()


class OpenAITTS(TTSProvider):
    """OpenAI TTS - simpler HTTP, doesn't stream as smoothly but works fine."""

    def __init__(self, voice: str):
        self.voice = voice if voice in {"alloy", "echo", "fable", "onyx", "nova", "shimmer"} else "alloy"
        self.api_key = os.getenv("OPENAI_API_KEY")
        self._cancel_event = asyncio.Event()

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        self._cancel_event.clear()
        import audioop

        # ratecv is stateful: carry `rate_state` across chunks for a clean,
        # click-free 24kHz->16kHz conversion. `leftover` holds a trailing odd
        # byte so we never feed ratecv a partial 16-bit sample.
        rate_state = None
        leftover = b""

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": "tts-1",
                    "voice": self.voice,
                    "input": text,
                    "response_format": "pcm",  # 24kHz raw PCM, mono, 16-bit
                },
            ) as r:
                async for chunk in r.content.iter_chunked(3200):
                    if self._cancel_event.is_set():
                        break
                    data = leftover + chunk
                    # keep only a whole number of 2-byte frames
                    usable = len(data) - (len(data) % 2)
                    leftover = data[usable:]
                    data = data[:usable]
                    if not data:
                        continue
                    converted, rate_state = audioop.ratecv(
                        data, 2, 1, 24000, 16000, rate_state
                    )
                    yield converted

    async def stop_current(self):
        self._cancel_event.set()

    async def stop(self):
        self._cancel_event.set()


class AzureTTS(TTSProvider):
    """Azure Cognitive Services TTS - placeholder."""

    def __init__(self, voice: str):
        self.voice = voice

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        raise NotImplementedError("Wire up azure-cognitiveservices-speech")
        yield b""

    async def stop_current(self): ...
    async def stop(self): ...
