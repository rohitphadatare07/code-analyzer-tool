"""
Agentic pipeline orchestrator.

Unlike v1 (fixed steps), this pipeline:
1. Creates tools + executor
2. Starts the agent loop
3. The AGENT decides the rest
4. Compiles agent findings → PDF
"""
from __future__ import annotations

import sys
import time
from argparse import Namespace
from pathlib import Path

from repoanalyzer_v2.agent import AgentLoop, AgentStep


def _print_step(step: AgentStep) -> None:
    """Print a summary of an agent step as it happens."""
    tool_names = [tc["name"] for tc in step.tool_calls]
    tools_str = ", ".join(tool_names)
    thinking_preview = step.thinking[:120].replace("\n", " ") if step.thinking else ""
    if thinking_preview:
        print(f"   💭 {thinking_preview}...")
    for call, result in zip(step.tool_calls, step.tool_results):
        icon = "✓" if result.success else "✗"
        inp_preview = str(call["input"])[:60]
        print(f"   {icon} {call['name']}({inp_preview})")


def run_pipeline(args: Namespace) -> None:
    start = time.time()

    # ── 1. Provider ───────────────────────────────────────────────────────────
    print("⚙️  Initializing LLM provider...")
    try:
        from repoanalyzer_v2.providers import create_provider
        llm = create_provider(
            provider=args.provider,
            model=args.model,
            ollama_url=getattr(args, "ollama_url", "http://localhost:11434"),
        )
        print(f"   Provider: {llm.name()}")
    except (ValueError, ImportError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    # ── 2. Scan (lightweight — just for PDF stats/tree) ───────────────────────
    print("\n📂 Scanning repository structure...")
    from repoanalyzer_v2.scanner import scan_repository, format_scan_summary
    scan = scan_repository(
        root=args.repo_path,
        exclude_patterns=getattr(args, "exclude", []),
        max_file_size=getattr(args, "max_file_size", 100_000),
        max_files=getattr(args, "max_files", 200),
    )
    print(f"   {format_scan_summary(scan)}")

    if not scan.files:
        print("error: no analyzable files found", file=sys.stderr)
        sys.exit(1)

    # ── 3. Agentic loop ───────────────────────────────────────────────────────
    print(f"\n🤖 Starting agentic analysis loop...")
    print(f"   The agent will autonomously explore, decide what to read,")
    print(f"   record findings, and signal when done.\n")

    verbose = getattr(args, "verbose", False)
    max_iters = getattr(args, "max_iterations", 40)

    loop = AgentLoop(
        llm_provider=llm,
        repo_root=args.repo_path,
        max_iterations=max_iters,
        on_step=_print_step if verbose else _print_step,  # always show steps
        verbose=False,
    )

    try:
        executor, run = loop.run_loop()
    except ConnectionError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"error during agentic loop: {e}", file=sys.stderr)
        if verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)

    print(f"\n   Agent completed:")
    print(f"   Steps: {len(run.steps)}")
    print(f"   Tool calls: {run.total_tool_calls}")
    print(f"   Findings: {len(executor.findings)}")
    print(f"   Diagrams: {list(executor.diagrams.keys())}")
    print(f"   Tokens: {run.total_input_tokens:,} in / {run.total_output_tokens:,} out")

    if not executor.finished:
        print("   ⚠️  Agent did not call finish_analysis — report may be incomplete")

    # ── 4. Compile results ────────────────────────────────────────────────────
    print("\n📊 Compiling agent findings...")
    from repoanalyzer_v2.results import compile_results
    result = compile_results(executor, run, llm.name())

    print(f"   Purpose: {result.purpose[:80]}...")
    print(f"   Architecture: {result.architecture_style}")
    print(f"   Components: {len(result.key_components)}")
    print(f"   Tech stack items: {len(result.tech_stack)}")

    # ── 5. Generate PDF ───────────────────────────────────────────────────────
    print("\n📄 Generating PDF report...")
    from repoanalyzer_v2.pdf_generator import generate_pdf

    output_path: Path = args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        generate_pdf(
            output_path=output_path,
            scan=scan,
            result=result,
            provider_name=llm.name(),
        )
    except Exception as e:
        print(f"error generating PDF: {e}", file=sys.stderr)
        if verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)

    elapsed = time.time() - start
    size_kb = output_path.stat().st_size // 1024

    print(f"\n✅ Done in {elapsed:.1f}s")
    print(f"   Output: {output_path.resolve()}")
    print(f"   Size:   {size_kb} KB")
    print()
