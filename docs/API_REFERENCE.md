# Voice Agent — API Reference (for the UI / frontend)

Base URL: `http://<host>:8000/api/v1`
All request/response bodies are JSON unless noted (file upload is multipart).

This backend already supports the full multi-agent workflow:
**many agents, each with its own prompt, voice, and linked knowledge bases.**
The UI just needs to call these endpoints. Data persists in MongoDB.

Interactive docs (auto-generated, try requests live): `http://<host>:8000/docs`

---

## 1. Agents

An agent = a persona: a prompt (`base_instructions`), a voice, STT/TTS choice,
and zero or more linked knowledge bases.

### Options for the create/edit form
`GET /agents/options` — returns dropdown data for the UI: valid `stt_providers`,
`tts_providers`, `llm_models`, `languages`, OpenAI voice names, and the live
list of **ElevenLabs voices** (`{id, name, category}`). Use this to build the
voice picker instead of hardcoding IDs.

### Create agent
`POST /agents`
```json
{
  "name": "Sales Bot",
  "base_instructions": "You are Riya, a friendly sales agent. Use the knowledge base for pricing.",
  "initial_message": "Hi! I can help with our plans.",
  "knowledge_base_ids": ["kb_abc123"],
  "voice": "EXAVITQu4vr4xnSDxMaL",
  "stt_provider": "deepgram",
  "tts_provider": "elevenlabs",
  "language": "en-US",
  "max_call_duration": 600,
  "interruption_enabled": true,
  "transfer_number": null,
  "webhook_url": null
}
```
Returns the created agent with its generated `id` (e.g. `agent_xxx`).
Only `name` and `base_instructions` are required; the rest have sensible defaults.

### List agents
`GET /agents?limit=50&offset=0` → array of agents.

### Get one agent
`GET /agents/{agent_id}`

### Update agent (edit prompt, voice, linked KBs, etc.)
`PATCH /agents/{agent_id}` — send only the fields you want to change:
```json
{ "base_instructions": "New prompt text...", "knowledge_base_ids": ["kb_abc123","kb_def456"] }
```
This is the endpoint behind the UI's "edit prompt" / "configure agent".

### Delete agent
`DELETE /agents/{agent_id}` → 204

### Test an agent (no phone call)
`POST /agents/{agent_id}/test?message=How%20much%20is%20premium%3F`
Returns the agent's text reply (runs the full LLM + RAG, just no audio). Great
for a "test in chat" button in the UI before placing real calls.

---

## 2. Knowledge Bases (RAG)

A knowledge base holds documents that an agent can pull answers from. Link a KB
to an agent via the agent's `knowledge_base_ids`.

### Create KB
`POST /knowledge-base`
```json
{ "name": "pricing_faq", "description": "Plan pricing", "chunk_size": 800, "chunk_overlap": 100 }
```
Returns the KB with its `id` (e.g. `kb_xxx`).

### Add data — upload files
`POST /knowledge-base/{kb_id}/upload` — multipart form, field name `files`
(one or more). Supported: **PDF, DOCX, TXT, HTML**.
```
curl -F "files=@pricing.pdf" -F "files=@faq.docx" .../knowledge-base/kb_xxx/upload
```
Files are embedded in the background; the response returns docs with
`status: "processing"`. Poll `GET /knowledge-base/{kb_id}/documents` for status.

### Add data — from a URL
`POST /knowledge-base/{kb_id}/url?url=https://example.com/pricing`
Fetches the page, extracts text, embeds it.

### List documents in a KB
`GET /knowledge-base/{kb_id}/documents`

### Delete a document / whole KB
`DELETE /knowledge-base/{kb_id}/documents/{doc_id}` → 204
`DELETE /knowledge-base/{kb_id}` → 204 (also removes its vectors)

### Search a KB (debug/preview retrieval)
`POST /knowledge-base/{kb_id}/search?query=how%20much%20is%20premium&top_k=5`
Returns the top matching chunks with similarity scores — useful for a UI
"preview what the agent will retrieve" feature.

---

## 3. Calls

This is the "enter a phone number, pick an agent, hit Call" flow.

### Place an outbound AI call
`POST /calls/outbound`
```json
{ "agent_id": "agent_xxx", "destination": "+917791027690",
  "variables": { "customer_name": "Rahul" },
  "metadata": { "source": "ui", "campaign": "june-promo" } }
```
- `destination` = the phone number (E.164).
- `variables` fill `{placeholders}` in the agent's prompt/initial_message.
- `metadata` = any free-form fields you want stored on the record (e.g. who
  initiated it, a CRM lead id).

Returns the created call record (status `initiated`). **Every attempt is
persisted** — even if dialing fails, the record is stored with `status: "failed"`
and an `error`, so failed calls still appear in history.

### Bulk calls (campaign)
`POST /calls/bulk?agent_id=agent_xxx&rate_per_second=5`
Body: `[{ "destination": "+91...", "variables": {...} }, ...]`

### Conversation history & transcripts (requires MongoDB)
Every call is stored in MongoDB (`voice_agent.calls`). The record holds:
```json
{
  "call_id": "call_xxx",
  "agent_id": "agent_xxx",
  "agent_name": "Sales Bot",          // denormalized for list views
  "destination": "+917791027690",      // the number called
  "direction": "outbound",
  "status": "completed",               // initiated | failed | completed
  "started_at": "...", "ended_at": "...",
  "duration_seconds": 73,
  "turn_count": 8,
  "outcome": "user_ended",
  "transcript": [
    { "role": "assistant", "content": "Hi! ...", "at": "2026-..." },
    { "role": "user",      "content": "I can hear you", "at": "2026-..." }
  ],
  "variables": {...}, "metadata": {...}
}
```
Endpoints:
- `GET /calls?agent_id=...&status=...&limit=100` — list (newest first) for a
  history table. Filter by agent or status.
- `GET /calls/{call_id}` — one call with its full timestamped transcript.
- `POST /calls/{call_id}/hangup` — end an in-progress call.
- `POST /calls/{call_id}/transfer?destination=+91...` — transfer to a human.

---

## 4. Webhooks (post-call → your CRM)

Set `webhook_url` on an agent and the service POSTs the transcript + outcome
when each call completes:
```json
{ "event": "call.completed", "call_id": "...", "duration_seconds": 73,
  "transcript": [{ "role": "assistant", "content": "..." }, ...], "outcome": "user_ended" }
```

There's also an inbound trigger: `POST /webhooks/trigger-call` (same body as
`/calls/outbound`) so your CRM can start a call.

---

## Typical UI flows

**Create a new agent with a knowledge base:**
1. `POST /knowledge-base` → get `kb_id`
2. `POST /knowledge-base/{kb_id}/upload` (the docs the UI user provides)
3. `POST /agents` with `knowledge_base_ids: [kb_id]` and the prompt
4. `POST /agents/{id}/test?message=...` to sanity-check before going live
5. `POST /calls/outbound` to call

**Edit an existing agent's prompt or knowledge:**
- `PATCH /agents/{id}` with the new `base_instructions` and/or `knowledge_base_ids`
- add more KB data anytime via `POST /knowledge-base/{kb_id}/upload`

---

## Notes / current limits

- **Persistence**: agents, KBs, embeddings, calls all live in MongoDB
  (`voice_agent` database). Without `MONGO_URL` the app falls back to file-based
  agents and skips history.
- **Vector store**: embeddings are stored in Mongo (`kb_vectors`) and searched
  with cosine similarity in-process — no separate vector DB. Great for
  per-agent KBs; revisit for very large corpora.
- **Telephony**: one SIP registration (extension) at a time; concurrent calls
  share it. For high concurrency, run multiple instances / extensions.
- **CORS**: set `CORS_ORIGINS` in `.env` to your UI's origin(s) for browser calls.
