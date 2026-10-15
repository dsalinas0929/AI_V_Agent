"""Server-side model selection. Client access tokens are unrelated to provider keys."""

import os
from dataclasses import dataclass, field

from agent_v.saygm import DEFAULT_MODEL


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    api_key: str = field(default="", repr=False)


def model_config() -> ModelConfig:
    if os.getenv("AGENT_V_DEMO") == "1":
        return ModelConfig("demo", "demo")
    provider = os.getenv("AGENT_V_PROVIDER", "saygm").strip().lower()
    if provider == "saygm":
        key_name, default_model = "GM_API_KEY", DEFAULT_MODEL
    elif provider == "gemini":
        key_name, default_model = "GEMINI_API_KEY", "gemini-3.8-flash"
    else:
        raise ValueError("AGENT_V_PROVIDER must be saygm or gemini (or use serve --demo).")
    key = os.getenv(key_name, "").strip()
    if not key:
        raise ValueError(f"Set {key_name} on the server for {provider}, or use serve --demo.")
    model = os.getenv("AGENT_V_MODEL", default_model).strip()
    if not model:
        raise ValueError("AGENT_V_MODEL must not be empty.")
    return ModelConfig(provider, model, key)


def create_model():
    from agent_v.model import DemoModel, GeminiModel
    from agent_v.saygm import SayGMModel

    config = model_config()
    if config.provider == "demo":
        return DemoModel()
    if config.provider == "saygm":
        return SayGMModel(config.api_key, config.model)
    return GeminiModel(config.api_key, config.model)
