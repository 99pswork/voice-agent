#!/usr/bin/env python3
"""
SIP smoke test — register as the configured extension and place ONE call.

This deliberately bypasses the whole app (no FastAPI, no MongoDB, no STT/LLM/TTS)
so we can prove the SIP leg works on its own before wiring the AI back in.

When the call connects it plays a short tone + TTS-less spoken-ish beep pattern
so you can confirm two-way audio is alive. Then it hangs up after ~10s (or when
the remote party hangs up).

Usage:
    cd voice-agent
    .venv/bin/python scripts/sip_smoketest.py +917791027690

Reads SIP creds from .env (SIP_DOMAIN, SIP_USERNAME, SIP_PASSWORD, ...).
"""
import os
import sys
import time
import math
import struct
import threading
from pathlib import Path

# Load .env from the project root (parent of scripts/)
ROOT = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

try:
    import pjsua2 as pj
except ImportError:
    sys.exit(
        "pjsua2 is not installed in this interpreter.\n"
        "Run with the project venv:  .venv/bin/python scripts/sip_smoketest.py <number>\n"
        "and make sure the pjsua2 bindings were installed into that venv."
    )

CLOCK_RATE = 16000
FRAME_MS = 20
SAMPLES = CLOCK_RATE * FRAME_MS // 1000
BYTES = SAMPLES * 2


def _tone_frame(phase: float, freq: float = 440.0):
    """Generate one 20ms frame of a sine tone (16-bit PCM)."""
    out = bytearray()
    for i in range(SAMPLES):
        t = phase + i / CLOCK_RATE
        val = int(0.25 * 32767 * math.sin(2 * math.pi * freq * t))
        out += struct.pack("<h", val)
    return bytes(out), phase + SAMPLES / CLOCK_RATE


class TonePort(pj.AudioMediaPort):
    """Plays a repeating short tone to the remote party so audio is audible."""

    def __init__(self):
        super().__init__()
        self._phase = 0.0
        self._on = True

    def make(self, name):
        fmt = pj.MediaFormatAudio()
        fmt.type = pj.PJMEDIA_TYPE_AUDIO
        fmt.clockRate = CLOCK_RATE
        fmt.channelCount = 1
        fmt.bitsPerSample = 16
        fmt.frameTimeUsec = FRAME_MS * 1000
        self.createPort(name, fmt)

    def onFrameRequested(self, frame):
        frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
        # beep on/off ~ every 25 frames (0.5s) so it's clearly a test pattern
        if int(self._phase * 2) % 2 == 0:
            buf, self._phase = _tone_frame(self._phase)
        else:
            buf = b"\x00" * BYTES
            self._phase += SAMPLES / CLOCK_RATE
        frame.buf = pj.ByteVector(buf)
        frame.size = len(buf)

    def onFrameReceived(self, frame):
        # we just count inbound audio to confirm the remote->us path is alive
        pass


class Call(pj.Call):
    def __init__(self, acc, done_event):
        super().__init__(acc)
        self.done = done_event
        self.tone = TonePort()
        self.tone.make("smoketest-tone")
        self._connected = False

    def onCallState(self, prm):
        ci = self.getInfo()
        print(f"[call] state={ci.stateText} code={ci.lastStatusCode} ({ci.lastReason})")
        if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self.done.set()

    def onCallMediaState(self, prm):
        ci = self.getInfo()
        for i, mi in enumerate(ci.media):
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                aud = self.getAudioMedia(i)
                self.tone.startTransmit(aud)   # tone -> remote
                aud.startTransmit(self.tone)   # remote -> us
                if not self._connected:
                    self._connected = True
                    print("[call] ✅ media is ACTIVE — you should hear a beep pattern")


class Account(pj.Account):
    def __init__(self):
        super().__init__()
        self.reg_ok = threading.Event()

    def onRegState(self, prm):
        ai = self.getInfo()
        print(f"[reg] active={ai.regIsActive} code={prm.code} ({prm.reason})")
        if ai.regIsActive and 200 <= prm.code < 300:
            self.reg_ok.set()


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: sip_smoketest.py <destination-number>")
    dest = sys.argv[1]

    domain = os.getenv("SIP_DOMAIN", "")
    user = os.getenv("SIP_USERNAME", "")
    pwd = os.getenv("SIP_PASSWORD", "")
    transport = os.getenv("SIP_TRANSPORT", "udp").lower()
    local_port = int(os.getenv("SIP_LOCAL_PORT", "5060"))

    if not domain or not user or pwd in ("", "change-me"):
        sys.exit(
            f"Missing/placeholder SIP creds in .env "
            f"(SIP_DOMAIN={domain!r}, SIP_USERNAME={user!r}, SIP_PASSWORD set={pwd not in ('', 'change-me')}).\n"
            "Fill SIP_PASSWORD (and confirm domain/user) before testing."
        )

    host, _, port = domain.partition(":")
    server_port = port or "5060"

    ep = pj.Endpoint()
    ep.libCreate()
    cfg = pj.EpConfig()
    cfg.uaConfig.maxCalls = 4
    cfg.medConfig.clockRate = CLOCK_RATE
    cfg.logConfig.level = 4
    ep.libInit(cfg)

    tcfg = pj.TransportConfig()
    tcfg.port = local_port
    tp = pj.PJSIP_TRANSPORT_TCP if transport == "tcp" else pj.PJSIP_TRANSPORT_UDP
    ep.transportCreate(tp, tcfg)
    ep.libStart()
    print(f"[ep] started, dialing as {user}@{host}:{server_port} via {transport}")

    acc_cfg = pj.AccountConfig()
    acc_cfg.idUri = f"sip:{user}@{host}"
    acc_cfg.regConfig.registrarUri = f"sip:{host}:{server_port}"
    acc_cfg.sipConfig.authCreds.append(
        pj.AuthCredInfo("digest", "*", user, 0, pwd)
    )
    acc = Account()
    acc.create(acc_cfg)

    print("[reg] waiting up to 15s for registration...")
    if not acc.reg_ok.wait(15):
        print("[reg] ⚠️  not registered — check password / reachability of "
              f"{host}:{server_port}. Aborting.")
        ep.libDestroy()
        return

    # Apply the same normalization the app uses (strip country code / add prefix
    # per SIP_DIAL_STRIP_CC / SIP_DIAL_PREFIX) so the smoke test matches reality.
    num = dest.lstrip("+").strip()
    strip_cc = os.getenv("SIP_DIAL_STRIP_CC", "").strip()
    if strip_cc and num.startswith(strip_cc) and len(num) > len(strip_cc):
        num = num[len(strip_cc):]
    num = os.getenv("SIP_DIAL_PREFIX", "").strip() + num
    uri = f"sip:{num}@{host}:{server_port}"
    done = threading.Event()
    call = Call(acc, done)
    print(f"[call] dialing {uri} ...")
    call.makeCall(uri, pj.CallOpParam(True))

    # let it ring/talk up to 30s, or until remote hangs up
    done.wait(30)
    try:
        call.hangup(pj.CallOpParam(True))
    except Exception:
        pass
    time.sleep(1)
    print("[done] tearing down")
    ep.libDestroy()


if __name__ == "__main__":
    main()
