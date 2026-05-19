from typing import Optional, Sequence

__version__: str

class User:
    id: str
    def say(self, target: str, text: str) -> None: ...

class Agent:
    id: str
    def say(self, target: str, text: str) -> None: ...

class World:
    def __init__(
        self,
        name: Optional[str] = None,
        backend: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None: ...
    @property
    def name(self) -> str: ...
    @property
    def backend(self) -> str: ...
    def actor_count(self) -> int: ...
    def spawn_user(
        self,
        id: Optional[str] = None,
        persona: Optional[str] = None,
        hidden_goal: Optional[str] = None,
        model: str = "user-model",
    ) -> User: ...
    def spawn_agent(
        self,
        id: Optional[str] = None,
        model: str = "claude-sonnet-4-5",
        tools: Optional[Sequence[str]] = None,
    ) -> Agent: ...
    def _mock_say(self, model: str, text: str) -> None: ...
    def _mock_tool(self, model: str, tool: str, args_json: str) -> None: ...
