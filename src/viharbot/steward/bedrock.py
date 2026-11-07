"""The model client: Amazon Bedrock's Converse API, authorized by the runtime's IAM role alone.

There is no API key. The role may invoke exactly one model (see the runtime policy), and this client
refuses to be built for anything outside the approved family. It sends no tools, sets a small output
cap and low temperature, and never logs or returns anything but the model's text. The client is
injected, so the tests run against a fake and no model is ever called by a test.
"""

from __future__ import annotations

from typing import Any

from .config import APPROVED_MODEL_IDS

MAX_RESPONSE_CHARS = 2_000  # more than any valid opening; a runaway reply is cut before validation


class ModelUnavailable(RuntimeError):
    """The model could not be used. Fixed wording: no prompt, reply or ARN."""


class BedrockOpeningModel:
    def __init__(self, client: Any, model_id: str, max_tokens: int = 150,
                 temperature: float = 0.3) -> None:
        if model_id not in APPROVED_MODEL_IDS:
            raise ValueError("model is not on the approved allowlist (ADR-011)")
        if not 20 <= max_tokens <= 400 or not 0.0 <= temperature <= 1.0:
            raise ValueError("token cap or temperature out of range")
        self._client, self._model_id = client, model_id
        self._max_tokens, self._temperature = max_tokens, temperature

    @property
    def model_id(self) -> str:
        return self._model_id

    def complete(self, system: str, user: str) -> str:
        try:
            response = self._client.converse(
                modelId=self._model_id,
                system=[{"text": system}],
                messages=[{"role": "user", "content": [{"text": user}]}],
                inferenceConfig={"maxTokens": self._max_tokens, "temperature": self._temperature,
                                 "topP": 0.9},
            )
            blocks = response["output"]["message"]["content"]
            text = "".join(b["text"] for b in blocks if isinstance(b, dict) and "text" in b)
        except Exception:  # throttling, access denied, a malformed reply: all the same to the caller
            raise ModelUnavailable("the model could not be used") from None
        if not text.strip():
            raise ModelUnavailable("the model returned nothing")
        return text[:MAX_RESPONSE_CHARS]
