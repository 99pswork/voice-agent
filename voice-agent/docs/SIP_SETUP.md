# Direct SIP integration (no Asterisk)

This wires the voice agent straight into your SIP server as a registering
extension — exactly like the softphone (Zoiper) you're already using.

Your account (from the softphone):

| Field      | Value              |
|------------|--------------------|
| Extension  | `1055`             |
| Password   | (your password)    |
| SIP server | `15.207.28.98:7719`|

The agent registers as `1055`, then places outbound calls through that same
PBX. PSTN routing (reaching a mobile number) happens on the PBX, just as it
does when you dial from the softphone.

> ⚠️ A PBX usually allows **one registration per extension**. While the agent
> is running as `1055`, your softphone logged in as `1055` will likely be
> kicked offline. Ask for a second extension (e.g. `1056`) for the agent when
> you can — then both can run at once.

---

## How it works

```
 ┌────────────┐   SIP REGISTER / INVITE    ┌──────────────────┐   PSTN   ┌────────┐
 │ voice-agent │ ─────────────────────────▶ │ PBX 15.207.28.98 │ ───────▶ │ mobile │
 │ (pjsua2)    │ ◀── RTP audio (G.711) ────▶ │      :7719        │          └────────┘
 └─────┬──────┘                             └──────────────────┘
       │ PCM 16 kHz
       ▼
   STT ─▶ LLM + RAG ─▶ TTS ──┐
       ▲                     │
       └─────────────────────┘  (audio back to the caller)
```

PJSIP negotiates the codec (normally 8 kHz G.711 µ-law/a-law) and resamples
to/from the agent's internal 16 kHz PCM automatically — you don't manage codecs.

Code:
- `src/sip/pjsip_client.py`     — PJSIP endpoint, registration, dialing, audio tap
- `src/sip/sip_call_session.py` — per-call STT→LLM→TTS loop
- `src/sip/sip_call_manager.py` — outbound call orchestration
- `src/sip/sip_backend.py`      — telephony backend on `app.state.telephony`

---

## 1. Install PJSIP with Python (pjsua2) bindings

pjsua2 is a **native** library, not a pip wheel. Two options:

### Option A — Homebrew + SWIG build (macOS, for local dev)

```bash
brew install pjsip swig python@3.11
# Build the pjsua2 python module from the pjproject source:
git clone https://github.com/pjsip/pjproject.git
cd pjproject
./configure --enable-shared && make dep && make
cd pjsip-apps/src/swig/python
make && make install      # installs the pjsua2 module into your active python
python -c "import pjsua2; print('pjsua2 OK')"
```

### Option B — Docker (recommended for the server, reproducible)

Add to the `Dockerfile` (a build stage that compiles pjsua2):

```dockerfile
RUN apt-get update && apt-get install -y \
      build-essential libssl-dev libasound2-dev swig python3-dev \
 && git clone --depth 1 https://github.com/pjsip/pjproject.git /tmp/pj \
 && cd /tmp/pj && ./configure CFLAGS="-fPIC" --enable-shared \
 && make dep && make && make install && ldconfig \
 && cd pjsip-apps/src/swig/python && make && make install \
 && rm -rf /tmp/pj
```

Then `python -c "import pjsua2"` inside the container should succeed.

> If pjsua2 is not installed, the app still starts, but any dial attempt raises
> a clear error telling you to install it.

---

## 2. Configure `.env`

```dotenv
SIP_DOMAIN=15.207.28.98:7719
SIP_USERNAME=1055
SIP_PASSWORD=your-real-password
SIP_TRANSPORT=udp
SIP_LOCAL_PORT=5060
SIP_DIAL_STRIP_CC=91

# Plus the AI keys the pipeline needs:
OPENAI_API_KEY=sk-...
DEEPGRAM_API_KEY=...
ELEVENLABS_API_KEY=...
```

> If you have NAT/firewall between the agent host and the PBX, make sure the
> local SIP/RTP UDP ports are reachable, or run the agent on the same network
> as the PBX. PJSIP handles symmetric RTP, which covers most NAT cases.

---

## 3. Start the service and confirm registration

```bash
cd voice-agent
pip install -r requirements.txt
python src/main.py        # or: uvicorn main:app --app-dir src --port 8000
```

Look for this log line:

```
SIP registration: active=True code=200 (OK)
SIP backend registered and ready
```

If you see `code=401/403`, the username/password/domain is wrong.
If you see `code=408` (timeout), the agent can't reach `15.207.28.98:7719`.

---

## 4. Create an agent, then place ONE outbound call

```bash
# (a) create a voice agent
curl -X POST localhost:8000/api/v1/agents -H 'Content-Type: application/json' -d '{
  "name": "Test Agent",
  "base_instructions": "You are a friendly assistant making a test call. Greet the person, confirm they can hear you, then say goodbye.",
  "initial_message": "Hi! This is an AI test call. Can you hear me clearly?",
  "stt_provider": "deepgram",
  "tts_provider": "elevenlabs",
  "voice": "Rachel",
  "language": "en-US"
}'
# -> note the returned agent id, e.g. "agent_abc123"

# (b) place the call to a mobile number (E.164)
curl -X POST localhost:8000/api/v1/calls/outbound -H 'Content-Type: application/json' -d '{
  "agent_id": "agent_abc123",
  "destination": "+9198XXXXXXXX"
}'
```

What should happen:
1. The agent dials `+9198XXXXXXXX` through the PBX.
2. When the mobile answers, the agent speaks the `initial_message`.
3. You talk; STT → LLM → TTS replies back over the call.
4. Say "goodbye" (or wait for max duration) and the call ends; transcript +
   outcome are saved on the call record (`GET /api/v1/calls/{call_id}`).

> The `trunk` field in the outbound request is ignored in direct-SIP mode —
> the PBX decides how the number reaches the PSTN, exactly as with the softphone.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `pjsua2 is not installed` on dial | Build pjsua2 (step 1) into the same venv the app runs in. |
| Registers, but call drops instantly (`code=403/404`) | Extension not allowed to dial that number, or number format. Try dialing an internal extension first. |
| Call connects but no audio one way | NAT/RTP reachability. Run agent on the PBX's network, or open RTP UDP ports. |
| Softphone keeps logging the agent out | Single-registration extension. Get a dedicated extension for the agent. |
| Robotic / wrong-speed audio | Codec/rate mismatch — confirm the PBX offers G.711; PJSIP resamples to 16 kHz internally. |
