"""
The Agentic Loop.

The LLM is the agent. It decides:
  - Which files to read (and in what order)
  - What to search for
  - When it has enough context
  - When to record a finding vs keep exploring
  - When to generate diagrams
  - When analysis is complete

The human code only:
  - Provides tools
  - Routes tool calls to executors
  - Enforces safety limits (max iterations)
  - Passes results back to the agent
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from repoanalyzer_v2.tools import TOOL_SCHEMAS, ToolExecutor, ToolResult


AGENT_SYSTEM_PROMPT = """You are an expert software architect performing a deep analysis of a code repository.

You have access to tools that let you explore the repository. You are the decision-maker — you decide:
- Which files to read and in what order (start broad, then drill deep)
- What patterns to search for
- When you've gathered enough information about a component
- When to record findings vs continue exploring
- When to generate diagrams (only after you understand the system)
- When the analysis is complete

## Your Analysis Strategy

1. **Orient first**: Call `get_file_stats` and `list_directory` to understand the repo's shape
2. **Find entry points**: Read README, main files, config files, package manifests
3. **Trace the architecture**: Follow imports to understand how modules connect
4. **Go deep on key components**: Read the most important files fully
5. **Search for patterns**: Find API routes, database models, auth logic, etc.
6. **Record findings as you go**: Don't wait until the end — record each insight immediately
7. **Generate diagrams when ready**: Only when you have enough understanding
8. **Finish**: Call `finish_analysis` with your executive summary

## Rules
- Always start with `get_file_stats` to understand the overall scope
- Read at least 8-10 files before generating diagrams
- Record a finding every time you discover something significant
- Use `search_code` to trace how things connect across files
- Use `read_multiple_files` when you need context from several related files at once
- Only call `finish_analysis` when you have a complete picture
- Be thorough — a good analysis requires 15-25 tool calls minimum

## Finding Categories
Use `record_finding` with these categories:
- `architecture` — overall structure and patterns
- `component` — a specific module/class/service
- `data_flow` — how data moves through the system
- `api_endpoint` — HTTP routes/endpoints
- `security` — security concerns or good practices
- `tech_stack` — languages, frameworks, libraries
- `design_pattern` — patterns like Factory, Observer, Repository
- `code_quality` — quality observations
- `improvement` — concrete improvement suggestions
- `entry_point` — main entry points
- `dependency` — external dependencies
- `database_model` — data models/entities
- `testing` — test strategy
- `deployment` — how it runs/deploys
- `file_detail` — detailed analysis of a specific important file
"""


@dataclass
class AgentStep:
    """Represents one step of the agent's reasoning."""
    step_num: int
    thinking: str        # Agent's reasoning text
    tool_calls: list[dict]
    tool_results: list[ToolResult]
    timestamp: float = field(default_factory=time.time)


@dataclass
class AgentRun:
    """Complete record of an agent's analysis run."""
    steps: list[AgentStep] = field(default_factory=list)
    total_tool_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    elapsed_seconds: float = 0.0


class AgentLoop:
    """
    The agentic loop.

    The LLM is given tools and runs in a loop:
      1. LLM receives current state and decides what to do
      2. LLM calls one or more tools
      3. Tool results are returned to LLM
      4. LLM continues until it calls finish_analysis
    """

    def __init__(
        self,
        llm_provider,
        repo_root: Path,
        max_iterations: int = 40,
        on_step: Callable[[AgentStep], None] | None = None,
        verbose: bool = False,
    ):
        self.llm = llm_provider
        self.executor = ToolExecutor(repo_root)
        self.max_iterations = max_iterations
        self.on_step = on_step
        self.verbose = verbose
        self.run = AgentRun()
        self._messages: list[dict] = []

    def _call_llm_with_tools(self) -> dict:
        """Call the LLM provider with tool use support."""
        return self.llm.complete_with_tools(
            system=AGENT_SYSTEM_PROMPT,
            messages=self._messages,
            tools=TOOL_SCHEMAS,
            max_tokens=4096,
        )

    def _process_response(self, response: dict) -> tuple[str, list[dict]]:
        """Extract thinking text and tool calls from LLM response."""
        thinking_parts = []
        tool_calls = []

        for block in response.get("content", []):
            if block.get("type") == "text":
                thinking_parts.append(block["text"])
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block["id"],
                    "name": block["name"],
                    "input": block.get("input", {}),
                })

        return "\n".join(thinking_parts), tool_calls

    def _execute_tools(self, tool_calls: list[dict]) -> list[ToolResult]:
        """Execute all tool calls and return results."""
        results = []
        for call in tool_calls:
            if self.verbose:
                print(f"      🔧 {call['name']}({json.dumps(call['input'])[:80]}...)")
            result = self.executor.execute(call["name"], call["input"])
            results.append(result)
            self.run.total_tool_calls += 1
        return results

    def _build_tool_result_message(self, tool_calls: list[dict], results: list[ToolResult]) -> dict:
        """Build the tool_result message to send back to the LLM."""
        content = []
        for call, result in zip(tool_calls, results):
            content.append({
                "type": "tool_result",
                "tool_use_id": call["id"],
                "content": result.output[:8000],  # cap per result
            })
        return {"role": "user", "content": content}

    def run_loop(self) -> tuple[ToolExecutor, AgentRun]:
        """Run the agentic loop until completion or max iterations."""
        start = time.time()

        # Initial message to kick off the agent
        self._messages = [{
            "role": "user",
            "content": (
                f"Please analyze the repository at: {self.executor.repo_root}\n\n"
                "Start by getting file stats and exploring the directory structure, "
                "then systematically read key files, record your findings, generate diagrams, "
                "and finish with an executive summary. Take your time and be thorough."
            ),
        }]

        for iteration in range(self.max_iterations):
            if self.verbose:
                print(f"\n   [Step {iteration + 1}]")

            # LLM decides what to do
            response = self._call_llm_with_tools()

            # Track token usage
            usage = response.get("usage", {})
            self.run.total_input_tokens += usage.get("input_tokens", 0)
            self.run.total_output_tokens += usage.get("output_tokens", 0)

            # Extract thinking and tool calls
            thinking, tool_calls = self._process_response(response)

            if self.verbose and thinking:
                print(f"   💭 {thinking[:200]}...")

            # No tool calls = agent is done or stuck
            if not tool_calls:
                if self.verbose:
                    print("   Agent returned no tool calls — stopping loop")
                break

            # Execute tool calls
            results = self._execute_tools(tool_calls)

            # Record this step
            step = AgentStep(
                step_num=iteration + 1,
                thinking=thinking,
                tool_calls=tool_calls,
                tool_results=results,
            )
            self.run.steps.append(step)

            if self.on_step:
                self.on_step(step)

            # Add assistant response to conversation
            self._messages.append({
                "role": "assistant",
                "content": response["content"],
            })

            # Add tool results back to conversation
            self._messages.append(
                self._build_tool_result_message(tool_calls, results)
            )

            # Check if agent called finish_analysis
            if self.executor.finished:
                if self.verbose:
                    print(f"\n   ✅ Agent finished after {iteration + 1} steps")
                break

        self.run.elapsed_seconds = time.time() - start
        return self.executor, self.run
