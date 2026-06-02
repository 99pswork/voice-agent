# Voice Calling Agent

AI-powered outbound voice agent that integrates with your existing **Asterisk / IPPBX / SIP / WebRTC** infrastructure. The agent dials your customers, holds a natural conversation using an LLM grounded on documents you upload, and hands off to a human when needed.

---

## Table of Contents

1. [What this service does](#what-this-service-does)
2. [Architecture](#architecture)
3. [Prerequisites](#prerequisites)
4. [Installation](#installation)
5. [Configuration](#configuration)
6. [Asterisk Integration](#asterisk-integration)
7. [API Reference](#api-reference)
8. [End-to-end walkthrough](#end-to-end-walkthrough)
9. [Operational notes](#operational-notes)
10. [Troubleshooting](#troubleshooting)
11. [Security](#security)

---

## What this service does

This service runs **alongside your existing Asterisk** and uses the **Asterisk REST Interface (ARI)** to:

- Originate outbound calls through your existing SIP trunks
- Bridge the customer's audio into a streaming AI loop (STT → LLM → TTS)
- Maintain natural, interruptible conversation
- Use a knowledge base (PDF / DOCX / URL) for accurate, grounded answers
- Transfer to a human agent on request
- Save transcripts and fire webhooks back to your CRM

It does **not replace** your IPPBX, dialplan, or WebRTC frontend — it plugs into them. Your existing inbound flows, queues, IVR, and softphones continue to work unchanged.

## Architecture

```
                      ┌────────────────────────────────────┐
                      │   YOUR EXISTING SYSTEM             │
                      │   (Asterisk + IPPBX + WebRTC)      │
                      │                                    │
   PSTN  ◄──SIP──►   │   PJSIP Trunks                     │
                      │   Dialplan / Queues / IVR          │
                      │                                    │
                      └──────────┬─────────────────────────┘
                                 │ ARI (HTTP + WS) on :8088
                                 │ + RTP externalMedia (UDP)
                                 ▼
        ┌───────────────────────────────────────────────────┐
        │   VOICE CALLING AGENT (this service)              │
        │                                                   │
        │   FastAPI ──► CallManager ──► CallSession         │
        │                                  │                │
        │                                  ▼                │
        │   STT (Deepgram) ──► ConversationEngine           │
        │                       │   (LLM + RAG)             │
        │                       ▼                           │
        │   TTS (ElevenLabs) ──► RTP back to Asterisk       │
        │                                                   │
        │   MongoDB (agents, calls, transcripts)            │
        │   Qdrant (knowledge base embeddings)              │
        └───────────────────────────────────────────────────┘
                                 ▲
                                 │ REST API
                                 │
                      ┌──────────┴──────────┐
                      │  Your CRM / Backend │
                      └─────────────────────┘
```

**Key idea:** Asterisk ARI's `externalMedia` channel exposes raw RTP audio (`slin16` = 16-bit PCM @ 16 kHz) to a UDP socket on this service. We decode the inbound audio, feed it into a streaming STT, run the LLM with knowledge base retrieval, synthesize the reply, and send PCM back via RTP. Asterisk handles the actual SIP signalling and PSTN connectivity through your existing trunks.

---

## Prerequisites

You already have, per your message:

- ✅ Asterisk with IPPBX and dialplan
- ✅ SIP trunk(s) for PSTN connectivity
- ✅ WebRTC frontend

You additionally need:

| Component | Purpose | Notes |
|-----------|---------|-------|
| **Asterisk 16+** with ARI enabled | Channel control & media bridging | Required modules: `res_ari`, `res_stasis`, `res_rtp_asterisk` |
| **MongoDB 5+** | Stores agents, calls, transcripts | Can run on the same host |
| **Qdrant 1.7+** | Vector store for KB embeddings | Or swap for Weaviate/Pinecone |
| **Python 3.11+** | Runtime | Or use the provided Docker setup |
| **OpenAI API key** | LLM + embeddings | GPT-4o-mini works well; GPT-4o for best quality |
| **Deepgram API key** | Real-time STT | Cheaper/faster than Whisper streaming |
| **ElevenLabs API key** | TTS | Or use OpenAI TTS as fallback |
| **Network reachability** | Asterisk → this service over ARI (TCP 8088) and RTP (UDP) | Same VLAN strongly recommended |

---

## Installation

### Option A — Docker (recommended)

```bash
git clone <this-repo> voice-agent
cd voice-agent
cp .env.example .env
# Edit .env with your credentials and Asterisk address
docker-compose up -d
```

Three containers come up: the agent, MongoDB, Qdrant. The agent uses `network_mode: host` so the RTP UDP ports on the host are reachable from Asterisk.

### Option B — Bare metal

```bash
git clone <this-repo> voice-agent
cd voice-agent
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env

# Start MongoDB and Qdrant separately
docker run -d -p 27017:27017 mongo:7
docker run -d -p 6333:6333 qdrant/qdrant

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
# Where to find YOUR Asterisk
ARI_URL=http://10.0.0.5:8088
ARI_USERNAME=asterisk
ARI_PASSWORD=match-the-asterisk-config
ARI_APP_NAME=voice-agent       # Stasis app name; pick anything, must match dialplan

# Default trunk (the PJSIP endpoint name in your pjsip.conf)
DEFAULT_TRUNK=my-sip-provider

# Where Asterisk should send RTP audio for AI processing
# If agent is on same host as Asterisk: 127.0.0.1
# If on a different host: this server's IP that Asterisk can reach
MEDIA_HOST=10.0.0.10
MEDIA_PORT_START=10000

# AI providers
OPENAI_API_KEY=sk-...
DEEPGRAM_API_KEY=...
ELEVENLABS_API_KEY=...
```

---

## Asterisk Integration

You need to make **three small additions** to your existing Asterisk config. None of them disturb your current dialplan.

### 1. Enable ARI

`/etc/asterisk/ari.conf`:

```ini
[general]
enabled = yes
pretty = yes
allowed_origins = *

[asterisk]
type = user
read_only = no
password = change-me-to-match-env-ARI_PASSWORD
password_format = plain
```

### 2. Enable HTTP server (ARI rides on it)

`/etc/asterisk/http.conf`:

```ini
[general]
enabled = yes
bindaddr = 0.0.0.0
bindport = 8088
```

### 3. (Optional) Add a dialplan extension to test inbound

`/etc/asterisk/extensions.conf`:

```ini
[from-internal]
exten => 9999,1,NoOp(Connecting to Voice Agent)
 same => n,Answer()
 same => n,Stasis(voice-agent,inbound_${UNIQUEID})
 same => n,Hangup()
```

Reload:

```bash
asterisk -rx "module reload res_ari.so"
asterisk -rx "module reload res_stasis.so"
asterisk -rx "dialplan reload"
asterisk -rx "ari show apps"   # should list 'voice-agent' once the service connects
```

> **Outbound calls do not require any dialplan changes.** The service uses ARI's `originate` method, which dials directly through `PJSIP/<number>@<trunk>` and drops the answered channel into the Stasis app — bypassing the dialplan entirely.

### Network / firewall

| Direction | Port | Protocol | Purpose |
|-----------|------|----------|---------|
| Agent → Asterisk | 8088 | TCP | ARI HTTP + WebSocket |
| Asterisk → Agent | 10000-10500 | UDP | RTP externalMedia audio |

The full snippet file is at `config/asterisk-snippets.conf`.

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
    "trunk": "my-sip-provider",
    "variables": {"customer_name": "Rahul"}
  }'
# → {"call_id": "call_xyz", "status": "initiated", ...}
```

### Step 5 — Watch what happens

```bash
# Server logs
docker logs -f voice-agent

# Asterisk console
asterisk -rvvv

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

A single instance comfortably handles ~50 concurrent calls on a 4-core / 8 GB box. Scale horizontally by running multiple instances behind a load-balanced ARI app — Asterisk's `Stasis` will distribute newly-arriving channels across them.

### Cost (rough, USD)

For a typical 3-minute call:
- STT (Deepgram Nova-2): ~$0.013
- LLM (GPT-4o-mini, ~3k tokens): ~$0.001
- TTS (ElevenLabs Turbo, ~600 chars): ~$0.05
- **Total: ~$0.07 per 3-min call** (excluding your existing PSTN/trunk charges)

### Recording

Add an ARI recording call to `CallSession.start()` if you need recordings:

```python
await self.ari.post(f"/channels/{self.channel_id}/record", params={
    "name": f"call-{self.call_id}",
    "format": "wav",
    "maxDurationSeconds": self.config.max_call_duration,
})
```

Recordings land in `/var/spool/asterisk/recording/` by default.

---

## Troubleshooting

### "Connected to ARI but no calls go through"

Check that the Stasis app shows up:

```bash
asterisk -rx "ari show apps"
# Should list: voice-agent
```

If it doesn't, the agent failed to subscribe. Check the agent logs for the WebSocket connection error and verify `ARI_PASSWORD` matches `/etc/asterisk/ari.conf`.

### "Call connects but no audio / one-way audio"

This is almost always RTP routing:

1. Confirm `MEDIA_HOST` in `.env` is reachable from Asterisk
2. Confirm UDP ports 10000-10500 (or whatever range you set) are open between Asterisk and the agent
3. If running in Docker, the agent container **must** use `network_mode: host` (or you must publish the UDP range)
4. Check Asterisk logs: `pjsip set logger on` then `rtp set debug on`

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

### "ARI events not arriving"

Check that the WS connection stays open: `netstat -an | grep 8088`. The client auto-reconnects on drop, but if your Asterisk is behind a proxy that closes idle sockets, configure ARI keepalive in `ari.conf`:

```ini
[general]
websocket_write_timeout = 300
```

---

## Security

- **Never expose port 8000 to the public internet.** Put it behind your existing reverse proxy with auth.
- **Restrict ARI access** by binding `bindaddr` in `http.conf` to an internal interface and using firewall rules.
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
│   │   ├── ari_client.py             # Asterisk ARI WS client
│   │   ├── call_manager.py           # Channel/bridge orchestration
│   │   ├── call_session.py           # Per-call audio loop
│   │   ├── rtp_handler.py            # UDP RTP I/O
│   │   └── dialer.py                 # Bulk campaign engine
│   ├── kb/
│   │   ├── document_processor.py     # PDF/DOCX/HTML extraction
│   │   └── vector_store.py           # Qdrant + embeddings
│   └── utils/
│       ├── db.py                     # MongoDB
│       ├── logger.py
│       └── webhook.py                # HMAC-signed outbound webhooks
├── config/
│   └── asterisk-snippets.conf        # Drop-in Asterisk config
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
