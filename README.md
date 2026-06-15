# Voice Calling Agent

AI-powered outbound voice agent that registers as a **SIP extension** on your PBX (via PJSIP/pjsua2) and places calls directly — no Asterisk required. The agent dials your customers, holds a natural conversation using an LLM grounded on documents you upload, and hands off to a human when needed.

---

## Table of Contents

1. [What this service does](#what-this-service-does)
2. [Architecture](#architecture)
3. [Prerequisites](#prerequisites)
4. [Installation](#installation)
5. [Configuration](#configuration)
6. [SIP Integration](#sip-integration)
7. [API Reference](#api-reference)
8. [End-to-end walkthrough](#end-to-end-walkthrough)
9. [Operational notes](#operational-notes)
10. [Troubleshooting](#troubleshooting)
11. [Security](#security)

---

## What this service does

This service registers as a **SIP extension on your PBX** (like a softphone) and uses it to:

- Originate outbound calls through your PBX's existing trunks
- Stream the caller's audio into an AI loop (STT → LLM → TTS)
- Maintain natural, interruptible conversation
- Use a knowledge base (PDF / DOCX / URL) for accurate, grounded answers
- Transfer to a human agent on request
- Save transcripts and fire webhooks back to your CRM

It does **not replace** your PBX — it plugs into it as just another registered extension. Your existing inbound flows, queues, IVR, and softphones continue to work unchanged.

## Architecture

```
   PSTN / mobiles
        ▲
        │ SIP + RTP
        ▼
   ┌─────────────────────┐
   │   YOUR PBX          │   (e.g. 15.207.28.98:7719)
   │   extensions/trunks │
   └──────────┬──────────┘
              │ SIP REGISTER (as an extension) + RTP audio
              ▼
   ┌───────────────────────────────────────────────────┐
   │   VOICE CALLING AGENT (this service)              │
   │                                                   │
   │   FastAPI ──► SIPCallManager ──► SIPCallSession   │
   │                       (PJSIP / pjsua2)            │
   │                                  │                │
   │                                  ▼                │
   │   STT (Deepgram) ──► ConversationEngine           │
   │                       │   (LLM + RAG)             │
   │                       ▼                           │
   │   TTS (ElevenLabs) ──► RTP back to the caller     │
   │                                                   │
   │   MongoDB (agents, calls, transcripts, KB embeddings) │
   └───────────────────────────────────────────────────┘
              ▲
              │ REST API
       ┌──────┴──────┐
       │ CRM/Backend │
       └─────────────┘
```

**Key idea:** the service uses **PJSIP/pjsua2** to register itself as a SIP extension on your PBX — exactly like a softphone. To place a call it sends an INVITE through the PBX (which handles PSTN routing through your trunks). PJSIP negotiates the codec (usually 8 kHz G.711) and resamples to the agent's internal 16 kHz PCM. We feed inbound audio to streaming STT, run the LLM with knowledge-base retrieval, synthesize the reply, and stream PCM back over RTP. No Asterisk/ARI required.

See **[docs/SIP_SETUP.md](docs/SIP_SETUP.md)** for the full SIP build/setup guide.

---

## Prerequisites

| Component | Purpose | Notes |
|-----------|---------|-------|
| **A SIP account** (extension + password + domain) | Registers the agent on your PBX | Softphone-style credentials; one registration per extension |
| **PJSIP with Python bindings (pjsua2)** | SIP signalling + RTP/codec handling | Native build — see [docs/SIP_SETUP.md](docs/SIP_SETUP.md) |
| **MongoDB 5+ (or Atlas)** | Stores agents, calls, transcripts, AND KB embeddings | No separate vector DB needed |
| **Python 3.11+** | Runtime | Or use the provided Docker setup |
| **OpenAI API key** | LLM + embeddings | GPT-4o-mini works well; GPT-4o for best quality |
| **Deepgram API key** | Real-time STT | Cheaper/faster than Whisper streaming |
| **ElevenLabs API key** | TTS | Or use OpenAI TTS as fallback |
| **Network reachability** | Agent → PBX over SIP (UDP/TCP) and RTP (UDP) | Same network as the PBX strongly recommended |

---

## Installation

### Option A — Docker (recommended)

```bash
git clone <this-repo> voice-agent
cd voice-agent
cp .env.example .env
# Edit .env with your SIP account + AI provider keys
docker-compose up -d
```

The agent container uses `network_mode: host` so SIP/RTP UDP ports are reachable to/from the PBX. MongoDB is MongoDB Atlas by default (set `MONGO_URL` in `.env`); KB embeddings are stored in Mongo too, so there's no separate vector DB. (Note: pjsua2 must be built into the image — see the Dockerfile notes in [docs/SIP_SETUP.md](docs/SIP_SETUP.md).)

### Option B — Bare metal

```bash
git clone <this-repo> voice-agent
cd voice-agent
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env

# MongoDB: use MongoDB Atlas (set MONGO_URL in .env) — recommended.
# Or run a local MongoDB:  docker run -d -p 27017:27017 mongo:7
# (KB embeddings live in Mongo; no separate vector DB needed.)

# Start the agent
export PYTHONPATH=$(pwd)/src
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000
```

Verify:

```bash
curl http://localhost:8000/health
# {"status":"healthy","service":"voice-calling-agent"}
```

---

## Configuration

All settings live in `.env`. The most important values:

```bash
# SIP account — registers the agent on your PBX (softphone-style credentials)
SIP_DOMAIN=15.207.28.98:7719   # SIP server host:port
SIP_USERNAME=1055              # extension
SIP_PASSWORD=your-password
SIP_TRANSPORT=udp
SIP_LOCAL_PORT=5060

# Outbound number formatting for the PBX dialplan
SIP_DIAL_STRIP_CC=91           # strip leading country code (this PBX wants 10-digit)
SIP_DIAL_PREFIX=               # add a trunk prefix here if your PBX needs one

# AI providers
OPENAI_API_KEY=sk-...
DEEPGRAM_API_KEY=...
ELEVENLABS_API_KEY=...
```

---

## SIP Integration

This service registers itself as a **SIP extension** on your PBX (like a softphone), so there is **no Asterisk/ARI config to add** — you only need valid SIP credentials.

1. **Install PJSIP/pjsua2** (native build) — see [docs/SIP_SETUP.md](docs/SIP_SETUP.md).
2. **Put your SIP account in `.env`** (`SIP_DOMAIN`, `SIP_USERNAME`, `SIP_PASSWORD`).
3. **Confirm registration + a test call:**

```bash
.venv/bin/python scripts/sip_smoketest.py <number>
# Look for: [reg] active=True code=200, then [call] ✅ media is ACTIVE
```

> **Number format:** PBXs differ on what they route. If a call returns `404 Not Found`, the dialed string didn't match the dialplan — adjust `SIP_DIAL_STRIP_CC` / `SIP_DIAL_PREFIX`. Dial whatever format works from your softphone.

> **One registration per extension:** while the agent is registered as an extension, a softphone logged in as the same extension may be kicked offline. Use a dedicated extension for the agent when possible.

### Network / firewall

| Direction | Port | Protocol | Purpose |
|-----------|------|----------|---------|
| Agent ↔ PBX | 5060 (or `SIP_LOCAL_PORT`) | UDP/TCP | SIP signalling |
| Agent ↔ PBX | RTP range | UDP | Call audio |

---

## API Reference

Base URL: `http://<agent-host>:8000/api/v1`

### Knowledge Base

#### Create a knowledge base

```bash
POST /knowledge-base
{
  "name": "product_catalog",
  "description": "Pricing and feature specs",
  "chunk_size": 800,
  "chunk_overlap": 100
}
```

Response: `{"id": "kb_abc123...", "status": "ready", ...}`

#### Upload documents

```bash
POST /knowledge-base/{kb_id}/upload
# multipart/form-data with one or more `files` fields
```

Supported: PDF, DOCX, TXT, MD, CSV, HTML, JSON. Max 50 MB per file. Documents are processed asynchronously — poll `GET /knowledge-base/{kb_id}` and watch `chunk_count` grow.

#### Add a URL

```bash
POST /knowledge-base/{kb_id}/url?url=https://yourcompany.com/faq
```

Scrapes and indexes the page contents.

#### Test retrieval

```bash
POST /knowledge-base/{kb_id}/search?query=what+is+the+refund+policy&top_k=3
```

Returns top-k chunks with similarity scores. Useful for sanity-checking what the agent can "see".

### Agents

#### Create a voice agent

```bash
POST /agents
{
  "name": "Sales - Premium Plan",
  "base_instructions": "You are Riya, a polite sales agent for ABC Corp. Pitch the premium plan, answer questions from the knowledge base, and offer to transfer to a human if asked.",
  "voice": "nova",
  "language": "en-IN",
  "knowledge_base_ids": ["kb_abc123"],
  "llm_model": "gpt-4o-mini",
  "stt_provider": "deepgram",
  "tts_provider": "elevenlabs",
  "max_call_duration": 600,
  "interruption_enabled": true,
  "initial_message": "Hi, am I speaking with {customer_name}?",
  "end_call_phrases": ["goodbye", "bye", "not interested"],
  "transfer_number": "+918012345678",
  "webhook_url": "https://your-crm.com/voice-callback"
}
```

**`base_instructions` is your system prompt.** Write it like you'd write a brief for a human telecaller — describe the persona, goal, tone, do's and don'ts, and edge cases. The KB content is automatically appended at runtime.

**Template variables:** Use `{variable_name}` in `base_instructions` and `initial_message`. They're replaced when you place a call, e.g. `{customer_name}` → `"Rahul"`.

#### Test the agent (text only, no call)

```bash
POST /agents/{agent_id}/test?message=What+is+your+refund+policy
```

Quick way to verify the prompt and KB are working before placing a real call.

### Calls

#### Place a single outbound call

```bash
POST /calls/outbound
{
  "agent_id": "agent_abc123",
  "destination": "+919812345678",
  "caller_id": "+1800123456",
  "trunk": "my-sip-provider",
  "variables": {
    "customer_name": "Rahul",
    "order_id": "ORD-998"
  },
  "metadata": {
    "crm_lead_id": "LEAD-9981"
  }
}
```

`destination` accepts:

- E.164 number: `+919812345678` → dialed as `PJSIP/919812345678@<trunk>`
- Full SIP URI: `sip:user@sip.example.com` → dialed as `PJSIP/sip:user@sip.example.com`

#### Bulk dial (campaigns)

```bash
POST /calls/bulk?agent_id=agent_abc123&rate_per_second=5
[
  {"destination": "+919812345678", "variables": {"customer_name": "Rahul"}},
  {"destination": "+919876543210", "variables": {"customer_name": "Priya"}},
  ...
]
```

Throttled by `rate_per_second` to respect SIP trunk concurrency limits.

#### Get call status / transcript

```bash
GET /calls/{call_id}
```

Returns full record including `status`, `duration_seconds`, `transcript`, and `outcome`.

#### Hang up / transfer mid-call

```bash
POST /calls/{call_id}/hangup
POST /calls/{call_id}/transfer?destination=+918012345678
```

### Webhooks (post-call)

If `webhook_url` is set on the agent, after every call the service POSTs:

```json
{
  "event": "call.completed",
  "call_id": "call_abc123",
  "duration_seconds": 142,
  "transcript": [
    {"role": "assistant", "content": "Hi, am I speaking with Rahul?"},
    {"role": "user",      "content": "Yes, who is this?"},
    ...
  ],
  "outcome": "user_ended"
}
```

The `X-Webhook-Signature: sha256=<hmac>` header lets you verify authenticity using `WEBHOOK_SECRET`.

### CRM trigger webhook

To trigger a call **from your CRM**:

```bash
POST /webhooks/trigger-call
{
  "agent_id": "agent_abc123",
  "destination": "+919812345678",
  "variables": {"customer_name": "Rahul"}
}
```

This is the same as `/calls/outbound` but designed to be the simple "incoming webhook" you wire into your CRM's automation rules.

---

## End-to-end walkthrough

### Step 1 — Build the knowledge base

```bash
# Create the KB
curl -X POST http://localhost:8000/api/v1/knowledge-base \
  -H "Content-Type: application/json" \
  -d '{"name":"product_catalog"}'
# → {"id": "kb_xyz", ...}

# Upload your PDF
curl -X POST http://localhost:8000/api/v1/knowledge-base/kb_xyz/upload \
  -F "files=@./catalog.pdf" \
  -F "files=@./pricing.pdf"

# Wait a few seconds, then verify
curl http://localhost:8000/api/v1/knowledge-base/kb_xyz
# {"chunk_count": 142, "status": "ready", ...}

# Test retrieval
curl -X POST "http://localhost:8000/api/v1/knowledge-base/kb_xyz/search?query=refund+policy&top_k=3"
```

### Step 2 — Create the agent

```bash
curl -X POST http://localhost:8000/api/v1/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Sales Bot",
    "base_instructions": "You are Riya, a polite sales rep for ABC Corp. Greet the customer by name, ask if they have 2 minutes, then pitch the premium plan based on the knowledge base. Be warm but concise.",
    "voice": "nova",
    "language": "en-IN",
    "knowledge_base_ids": ["kb_xyz"],
    "initial_message": "Hi, am I speaking with {customer_name}?",
    "transfer_number": "+918012345678",
    "webhook_url": "https://crm.example.com/api/voice-callback"
  }'
# → {"id": "agent_abc", ...}
```

### Step 3 — Test in text mode

```bash
curl -X POST "http://localhost:8000/api/v1/agents/agent_abc/test?message=What%20is%20included%20in%20the%20premium%20plan"
```

If the reply makes sense, you're ready to call.

### Step 4 — Place a call

```bash
curl -X POST http://localhost:8000/api/v1/calls/outbound \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent_abc",
    "destination": "+919812345678",
    "variables": {"customer_name": "Rahul"}
  }'
# → {"call_id": "call_xyz", "status": "initiated", ...}
```

### Step 5 — Watch what happens

```bash
# Server logs (SIP signalling + STT/LLM/TTS)
docker logs -f voice-agent

# Final transcript
curl http://localhost:8000/api/v1/calls/call_xyz | jq '.transcript'
```

A complete Python example is in `examples/python_client.py`.

---

## Operational notes

### Latency

End-to-end response latency target: **600–1000 ms** from end-of-user-speech to start-of-agent-speech. Achieved by:

- Streaming STT (Deepgram) — user partial transcripts arrive ~100 ms behind speech
- Streaming LLM tokens (OpenAI) — first token in ~250 ms
- Streaming TTS (ElevenLabs Turbo) — first audio chunk in ~150 ms after first sentence boundary
- Sentence-level pipelining in `CallSession._speak_streaming()`

If you see higher latency, the usual culprits are: (a) network distance to AI providers, (b) packet loss on the RTP path, (c) running on a CPU with poor single-thread performance.

### Concurrency

Each active call uses:

- 1 Deepgram WebSocket
- 1 OpenAI streaming connection (per turn)
- 1 ElevenLabs WebSocket (per agent reply)
- 1 UDP socket for RTP
- ~50 MB RAM

A single instance handles a modest number of concurrent calls (bounded by `maxCalls` in the PJSIP endpoint config and your CPU). Scale horizontally by running multiple instances, each registered as its own extension, and distributing targets across them.

### Cost (rough, USD)

For a typical 3-minute call:
- STT (Deepgram Nova-2): ~$0.013
- LLM (GPT-4o-mini, ~3k tokens): ~$0.001
- TTS (ElevenLabs Turbo, ~600 chars): ~$0.05
- **Total: ~$0.07 per 3-min call** (excluding your existing PSTN/trunk charges)

### Recording

The agent already has both audio streams in `SIPCallSession` (inbound PCM via
`on_inbound_pcm`, outbound PCM via `_send`). To record, tee those 16 kHz PCM
buffers to a WAV file and store the path on the call record (`recording_path`),
which the `GET /calls/{id}/recording` endpoint already serves.

---

## Troubleshooting

### "Registration fails (401/403) or times out (408)"

- `401/403` → wrong `SIP_USERNAME` / `SIP_PASSWORD`.
- `408` (timeout) → the agent can't reach `SIP_DOMAIN`; check network/firewall to the PBX host:port.
- Run `scripts/sip_smoketest.py` and read the `[reg]` line.

### "Call returns 404 Not Found"

The PBX has no route for the dialed string — a dialplan/format issue, not a bug. Dial whatever format works from your softphone, and set `SIP_DIAL_STRIP_CC` / `SIP_DIAL_PREFIX` to match.

### "Call connects but no audio / one-way audio"

Almost always RTP routing/NAT:

1. Run the agent on the same network as the PBX, or ensure RTP UDP ports are reachable both ways.
2. If running in Docker, the agent container **must** use `network_mode: host` (or publish the SIP/RTP UDP ports).
3. Confirm the negotiated codec — the PBX should offer G.711 (PCMU/PCMA); PJSIP resamples to 16 kHz internally.

### "Agent talks too fast / cuts itself off"

Tweak in `agent/conversation_engine.py`:
- Lower `temperature` for more predictable responses
- Adjust `max_tokens` (200 is a good default for voice)
- Tighten `base_instructions` — voice prompting differs from chat prompting; tell the agent explicitly to be short

### "Knowledge base answers are wrong/irrelevant"

- Use `/knowledge-base/{kb_id}/search` to see what's actually retrieved for a query
- If retrieved chunks look right but the agent ignores them, your `base_instructions` is overriding — add an explicit line: *"Always use the knowledge base provided. If the answer isn't there, say you'll follow up."*
- If retrieved chunks are wrong, tune `chunk_size`/`chunk_overlap` (smaller chunks for FAQ-style content, larger for narrative documents)
- Consider switching to `text-embedding-3-large` for better recall on multilingual content

### "Registration drops after a while"

The agent re-registers automatically before expiry. If it keeps dropping, the PBX may only allow one registration per extension and another device (e.g. a softphone) is logging in as the same extension — give the agent its own extension.

---

## Security

- **Never expose port 8000 to the public internet.** Put it behind your existing reverse proxy with auth.
- **Keep SIP credentials in `.env` only** and restrict who can read it; the agent's extension can place real (billable) calls.
- **Use webhook HMAC verification** in your CRM to confirm post-call payloads originated from this service.
- **Rotate API keys** for OpenAI / Deepgram / ElevenLabs regularly.
- **GDPR / DPDP / TCPA compliance** is your responsibility — make sure you have consent before placing automated calls and that the agent identifies itself appropriately. The `base_instructions` is the right place to enforce this (e.g. *"Always start by saying: 'This is an automated call from ABC Corp.'"*).
- **Recordings and transcripts contain PII** — store them encrypted at rest.

---

## Repository layout

```
voice-agent/
├── src/
│   ├── main.py                       # FastAPI entrypoint
│   ├── api/routes/
│   │   ├── agents.py                 # Agent CRUD
│   │   ├── knowledge_base.py         # KB upload / search
│   │   ├── calls.py                  # Outbound calls
│   │   ├── campaigns.py              # Bulk dialing status
│   │   └── webhooks.py               # CRM trigger webhooks
│   ├── agent/
│   │   ├── conversation_engine.py    # LLM + RAG loop
│   │   ├── voice_agent_config.py     # Config dataclass
│   │   ├── stt_provider.py           # Deepgram / Whisper / Google
│   │   └── tts_provider.py           # ElevenLabs / OpenAI / Azure
│   ├── sip/
│   │   ├── pjsip_client.py           # PJSIP/pjsua2 endpoint, registration, dialing, audio tap
│   │   ├── sip_backend.py            # Telephony backend (app.state.telephony)
│   │   ├── sip_call_manager.py       # Outbound call orchestration
│   │   ├── sip_call_session.py       # Per-call STT→LLM→TTS audio loop
│   │   └── dialer.py                 # Bulk campaign engine
│   ├── kb/
│   │   ├── document_processor.py     # PDF/DOCX/HTML extraction
│   │   └── vector_store.py           # MongoDB-backed embeddings + cosine search
│   └── utils/
│       ├── db.py                     # MongoDB
│       ├── logger.py
│       └── webhook.py                # HMAC-signed outbound webhooks
├── docs/
│   └── SIP_SETUP.md                  # PJSIP build + SIP setup guide
├── scripts/
│   └── sip_smoketest.py              # Register + dial + audio test (no AI)
├── examples/
│   └── python_client.py              # End-to-end client example
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## License

MIT — adapt freely for your deployment.
