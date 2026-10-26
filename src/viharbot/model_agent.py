"""Build Strands agents that route exclusively through the LiteLLM proxy."""

from strands import Agent
from strands.models.litellm import LiteLLMModel

from .litellm_client import LiteLLMError, litellm_credentials


def build_agent(*, model_alias: str, system_prompt: str) -> Agent:
    if not model_alias.strip():
        raise LiteLLMError("model alias is required")
    api_base, api_key = litellm_credentials()
    model = LiteLLMModel(
        model_id=model_alias,
        client_args={
            "api_base": api_base,
            "api_key": api_key,
            "use_litellm_proxy": True,
        },
    )
    return Agent(model=model, system_prompt=system_prompt, callback_handler=None)
