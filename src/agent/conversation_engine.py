"""
Conversation Engine - Combines LLM, RAG retrieval, and instruction templating.
This is the brain of the voice agent.
"""
import os
import logging
from typing import List, Dict, Optional, AsyncGenerator
from openai import AsyncOpenAI

from agent.voice_agent_config import VoiceAgentConfig
from kb.vector_store import VectorStoreManager

logger = logging.getLogger(__name__)


class ConversationEngine:
    """
    Manages a single live conversation:
      1. Receives transcribed user text
      2. Retrieves relevant KB chunks (RAG)
      3. Builds a context-aware prompt from base_instructions
      4. Streams LLM tokens to TTS
      5. Tracks conversation history
    """

    def __init__(self, config: VoiceAgentConfig, variables: Optional[Dict] = None):
        self.config = config
        self.variables = variables or {}
        self.history: List[Dict[str, str]] = []
        self.llm = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.vector_store = VectorStoreManager.instance()

    def _render_template(self, text: str) -> str:
        """Replace {variable} placeholders with values."""
        for key, value in self.variables.items():
            text = text.replace(f"{{{key}}}", str(value))
        return text

    async def _retrieve_context(self, user_query: str) -> str:
        """RAG: pull top chunks from all attached knowledge bases."""
        if not self.config.knowledge_base_ids:
            return ""

        all_chunks = []
        for kb_id in self.config.knowledge_base_ids:
            try:
                results = await self.vector_store.search(kb_id, user_query, top_k=4)
                all_chunks.extend(results)
            except Exception as e:
                logger.warning(f"KB search failed for {kb_id}: {e}")

        # Sort by score and take top 6 across all KBs
        all_chunks.sort(key=lambda x: x.get("score", 0), reverse=True)
        top = all_chunks[:6]

        if not top:
            return ""

        context_parts = []
        for i, chunk in enumerate(top, 1):
            context_parts.append(f"[Source {i}]\n{chunk['text']}")
        return "\n\n".join(context_parts)

    def _build_system_prompt(self, retrieved_context: str) -> str:
        base = self._render_template(self.config.base_instructions)

        guard_rails = """
You are a voice agent — your replies will be spoken aloud. Follow these rules strictly:
- Keep responses short and conversational (1-3 sentences typically).
- Use natural spoken language. No bullet points, markdown, or special formatting.
- Spell out abbreviations and numbers naturally (say "twenty dollars" not "$20").
- If you don't know something, say so honestly. Do not fabricate information.
- If the user wants to end the call, politely confirm and say goodbye.
- If the user asks to speak to a human, acknowledge and trigger a transfer.
- Do not reveal that you are an AI unless directly and sincerely asked.
"""

        kb_section = ""
        if retrieved_context:
            kb_section = f"""

# Relevant Knowledge Base Information
Use the following information to answer questions accurately. If the user's question is not covered here, say you'll need to follow up.

{retrieved_context}
"""

        return f"{base}\n\n{guard_rails}{kb_section}"

    async def generate_response(
        self, user_message: str, history: Optional[List[Dict]] = None
    ) -> str:
        """Non-streaming reply (used for testing endpoint)."""
        history = history if history is not None else self.history
        retrieved = await self._retrieve_context(user_message)
        system_prompt = self._build_system_prompt(retrieved)

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        completion = await self.llm.chat.completions.create(
            model=self.config.llm_model,
            messages=messages,
            temperature=0.7,
            max_tokens=200,
        )
        reply = completion.choices[0].message.content.strip()

        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": reply})
        return reply

    async def stream_response(
        self, user_message: str
    ) -> AsyncGenerator[str, None]:
        """
        Stream LLM tokens. Caller (CallSession) feeds them sentence-by-sentence
        into TTS to keep latency low.
        """
        retrieved = await self._retrieve_context(user_message)
        system_prompt = self._build_system_prompt(retrieved)

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(self.history)
        messages.append({"role": "user", "content": user_message})

        full_reply = ""
        stream = await self.llm.chat.completions.create(
            model=self.config.llm_model,
            messages=messages,
            temperature=0.7,
            max_tokens=200,
            stream=True,
        )

        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                full_reply += delta
                yield delta

        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": full_reply.strip()})

    def should_end_call(self, user_message: str) -> bool:
        msg = user_message.lower().strip()
        return any(phrase in msg for phrase in self.config.end_call_phrases)

    def should_transfer(self, user_message: str) -> bool:
        triggers = ["speak to human", "talk to agent", "real person", "transfer me", "manager"]
        return any(t in user_message.lower() for t in triggers)

    def get_initial_message(self) -> Optional[str]:
        if not self.config.initial_message:
            return None
        return self._render_template(self.config.initial_message)

    def get_transcript(self) -> List[Dict[str, str]]:
        return self.history.copy()
