"""
LangGraph state schema — minimal by design.

AgentState holds ONLY what LangGraph needs to manage the conversation loop.
Three fields:
  messages  — full conversation history (LangGraph appends via add_messages)
  iteration — step counter for safety limit enforcement
  finished  — set True when finish_analysis is called; stops the loop

Everything else (graph, communities, findings, assessment, diagrams,
finish_data, scan_result) lives in state_accumulator — a plain Python dict
created in run_agent() and shared into every tool via closure.

Why this separation:
  - LangGraph state must be serialisable for checkpointing
  - NetworkX graphs and large data structures are not serialisable
  - Keeping state lean avoids confusion between agent memory and outputs
  - state_accumulator is the "filing cabinet"; AgentState is "working memory"
"""
from __future__ import annotations

from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    messages:  Annotated[list, add_messages]   # full conversation; appended by reducer
    iteration: int                              # incremented each agent step
    finished:  bool                             # True after finish_analysis is called