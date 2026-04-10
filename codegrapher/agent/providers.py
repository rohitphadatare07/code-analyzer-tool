"""
LLM provider factory.

Supports:
  - anthropic   : Claude models via Anthropic API
  - openai      : GPT models via OpenAI API
  - bedrock     : Any Bedrock model (Claude, Nova, Llama, Mistral, Titan)
  - ollama      : Local models via Ollama (OpenAI-compatible endpoint)
  - gemini      : Google Gemini via langchain-google-genai
  - custom      : Any OpenAI-compatible endpoint — pass api_url + api_key + model_name

All providers return a LangChain BaseChatModel so LangGraph nodes
can call them identically with .invoke() / .bind_tools().
"""
from __future__ import annotations

import os
from typing import Optional
from langchain_core.language_models import BaseChatModel


# ── Bedrock model families ─────────────────────────────────────────────────────
# Maps model-id prefix → which Bedrock wrapper + invocation style to use.
BEDROCK_MODELS = {
    # Claude on Bedrock
    "anthropic.claude": "claude",
    # Amazon Nova
    "amazon.nova": "converse",
    # Meta Llama
    "meta.llama": "converse",
    # Mistral
    "mistral.": "converse",
    # Amazon Titan
    "amazon.titan": "titan",
}


def _bedrock_llm(model_id: str, region: str) -> BaseChatModel:
    """Return a LangChain chat model for an AWS Bedrock model."""
    try:
        from langchain_aws import ChatBedrock
    except ImportError:
        raise ImportError(
            "langchain-aws and boto3 are required for Bedrock.\n"
            "Install: pip install langchain-aws boto3"
        )

    return ChatBedrock(
        model_id=model_id,
        region_name=region,
        model_kwargs={"temperature": 0},
    )


def create_llm(
    provider: str,
    model: Optional[str] = None,
    # Bedrock
    aws_region: Optional[str] = None,
    # Custom / Ollama
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
    # Shared
    temperature: float = 0,
) -> BaseChatModel:
    """
    Create a LangChain BaseChatModel for the given provider.

    Parameters
    ----------
    provider : str
        One of: anthropic | openai | bedrock | ollama | gemini | custom
    model : str, optional
        Model name/ID. Defaults to a sensible default per provider.
    aws_region : str, optional
        AWS region for Bedrock (default: AWS_REGION env var or us-east-1).
    api_url : str, optional
        Base URL for custom / ollama providers.
    api_key : str, optional
        API key for custom provider (falls back to env vars).
    temperature : float
        Sampling temperature (default 0 for deterministic analysis).

    Returns
    -------
    BaseChatModel
        A LangChain-compatible chat model ready to use with LangGraph.
    """
    provider = provider.lower().strip()

    # ── Anthropic ──────────────────────────────────────────────────────────────
    if provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError:
            raise ImportError("pip install langchain-anthropic")

        return ChatAnthropic(
            model=model or "claude-sonnet-4-20250514",
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY", ""),
            temperature=temperature,
        )

    # ── OpenAI ────────────────────────────────────────────────────────────────
    elif provider == "openai":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            raise ImportError("pip install langchain-openai")

        return ChatOpenAI(
            model=model or "gpt-4o",
            api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
            temperature=temperature,
            base_url=api_url,  # None = default OpenAI URL
        )

    # ── AWS Bedrock ───────────────────────────────────────────────────────────
    elif provider == "bedrock":
        region = aws_region or os.environ.get("AWS_REGION", "us-east-1")
        model_id = model or "anthropic.claude-3-5-sonnet-20241022-v2:0"
        return _bedrock_llm(model_id, region)

    # ── Ollama (local) ────────────────────────────────────────────────────────
    elif provider == "ollama":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            raise ImportError("pip install langchain-openai")

        base = api_url or "http://localhost:11434"
        return ChatOpenAI(
            model=model or "llama3.2",
            base_url=f"{base.rstrip('/')}/v1",
            api_key="ollama",          # Ollama ignores the key
            temperature=temperature,
        )

    # ── Google Gemini ─────────────────────────────────────────────────────────
    elif provider == "gemini":
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError:
            raise ImportError("pip install langchain-google-genai")

        return ChatGoogleGenerativeAI(
            model=model or "gemini-2.0-flash",
            google_api_key=api_key or os.environ.get("GEMINI_API_KEY", ""),
            temperature=temperature,
        )

    # ── Custom (any OpenAI-compatible endpoint) ───────────────────────────────
    elif provider == "custom":
        if not api_url:
            raise ValueError(
                "--api-url is required for custom provider.\n"
                "Example: --api-url https://my-llm.internal/v1"
            )
        if not model:
            raise ValueError("--model is required for custom provider.")

        try:
            from langchain_openai import ChatOpenAI
        except ImportError:
            raise ImportError("pip install langchain-openai")

        key = api_key or os.environ.get("CUSTOM_API_KEY", "none")
        return ChatOpenAI(
            model=model,
            base_url=api_url,
            api_key=key,
            temperature=temperature,
        )

    else:
        raise ValueError(
            f"Unknown provider '{provider}'.\n"
            "Choose from: anthropic | openai | bedrock | ollama | gemini | custom"
        )


def provider_display_name(
    provider: str,
    model: Optional[str],
    api_url: Optional[str] = None,
) -> str:
    """Return a human-readable string for the provider+model."""
    defaults = {
        "anthropic": "claude-sonnet-4-20250514",
        "openai": "gpt-4o",
        "bedrock": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "ollama": "llama3.2",
        "gemini": "gemini-2.0-flash",
        "custom": model or "unknown",
    }
    m = model or defaults.get(provider, "unknown")
    if provider == "custom" and api_url:
        return f"custom/{m} @ {api_url}"
    return f"{provider}/{m}"
