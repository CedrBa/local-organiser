"""Small OpenAI-compatible client used only to obtain validated proposals."""

from typing import Protocol

import httpx

from organiser.core.config import Settings


class InterpreterClientError(Exception):
    pass


class InterpreterClient(Protocol):
    """Replaceable legacy interpretation client; return raw text, never execute actions."""

    model_name: str

    def interpret(self, prompt: str) -> str:
        """Return raw model output for legacy inbox validation; perform no domain mutation."""
        ...


class PlannerClient(Protocol):
    """Replaceable planning client with an explicit model identifier."""

    model_name: str

    def plan(self, prompt: str) -> str:
        """Return raw planning output for caller validation; perform no domain mutation."""
        ...


class ContextProposalClient(Protocol):
    """Replaceable context proposal client; no implicit database or file access."""

    model_name: str

    def propose_context(self, prompt: str) -> str:
        """Return raw context-action output; the caller validates, persists and reviews it."""
        ...


class OpenAICompatibleInterpreterClient:
    """HTTP adapter for model output with configured endpoint, model and timeout.

    It sends supplied prompts to that endpoint, but cannot apply their actions.
    The configured endpoint may be local or remote; callers control data selection."""

    def __init__(self, settings: Settings):
        if not settings.llm_base_url or not settings.llm_model:
            raise InterpreterClientError(
                "Lokaler LLM-Endpunkt und Modell muessen konfiguriert sein"
            )
        self._base_url = settings.llm_base_url.rstrip("/")
        self.model_name = settings.llm_model
        self._timeout_seconds = settings.llm_timeout_seconds

    def interpret(self, prompt: str) -> str:
        """Return raw model output for legacy inbox validation; perform no domain mutation."""
        return self._complete(
            prompt,
            system_prompt="Du bist ein strikt strukturierter Organiser-Interpreter.",
            max_tokens=400,
        )

    def plan(self, prompt: str) -> str:
        """Return raw planning output for caller validation; perform no domain mutation."""
        return self._complete(
            prompt,
            system_prompt="Du bist ein strikt strukturierter Organiser-Planner. Du schreibst niemals Datenbankbefehle.",
            max_tokens=1200,
        )

    def propose_context(self, prompt: str) -> str:
        """Return raw context-action output; the caller validates, persists and reviews it."""
        return self._complete(
            prompt,
            system_prompt=(
                "Du bist ein strikt strukturierter Organiser-Vorschlagsgenerator fuer Kontext-Aktionen. "
                "Du schreibst niemals Datenbankbefehle oder Pfade."
            ),
            max_tokens=1200,
        )

    def _complete(self, prompt: str, *, system_prompt: str, max_tokens: int) -> str:
        try:
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                json={
                    "model": self.model_name,
                    "temperature": 0,
                    "max_tokens": max_tokens,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
            raise InterpreterClientError(f"Lokaler Modellaufruf fehlgeschlagen: {error}") from error
        if not isinstance(content, str) or not content.strip():
            raise InterpreterClientError("Lokales Modell lieferte keinen Textinhalt")
        return content
