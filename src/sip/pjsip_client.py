"""
Direct SIP client (no Asterisk) built on PJSIP / pjsua2.

This lets the voice agent behave like a softphone: it REGISTERs as a SIP
extension to your PBX (e.g. ext 1055 @ 15.207.28.98:7719) and places outbound
calls directly. Audio frames from the call are bridged to the AI pipeline
(STT -> LLM -> TTS) via a custom pjsua2.AudioMediaPort.

Why pjsua2 instead of raw SIP/RTP: pjsua2 handles SIP registration, INVITE,
codec negotiation (G.711 ulaw/alaw, G.722, etc.), RTP, jitter buffering and
NAT for us. We only need to tap the decoded PCM stream.

Audio rate note:
   pjsua2's media clock is configurable; we run it at 16 kHz so frames match
   the agent pipeline (STT/TTS are all 16 kHz). PJSIP transparently resamples
   to/from the negotiated codec rate (usually 8 kHz G.711) on the wire.

pjsua2 runs its own native worker threads. All PJSIP objects MUST be touched
from a thread that pjsua2 knows about, and our agent runs on an asyncio loop.
We therefore marshal:
   - inbound audio (PJSIP thread -> asyncio loop)  via run_coroutine_threadsafe
   - outbound audio (asyncio loop -> PJSIP)         via a thread-safe queue the
     media port drains inside getFrame() on the PJSIP thread.
"""
import os
import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# pjsua2 is an optional/native dependency. Import lazily so the rest of the
# app (and unit tests) can run without it installed.
try:
    import pjsua2 as pj  # type: ignore

    PJSUA2_AVAILABLE = True
except ImportError:  # pragma: no cover
    pj = None
    PJSUA2_AVAILABLE = False


# The agent pipeline speaks 16 kHz, 16-bit, mono PCM end to end.
AGENT_CLOCK_RATE = 16000
FRAME_MS = 20
SAMPLES_PER_FRAME = AGENT_CLOCK_RATE * FRAME_MS // 1000  # 320
BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2  # 640


def _require_pjsua2():
    if not PJSUA2_AVAILABLE:
        raise RuntimeError(
            "pjsua2 is not installed. Install PJSIP with Python bindings "
            "(see docs/SIP_SETUP.md)."
        )


class _AudioBridgePort(pj.AudioMediaPort if PJSUA2_AVAILABLE else object):
    """
    A custom PJSIP audio port that is connected to the call's audio media.

    PJSIP calls:
      - onFrameReceived(frame): inbound audio from the remote party (PCM in).
        We forward it to `on_inbound_pcm` (the STT feed).
      - getFrame(frame) via the framework when it wants audio to PLAY to the
        remote party. We fill it from `outbound_queue` (TTS output).

    Both run on PJSIP's native media thread, so we keep them lock-light and
    never call into asyncio directly here except via thread-safe primitives.
    """

    def __init__(self):
        super().__init__()
        self.on_inbound_pcm: Optional[Callable[[bytes], None]] = None
        # Outbound PCM bytes queued by the asyncio side; drained per frame.
        self._outbound = bytearray()
        self._outbound_lock = threading.Lock()

    def createPort(self, name: str):
        """Create the underlying media port at the agent clock rate."""
        fmt = pj.MediaFormatAudio()
        fmt.type = pj.PJMEDIA_TYPE_AUDIO
        fmt.clockRate = AGENT_CLOCK_RATE
        fmt.channelCount = 1
        fmt.bitsPerSample = 16
        fmt.frameTimeUsec = FRAME_MS * 1000
        super().createPort(name, fmt)

    # ---- inbound: remote -> agent ----
    def onFrameReceived(self, frame):  # noqa: N802 (pjsua2 naming)
        try:
            buf = bytes(frame.buf)
            if buf and self.on_inbound_pcm:
                self.on_inbound_pcm(buf)
        except Exception:  # never let a callback kill the media thread
            logger.exception("onFrameReceived handler error")

    # ---- outbound: agent -> remote ----
    def queue_outbound(self, pcm: bytes):
        with self._outbound_lock:
            self._outbound.extend(pcm)

    def flush_outbound(self):
        with self._outbound_lock:
            self._outbound.clear()

    def onFrameRequested(self, frame):  # noqa: N802
        """PJSIP wants a frame to send to the remote party."""
        try:
            frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
            with self._outbound_lock:
                if len(self._outbound) >= BYTES_PER_FRAME:
                    chunk = bytes(self._outbound[:BYTES_PER_FRAME])
                    del self._outbound[:BYTES_PER_FRAME]
                else:
                    # underrun -> send silence so the call stays comfortable
                    chunk = bytes(self._outbound) + b"\x00" * (
                        BYTES_PER_FRAME - len(self._outbound)
                    )
                    self._outbound.clear()
            frame.buf = pj.ByteVector(chunk)
            frame.size = len(chunk)
        except Exception:
            logger.exception("onFrameRequested handler error")


class _AgentCall(pj.Call if PJSUA2_AVAILABLE else object):
    """One outbound/inbound SIP call. Owns the audio bridge port."""

    def __init__(self, acc, on_connected, on_disconnected, on_inbound_pcm):
        super().__init__(acc)
        self.bridge = _AudioBridgePort()
        self.bridge.on_inbound_pcm = on_inbound_pcm
        self.bridge.createPort("agent-bridge")
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._audio_connected = False

    def onCallState(self, prm):  # noqa: N802
        ci = self.getInfo()
        logger.info(f"SIP call state: {ci.stateText} ({ci.lastStatusCode})")
        if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            try:
                self._on_disconnected(ci.lastStatusCode)
            except Exception:
                logger.exception("on_disconnected error")

    def onCallMediaState(self, prm):  # noqa: N802
        """Media (audio) is up -> wire the call audio to our bridge port."""
        ci = self.getInfo()
        for i, mi in enumerate(ci.media):
            if (
                mi.type == pj.PJMEDIA_TYPE_AUDIO
                and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE
            ):
                call_audio = self.getAudioMedia(i)
                # remote -> bridge (inbound) and bridge -> remote (outbound)
                call_audio.startTransmit(self.bridge)
                self.bridge.startTransmit(call_audio)
                if not self._audio_connected:
                    self._audio_connected = True
                    try:
                        self._on_connected()
                    except Exception:
                        logger.exception("on_connected error")

    def queue_outbound(self, pcm: bytes):
        self.bridge.queue_outbound(pcm)

    def flush_outbound(self):
        self.bridge.flush_outbound()


class _AgentAccount(pj.Account if PJSUA2_AVAILABLE else object):
    """SIP account (the registered extension)."""

    def __init__(self):
        super().__init__()
        self.on_reg_state: Optional[Callable[[bool, int], None]] = None

    def onRegState(self, prm):  # noqa: N802
        ai = self.getInfo()
        active = ai.regIsActive
        logger.info(
            f"SIP registration: active={active} code={prm.code} ({prm.reason})"
        )
        if self.on_reg_state:
            self.on_reg_state(active, prm.code)


class PJSIPClient:
    """
    Manages the PJSIP endpoint lifecycle and one registered account.

    Usage:
        client = PJSIPClient(domain="15.207.28.98:7719", username="1055",
                             password="...", port=7719)
        client.start()                 # init lib + register
        call = client.make_call("+9199....", on_connected=..., ...)
        call.queue_outbound(pcm16k)    # play TTS audio
    """

    def __init__(
        self,
        domain: str,
        username: str,
        password: str,
        transport: str = "udp",
        local_port: int = 5060,
    ):
        _require_pjsua2()
        # `domain` may be "host:port" — split it for the SIP server URI.
        self.host, _, port = domain.partition(":")
        self.server_port = port or "5060"
        self.username = username
        self.password = password
        self.transport = transport
        self.local_port = local_port

        self.ep: Optional[pj.Endpoint] = None
        self.account: Optional[_AgentAccount] = None
        self._registered = threading.Event()

    def start(self):
        self.ep = pj.Endpoint()
        self.ep.libCreate()

        ep_cfg = pj.EpConfig()
        ep_cfg.uaConfig.maxCalls = 16
        # Run a dedicated PJSIP worker thread so SIP events (REGISTER replies,
        # INVITE responses) are pumped independently of the Python thread that
        # called start() — required when started from an asyncio executor.
        ep_cfg.uaConfig.threadCnt = 1
        ep_cfg.medConfig.clockRate = AGENT_CLOCK_RATE
        ep_cfg.medConfig.sndClockRate = AGENT_CLOCK_RATE
        ep_cfg.logConfig.level = 3
        self.ep.libInit(ep_cfg)

        # Transport
        tcfg = pj.TransportConfig()
        tcfg.port = self.local_port
        tp_type = (
            pj.PJSIP_TRANSPORT_TCP
            if self.transport.lower() == "tcp"
            else pj.PJSIP_TRANSPORT_UDP
        )
        self.ep.transportCreate(tp_type, tcfg)

        self.ep.libStart()
        logger.info("PJSIP endpoint started")

        self._register()

    def _register(self):
        acc_cfg = pj.AccountConfig()
        server = f"{self.host}:{self.server_port}"
        acc_cfg.idUri = f"sip:{self.username}@{self.host}"
        acc_cfg.regConfig.registrarUri = f"sip:{server}"

        cred = pj.AuthCredInfo("digest", "*", self.username, 0, self.password)
        acc_cfg.sipConfig.authCreds.append(cred)

        self.account = _AgentAccount()
        self.account.on_reg_state = self._on_reg_state
        self.account.create(acc_cfg)
        logger.info(f"Registering SIP account {acc_cfg.idUri} -> {server}")

    def _on_reg_state(self, active: bool, code: int):
        if active and 200 <= code < 300:
            self._registered.set()
        else:
            self._registered.clear()

    def wait_until_registered(self, timeout: float = 15.0) -> bool:
        return self._registered.wait(timeout)

    @property
    def is_registered(self) -> bool:
        return self._registered.is_set()

    def make_call(
        self,
        destination: str,
        on_connected: Callable[[], None],
        on_disconnected: Callable[[int], None],
        on_inbound_pcm: Callable[[bytes], None],
    ) -> "_AgentCall":
        """
        Place an outbound call. `destination` is a number or SIP user; it is
        dialed through the registered PBX (so PSTN routing happens on the PBX,
        exactly like dialing from the softphone).
        """
        if destination.startswith("sip:"):
            uri = destination
        else:
            uri = f"sip:{self._normalize_number(destination)}@{self.host}:{self.server_port}"

        call = _AgentCall(
            self.account, on_connected, on_disconnected, on_inbound_pcm
        )
        prm = pj.CallOpParam(True)
        call.makeCall(uri, prm)
        logger.info(f"Dialing {uri}")
        return call

    def _normalize_number(self, destination: str) -> str:
        """
        Turn a phone number into the digit string the PBX dialplan expects.

        Verified against the PBX at 15.207.28.98: it routes the 10-digit
        national number (e.g. 7791027690) and returns 404 for the country-code
        form (917791027690). So by default we strip a leading "+" and, if
        SIP_DIAL_STRIP_CC is set (e.g. "91"), strip that country code too.
        Set SIP_DIAL_PREFIX if your trunk needs a leading prefix (e.g. "0"/"9").
        """
        num = destination.lstrip("+").strip()
        strip_cc = os.getenv("SIP_DIAL_STRIP_CC", "").strip()
        if strip_cc and num.startswith(strip_cc) and len(num) > len(strip_cc):
            num = num[len(strip_cc):]
        prefix = os.getenv("SIP_DIAL_PREFIX", "").strip()
        return f"{prefix}{num}"

    def hangup(self, call: "_AgentCall"):
        try:
            prm = pj.CallOpParam(True)
            call.hangup(prm)
        except Exception:
            logger.exception("hangup failed")

    def shutdown(self):
        try:
            if self.account:
                self.account.shutdown()
            if self.ep:
                self.ep.libDestroy()
        except Exception:
            logger.exception("PJSIP shutdown error")

    # pjsua2 needs the calling thread registered with the library when called
    # from non-PJSIP threads (e.g. our asyncio loop).
    def register_thread(self, name: str = "asyncio-loop"):
        if self.ep and not self.ep.libIsThreadRegistered():
            self.ep.libRegisterThread(name)
