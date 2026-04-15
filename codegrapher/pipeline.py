"""
CodeGrapher pipeline orchestrator.

Wires together:
  1.  LLM provider creation   (codegrapher.agent.providers)
  2.  LangGraph agentic loop  (codegrapher.agent.graph)
       └─ tools call into     (codegrapher.core.*) for graph work
  3.  Mermaid diagrams        (codegrapher.output.mermaid_converter)
       └─ rendered via        (codegrapher.output.diagrams)  — offline-capable
  4.  PDF report              (codegrapher.output.pdf_generator)
"""
from __future__ import annotations

import sys
import time
from argparse import Namespace
from pathlib import Path


def run_pipeline(args: Namespace) -> None:
    start = time.time()

    # ── 1. LLM provider ──────────────────────────────────────────────────────
    print("⚙️  Initializing LLM provider...")
    try:
        from codegrapher.agent.providers import create_llm, provider_display_name
        llm = create_llm(
            provider=args.provider,
            model=getattr(args, "model", None),
            aws_region=getattr(args, "aws_region", None),
            api_url=getattr(args, "api_url", None),
            api_key=getattr(args, "api_key", None),
        )
        pname = provider_display_name(
            args.provider,
            getattr(args, "model", None),
            getattr(args, "api_url", None),
        )
        print(f"   Provider : {pname}")
    except (ValueError, ImportError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    # ── 2. LangGraph agentic loop ─────────────────────────────────────────────
    print(f"\n🤖 Starting LangGraph agentic analysis...")
    print(f"   Repository  : {args.repo_path.resolve()}")
    print(f"   Max steps   : {getattr(args, 'max_iterations', 50)}")
    print(f"\n   Agent will autonomously:")
    print(f"   → scan_repository → build_code_graph (AST + clustering + diagrams in one call)")
    print(f"   → Read key files in parallel batches (read_multiple_files)")
    print(f"   → Search for patterns + record findings (batched)")
    print(f"   → finish_analysis with executive summary\n")

    try:
        from codegrapher.agent.graph import run_agent
        state_accumulator, final_state = run_agent(
            llm=llm,
            repo_root=args.repo_path,
            max_iterations=getattr(args, "max_iterations", 50),
            verbose=True,
        )
    except Exception as e:
        print(f"error during agentic loop: {e}", file=sys.stderr)
        if getattr(args, "verbose", False):
            import traceback
            traceback.print_exc()
        sys.exit(1)

    findings = state_accumulator.get("findings", [])
    diagrams_raw = state_accumulator.get("diagrams", [])
    iteration = final_state.get("iteration", 0)

    print(f"\n   ✅ Agent completed:")
    print(f"      Steps        : {iteration}")
    print(f"      Findings     : {len(findings)}")
    print(f"      God nodes    : {len(state_accumulator.get('god_nodes', []))}")
    print(f"      Communities  : {len(state_accumulator.get('communities', {}))}")

    if not state_accumulator.get("finished"):
        print("      ⚠️  finish_analysis not called — report may be partial")

    # ── 3. Generate diagrams from real graph data ─────────────────────────────
    print("\n📊 Generating diagrams from graph data...")
    try:
        from codegrapher.output.mermaid_converter import generate_all_diagrams
        diagrams = generate_all_diagrams(state_accumulator)
        state_accumulator["diagrams"] = diagrams
        print(f"   Diagrams     : {[d['diagram_type'] for d in diagrams]}")
    except Exception as e:
        print(f"   ⚠️  Diagram generation failed: {e} — continuing without diagrams")

    # ── 4. Collect stats ──────────────────────────────────────────────────────
    scan = state_accumulator.get("scan_result", {})
    total_files = scan.get("total_files", 0)
    tree = scan.get("directory_tree", "")
    elapsed = time.time() - start

    # ── 5. Generate PDF ───────────────────────────────────────────────────────
    print("\n📄 Generating PDF report...")
    output_path: Path = args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from codegrapher.output.pdf_generator import generate_pdf
        generate_pdf(
            output_path=output_path,
            repo_name=args.repo_path.resolve().name,
            provider_name=pname,
            state_accumulator=state_accumulator,
            directory_tree=tree,
            total_files=total_files,
            total_lines=0,
            agent_steps=iteration,
            agent_tool_calls=iteration,
            elapsed_seconds=elapsed,
        )
    except Exception as e:
        print(f"error generating PDF: {e}", file=sys.stderr)
        if getattr(args, "verbose", False):
            import traceback
            traceback.print_exc()
        sys.exit(1)

    size_kb = output_path.stat().st_size // 1024
    print(f"\n✅ Done in {elapsed:.1f}s")
    print(f"   Output : {output_path.resolve()}")
    print(f"   Size   : {size_kb} KB\n")
