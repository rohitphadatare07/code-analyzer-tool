"""
LLM providers with tool-use / function-calling support.

The key difference from v1: providers now support complete_with_tools(),
which allows the LLM to call tools and receive results in a multi-turn loop.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any


class LLMProvider:
    def complete_with_tools(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 4096,
    ) -> dict:
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

    def complete_with_tools(self, system, messages, tools, max_tokens=4096) -> dict:
        payload = json.dumps({
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "tools": tools,
            "messages": messages,
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
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())


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

    def _anthropic_to_openai_tools(self, tools: list[dict]) -> list[dict]:
        """Convert Anthropic tool schema to OpenAI function schema."""
        return [{
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            }
        } for t in tools]

    def _convert_messages(self, messages: list[dict]) -> list[dict]:
        """Convert Anthropic message format to OpenAI format."""
        converted = []
        for msg in messages:
            role = msg["role"]
            content = msg["content"]

            if isinstance(content, str):
                converted.append({"role": role, "content": content})
            elif isinstance(content, list):
                # Handle tool_use and tool_result blocks
                for block in content:
                    if block.get("type") == "text":
                        converted.append({"role": role, "content": block["text"]})
                    elif block.get("type") == "tool_use":
                        converted.append({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": block["id"],
                                "type": "function",
                                "function": {
                                    "name": block["name"],
                                    "arguments": json.dumps(block.get("input", {})),
                                }
                            }]
                        })
                    elif block.get("type") == "tool_result":
                        converted.append({
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": block.get("content", ""),
                        })
        return converted

    def complete_with_tools(self, system, messages, tools, max_tokens=4096) -> dict:
        oai_messages = [{"role": "system", "content": system}] + self._convert_messages(messages)
        oai_tools = self._anthropic_to_openai_tools(tools)

        payload = json.dumps({
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": oai_messages,
            "tools": oai_tools,
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
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())

        # Convert OpenAI response → Anthropic format
        choice = data["choices"][0]["message"]
        content = []
        if choice.get("content"):
            content.append({"type": "text", "text": choice["content"]})
        for tc in choice.get("tool_calls", []):
            content.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["function"]["name"],
                "input": json.loads(tc["function"]["arguments"]),
            })
        return {
            "content": content,
            "usage": {
                "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
            }
        }


# ── AWS Bedrock ───────────────────────────────────────────────────────────────

class BedrockProvider(LLMProvider):
    """AWS Bedrock with tool use via the Converse API (supports Claude, Nova, Llama 3.1+)."""

    def __init__(self, model: str, region: str | None = None):
        self.model = model
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        try:
            import boto3
            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        except ImportError:
            raise ImportError("boto3 required for Bedrock: pip install boto3")

    def name(self) -> str:
        return f"bedrock/{self.model}"

    def _anthropic_tools_to_bedrock(self, tools: list[dict]) -> list[dict]:
        return [{
            "toolSpec": {
                "name": t["name"],
                "description": t["description"],
                "inputSchema": {"json": t["input_schema"]},
            }
        } for t in tools]

    def _convert_messages_to_bedrock(self, messages: list[dict]) -> list[dict]:
        """Convert Anthropic message format to Bedrock Converse format."""
        converted = []
        for msg in messages:
            role = msg["role"]
            content = msg["content"]

            if isinstance(content, str):
                converted.append({"role": role, "content": [{"text": content}]})
            elif isinstance(content, list):
                bedrock_content = []
                for block in content:
                    btype = block.get("type")
                    if btype == "text":
                        bedrock_content.append({"text": block["text"]})
                    elif btype == "tool_use":
                        bedrock_content.append({
                            "toolUse": {
                                "toolUseId": block["id"],
                                "name": block["name"],
                                "input": block.get("input", {}),
                            }
                        })
                    elif btype == "tool_result":
                        bedrock_content.append({
                            "toolResult": {
                                "toolUseId": block["tool_use_id"],
                                "content": [{"text": str(block.get("content", ""))}],
                            }
                        })
                if bedrock_content:
                    converted.append({"role": role, "content": bedrock_content})
        return converted

    def complete_with_tools(self, system, messages, tools, max_tokens=4096) -> dict:
        bedrock_messages = self._convert_messages_to_bedrock(messages)
        bedrock_tools = self._anthropic_tools_to_bedrock(tools)

        response = self._client.converse(
            modelId=self.model,
            system=[{"text": system}],
            messages=bedrock_messages,
            toolConfig={"tools": bedrock_tools},
            inferenceConfig={"maxTokens": max_tokens},
        )

        # Convert Bedrock response → Anthropic format
        content = []
        for block in response.get("output", {}).get("message", {}).get("content", []):
            if "text" in block:
                content.append({"type": "text", "text": block["text"]})
            elif "toolUse" in block:
                tu = block["toolUse"]
                content.append({
                    "type": "tool_use",
                    "id": tu["toolUseId"],
                    "name": tu["name"],
                    "input": tu.get("input", {}),
                })

        usage = response.get("usage", {})
        return {
            "content": content,
            "usage": {
                "input_tokens": usage.get("inputTokens", 0),
                "output_tokens": usage.get("outputTokens", 0),
            }
        }


# ── Ollama ────────────────────────────────────────────────────────────────────

class OllamaProvider(LLMProvider):
    """Ollama with tool use support (requires Ollama >= 0.3 and a tool-capable model)."""

    def __init__(self, model: str, base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")

    def name(self) -> str:
        return f"ollama/{self.model}"

    def _anthropic_to_ollama_tools(self, tools: list[dict]) -> list[dict]:
        return [{
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            }
        } for t in tools]

    def _convert_messages(self, messages: list[dict]) -> list[dict]:
        converted = []
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            if isinstance(content, str):
                converted.append({"role": role, "content": content})
            elif isinstance(content, list):
                for block in content:
                    btype = block.get("type")
                    if btype == "text":
                        converted.append({"role": role, "content": block["text"]})
                    elif btype == "tool_use":
                        converted.append({
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{
                                "id": block["id"],
                                "type": "function",
                                "function": {
                                    "name": block["name"],
                                    "arguments": json.dumps(block.get("input", {})),
                                }
                            }]
                        })
                    elif btype == "tool_result":
                        converted.append({
                            "role": "tool",
                            "content": str(block.get("content", "")),
                        })
        return converted

    def complete_with_tools(self, system, messages, tools, max_tokens=4096) -> dict:
        ollama_messages = [{"role": "system", "content": system}] + self._convert_messages(messages)
        ollama_tools = self._anthropic_to_ollama_tools(tools)

        payload = json.dumps({
            "model": self.model,
            "messages": ollama_messages,
            "tools": ollama_tools,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }).encode()

        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
        except urllib.error.URLError as e:
            raise ConnectionError(f"Cannot connect to Ollama at {self.base_url}: {e}")

        msg = data.get("message", {})
        content = []
        if msg.get("content"):
            content.append({"type": "text", "text": msg["content"]})
        for i, tc in enumerate(msg.get("tool_calls", [])):
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            content.append({
                "type": "tool_use",
                "id": f"ollama_call_{i}",
                "name": fn.get("name", ""),
                "input": args,
            })
        return {"content": content, "usage": {}}


# ── Google Gemini ─────────────────────────────────────────────────────────────

class GeminiProvider(LLMProvider):
    def __init__(self, model: str, api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY environment variable not set")

    def name(self) -> str:
        return f"gemini/{self.model}"

    def _convert_tools(self, tools: list[dict]) -> list[dict]:
        return [{
            "functionDeclarations": [{
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            } for t in tools]
        }]

    def _convert_messages(self, messages: list[dict]) -> list[dict]:
        converted = []
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            content = msg["content"]
            parts = []
            if isinstance(content, str):
                parts = [{"text": content}]
            elif isinstance(content, list):
                for block in content:
                    btype = block.get("type")
                    if btype == "text":
                        parts.append({"text": block["text"]})
                    elif btype == "tool_use":
                        parts.append({"functionCall": {"name": block["name"], "args": block.get("input", {})}})
                    elif btype == "tool_result":
                        parts.append({"functionResponse": {
                            "name": "tool",
                            "response": {"content": block.get("content", "")},
                        }})
            if parts:
                converted.append({"role": role, "parts": parts})
        return converted

    def complete_with_tools(self, system, messages, tools, max_tokens=4096) -> dict:
        gemini_messages = self._convert_messages(messages)
        gemini_tools = self._convert_tools(tools)

        payload = json.dumps({
            "system_instruction": {"parts": [{"text": system}]},
            "contents": gemini_messages,
            "tools": gemini_tools,
            "generationConfig": {"maxOutputTokens": max_tokens},
        }).encode()

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())

        content = []
        for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
            if "text" in part:
                content.append({"type": "text", "text": part["text"]})
            elif "functionCall" in part:
                fc = part["functionCall"]
                content.append({
                    "type": "tool_use",
                    "id": f"gemini_{fc['name']}",
                    "name": fc["name"],
                    "input": fc.get("args", {}),
                })
        return {"content": content, "usage": {}}


# ── Factory ───────────────────────────────────────────────────────────────────

def create_provider(provider: str, model: str, **kwargs) -> LLMProvider:
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
        raise ValueError(f"Unknown provider: {provider}")
