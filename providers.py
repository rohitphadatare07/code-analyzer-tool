"""LLM provider abstraction - supports Anthropic, OpenAI, AWS Bedrock, Ollama, Gemini."""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Any


class LLMProvider:
    """Base class for LLM providers."""

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> str:
        raise NotImplementedError

    def name(self) -> str:
        raise NotImplementedError


# ── Anthropic ─────────────────────────────────────────────────────────────────

class AnthropicProvider(LLMProvider):
    def __init__(self, model: str, api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable not set")

    def name(self) -> str:
        return f"anthropic/{self.model}"

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> str:
        payload = json.dumps({
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return data["content"][0]["text"]


# ── OpenAI ────────────────────────────────────────────────────────────────────

class OpenAIProvider(LLMProvider):
    def __init__(self, model: str, api_key: str | None = None, base_url: str = "https://api.openai.com"):
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY environment variable not set")

    def name(self) -> str:
        return f"openai/{self.model}"

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> str:
        payload = json.dumps({
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }).encode()

        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]


# ── AWS Bedrock ───────────────────────────────────────────────────────────────

class BedrockProvider(LLMProvider):
    """AWS Bedrock provider - supports Claude, Llama, Titan, Mistral, etc."""

    def __init__(self, model: str, region: str | None = None):
        self.model = model
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        # Validate boto3 is available
        try:
            import boto3
            self._boto3 = boto3
        except ImportError:
            raise ImportError(
                "boto3 is required for Bedrock provider. Install with: pip install boto3"
            )

    def name(self) -> str:
        return f"bedrock/{self.model}"

    def _invoke_claude(self, client: Any, system: str, user: str, max_tokens: int) -> str:
        """Invoke Anthropic Claude models on Bedrock."""
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        })
        response = client.invoke_model(modelId=self.model, body=body)
        result = json.loads(response["body"].read())
        return result["content"][0]["text"]

    def _invoke_llama(self, client: Any, system: str, user: str, max_tokens: int) -> str:
        """Invoke Meta Llama models on Bedrock."""
        prompt = f"<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n{system}<|eot_id|><|start_header_id|>user<|end_header_id|>\n{user}<|eot_id|><|start_header_id|>assistant<|end_header_id|>"
        body = json.dumps({"prompt": prompt, "max_gen_len": max_tokens, "temperature": 0.3})
        response = client.invoke_model(modelId=self.model, body=body)
        result = json.loads(response["body"].read())
        return result.get("generation", "")

    def _invoke_mistral(self, client: Any, system: str, user: str, max_tokens: int) -> str:
        """Invoke Mistral models on Bedrock."""
        prompt = f"<s>[INST] {system}\n\n{user} [/INST]"
        body = json.dumps({"prompt": prompt, "max_tokens": max_tokens, "temperature": 0.3})
        response = client.invoke_model(modelId=self.model, body=body)
        result = json.loads(response["body"].read())
        return result["outputs"][0]["text"]

    def _invoke_titan(self, client: Any, system: str, user: str, max_tokens: int) -> str:
        """Invoke Amazon Titan models on Bedrock."""
        body = json.dumps({
            "inputText": f"{system}\n\n{user}",
            "textGenerationConfig": {"maxTokenCount": max_tokens, "temperature": 0.3},
        })
        response = client.invoke_model(modelId=self.model, body=body)
        result = json.loads(response["body"].read())
        return result["results"][0]["outputText"]

    def _invoke_nova(self, client: Any, system: str, user: str, max_tokens: int) -> str:
        """Invoke Amazon Nova models on Bedrock (converse API)."""
        response = client.converse(
            modelId=self.model,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": max_tokens},
        )
        return response["output"]["message"]["content"][0]["text"]

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> str:
        client = self._boto3.client("bedrock-runtime", region_name=self.region)
        model_lower = self.model.lower()

        if "anthropic" in model_lower or "claude" in model_lower:
            return self._invoke_claude(client, system, user, max_tokens)
        elif "llama" in model_lower or "meta" in model_lower:
            return self._invoke_llama(client, system, user, max_tokens)
        elif "mistral" in model_lower:
            return self._invoke_mistral(client, system, user, max_tokens)
        elif "titan" in model_lower:
            return self._invoke_titan(client, system, user, max_tokens)
        elif "nova" in model_lower or "amazon" in model_lower:
            return self._invoke_nova(client, system, user, max_tokens)
        else:
            # Default: try converse API (works for most modern Bedrock models)
            response = client.converse(
                modelId=self.model,
                system=[{"text": system}],
                messages=[{"role": "user", "content": [{"text": user}]}],
                inferenceConfig={"maxTokens": max_tokens},
            )
            return response["output"]["message"]["content"][0]["text"]


# ── Ollama ────────────────────────────────────────────────────────────────────

class OllamaProvider(LLMProvider):
    def __init__(self, model: str, base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")

    def name(self) -> str:
        return f"ollama/{self.model}"

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> str:
        payload = json.dumps({
            "model": self.model,
            "stream": False,
            "options": {"num_predict": max_tokens},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }).encode()

        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
            return data["message"]["content"]
        except urllib.error.URLError as e:
            raise ConnectionError(
                f"Cannot connect to Ollama at {self.base_url}. "
                f"Make sure Ollama is running: ollama serve\nError: {e}"
            )


# ── Google Gemini ─────────────────────────────────────────────────────────────

class GeminiProvider(LLMProvider):
    def __init__(self, model: str, api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY environment variable not set")

    def name(self) -> str:
        return f"gemini/{self.model}"

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> str:
        payload = json.dumps({
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": max_tokens},
        }).encode()

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return data["candidates"][0]["content"]["parts"][0]["text"]


# ── Factory ───────────────────────────────────────────────────────────────────

def create_provider(provider: str, model: str, **kwargs) -> LLMProvider:
    """Create an LLM provider from a provider name and model string."""
    if provider == "anthropic":
        return AnthropicProvider(model=model)
    elif provider == "openai":
        return OpenAIProvider(model=model)
    elif provider == "bedrock":
        return BedrockProvider(model=model, region=kwargs.get("region"))
    elif provider == "ollama":
        return OllamaProvider(model=model, base_url=kwargs.get("ollama_url", "http://localhost:11434"))
    elif provider == "gemini":
        return GeminiProvider(model=model)
    else:
        raise ValueError(f"Unknown provider: {provider}. Choose from: anthropic, openai, bedrock, ollama, gemini")
