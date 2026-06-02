"""
STT Providers - Speech-to-Text streaming abstraction.
Supports Deepgram (real-time websocket) and Whisper (chunked).
"""
import os
import asyncio
import json
import logging
from typing import Callable, Optional
from abc import ABC, abstractmethod
import websockets

logger = logging.getLogger(__name__)


class STTProvider(ABC):
    on_partial: Optional[Callable[[str], None]] = None
    on_final: Optional[Callable[[str], None]] = None

    @abstractmethod
    async def start(self): ...

    @abstractmethod
    async def stop(self): ...

    @abstractmethod
    def feed_audio(self, pcm: bytes): ...

    @staticmethod
    def create(provider: str, language: str) -> "STTProvider":
        provider = provider.lower()
        if provider == "deepgram":
            return DeepgramSTT(language)
        elif provider == "whisper":
            return WhisperSTT(language)
        elif provider == "google":
            return GoogleSTT(language)
        raise ValueError(f"Unknown STT provider: {provider}")


class DeepgramSTT(STTProvider):
    """Real-time streaming STT via Deepgram WebSocket. Lowest latency option."""

    def __init__(self, language: str = "en-US"):
        self.language = language
        self.api_key = os.getenv("DEEPGRAM_API_KEY")
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self):
        if not self.api_key:
            raise RuntimeError("DEEPGRAM_API_KEY not configured")

        self._loop = asyncio.get_event_loop()
        url = (
            "wss://api.deepgram.com/v1/listen"
            f"?encoding=linear16&sample_rate=16000&channels=1"
            f"&language={self.language}&punctuate=true&interim_results=true"
            f"&endpointing=300&vad_events=true"
        )
        headers = {"Authorization": f"Token {self.api_key}"}
        self.ws = await websockets.connect(url, extra_headers=headers)
        self._reader_task = asyncio.create_task(self._reader())
        logger.debug("Deepgram STT connected")

    async def stop(self):
        if self._reader_task:
            self._reader_task.cancel()
        if self.ws:
            try:
                await self.ws.send(json.dumps({"type": "CloseStream"}))
                await self.ws.close()
            except Exception:
                pass

    def feed_audio(self, pcm: bytes):
        """Called from sync context (RTP handler). Schedule send on loop."""
        if self.ws and self._loop:
            asyncio.run_coroutine_threadsafe(self._send(pcm), self._loop)

    async def _send(self, pcm: bytes):
        try:
            await self.ws.send(pcm)
        except Exception as e:
            logger.warning(f"Deepgram send failed: {e}")

    async def _reader(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg.get("type") == "Results":
                    alt = msg["channel"]["alternatives"][0]
                    transcript = alt.get("transcript", "").strip()
                    if not transcript:
                        continue
                    if msg.get("is_final"):
                        if self.on_final:
                            self.on_final(transcript)
                    else:
                        if self.on_partial:
                            self.on_partial(transcript)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f"Deepgram reader crashed: {e}")


class WhisperSTT(STTProvider):
    """
    Chunked STT via OpenAI Whisper. Higher latency but cheaper.
    Buffers ~1.5s of audio then sends to /audio/transcriptions.
    """

    BUFFER_SECONDS = 1.5
    BUFFER_BYTES = int(16000 * 2 * BUFFER_SECONDS)  # 16kHz, 16-bit

    def __init__(self, language: str = "en"):
        self.language = language[:2]
        self.api_key = os.getenv("OPENAI_API_KEY")
        self._buffer = bytearray()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._silence_frames = 0

    async def start(self):
        self._loop = asyncio.get_event_loop()

    async def stop(self):
        pass

    def feed_audio(self, pcm: bytes):
        self._buffer.extend(pcm)
        if len(self._buffer) >= self.BUFFER_BYTES:
            chunk = bytes(self._buffer)
            self._buffer.clear()
            asyncio.run_coroutine_threadsafe(self._transcribe(chunk), self._loop)

    async def _transcribe(self, pcm: bytes):
        import io, wave, aiohttp
        # Wrap as WAV
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        buf.seek(0)

        form = aiohttp.FormData()
        form.add_field("file", buf, filename="audio.wav", content_type="audio/wav")
        form.add_field("model", "whisper-1")
        form.add_field("language", self.language)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.openai.com/v1/audio/transcriptions",
                data=form,
                headers={"Authorization": f"Bearer {self.api_key}"},
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    text = data.get("text", "").strip()
                    if text and self.on_final:
                        self.on_final(text)


class GoogleSTT(STTProvider):
    """Google Cloud Speech streaming - placeholder. Implement with google-cloud-speech."""

    def __init__(self, language: str):
        self.language = language

    async def start(self):
        raise NotImplementedError("Wire up google-cloud-speech here")

    async def stop(self): ...
    def feed_audio(self, pcm: bytes): ...
