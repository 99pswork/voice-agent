"""Voice agent configuration container."""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class VoiceAgentConfig:
    id: str
    name: str
    base_instructions: str
    voice: str = "alloy"
    language: str = "en-US"
    knowledge_base_ids: List[str] = field(default_factory=list)
    llm_model: str = "gpt-4o-mini"
    stt_provider: str = "whisper"
    tts_provider: str = "openai"
    max_call_duration: int = 600
    interruption_enabled: bool = True
    initial_message: Optional[str] = None
    end_call_phrases: List[str] = field(default_factory=list)
    transfer_number: Optional[str] = None
    webhook_url: Optional[str] = None

    def __post_init__(self):
        if not self.end_call_phrases:
            self.end_call_phrases = ["goodbye", "bye", "hang up", "end call"]
