"""Record types shared by adapters, store and aggregation."""
from dataclasses import dataclass


@dataclass
class UsageRecord:
    platform: str
    agent_kind: str
    agent_id: str
    session_id: str
    model: str
    effort_level: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    timestamp: float
    requests: int = 1

    @property
    def total_tokens(self):
        return (self.input_tokens + self.output_tokens
                + self.cache_read_tokens + self.cache_write_tokens)


@dataclass
class SubagentRun:
    platform: str
    agent_id: str
    agent_type: str
    agent_name: str
    session_id: str
    model: str
    effort_level: str
    t: float
    status: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
