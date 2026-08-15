"""
Backend factory — picks the chat/generation agent implementation at runtime.

The active backend is chosen by config: if `vllm_base_url` is set, SUNI uses the
OpenAI-compatible (vLLM) agent and Ollama is bypassed for chat; otherwise the
native Ollama agent (default, unchanged behaviour). Embeddings are NOT affected
by this switch — see project_vllm_backend memory / manager.py.
"""
from __future__ import annotations

from .. import config as _cfg
from .ollama_agent import OllamaAgent
from .openai_agent import OpenAICompatAgent


def active_backend() -> str:
    """'vllm' when a vLLM base_url is configured, else 'ollama'."""
    return "vllm" if str(_cfg.get("vllm_base_url", "") or "").strip() else "ollama"


def make_agent(
    name: str,
    model: str,
    system_prompt: str = "",
    host: str | None = None,
    backend: str | None = None,
):
    """Construct one chat agent for the given (or active) backend.
    For vLLM the model/host/key come from config (one vLLM server = one model),
    so `model`/`host` here are the Ollama-mode values and ignored under vLLM."""
    backend = backend or active_backend()
    if backend == "vllm":
        return OpenAICompatAgent(
            name=name,
            model=str(_cfg.get("vllm_model", "") or model),
            system_prompt=system_prompt,
            host=str(_cfg.get("vllm_base_url", "")).strip(),
            api_key=str(_cfg.get("vllm_api_key", "") or ""),
        )
    return OllamaAgent(
        name=name, model=model, system_prompt=system_prompt,
        host=host or _cfg.ollama_host(),
    )


def make_provider_agent(name, provider, model="", base_url="", api_key="", system_prompt=""):
    """Build a chat agent for a model-chain entry, dispatched by provider.

    Supports both auth modes:
      • key-based APIs — openai / gemini / anthropic / vllm (OpenAI-compatible or
        native Anthropic); the key is optional (falls back to the provider's env).
      • no-key subscription CLIs — claude-code (Claude Pro, machine-authenticated).
    Unknown providers fall back to a local Ollama agent (never a hard failure)."""
    provider = (provider or "ollama").strip().lower()
    if provider == "ollama":
        return OllamaAgent(name=name, model=model or "qwen2.5:7b",
                           system_prompt=system_prompt,
                           host=base_url or _cfg.ollama_host())
    if provider in ("vllm", "openai", "gemini"):
        _defaults = {
            "openai": "https://api.openai.com/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
        }
        return OpenAICompatAgent(name=name, model=model, system_prompt=system_prompt,
                                 host=(base_url or _defaults.get(provider, "")).strip(),
                                 api_key=api_key or "")
    if provider == "anthropic":
        from .claude_agent import ClaudeAgent
        return ClaudeAgent(name=name, model=model or "claude-sonnet-4-6",
                           system_prompt=system_prompt, api_key=api_key or None)
    if provider == "claude-code":
        from .claude_code_agent import ClaudeCodeAgent
        return ClaudeCodeAgent()
    if provider == "codex":
        from .codex_agent import CodexAgent
        return CodexAgent(model=model or "", api_key=api_key or "")
    return OllamaAgent(name=name, model=model or "qwen2.5:7b",
                       system_prompt=system_prompt,
                       host=base_url or _cfg.ollama_host())
