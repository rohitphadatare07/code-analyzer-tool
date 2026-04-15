"""
CodeGrapher pipeline orchestrator.

Wires together:
  1.  LLM provider creation   (codegrapher.agent.providers)
  2.  LangGraph agentic loop  (codegrapher.agent.graph)
       └─ tools call into     (codegrapher.core.*) for graph work
  3.  JSON document builder   (codegrapher.agent.json_agent)
       └─ converts tool outputs → structured analysis JSON
  4.  PDF report              (codegrapher.output.pdf_generator)
       └─ renders JSON → PDF  (no Mermaid, no diagram rendering)
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
    print(f"   → Run graphify AST extraction + Leiden clustering")
    print(f"   → Decide which files to read")
    print(f"   → Record findings as it explores")
    print(f"   → Generate diagrams from real graph data")
    print(f"   → Signal completion with executive summary\n")

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

    findings = state_accumulator.get("finish_data", {})
    iteration = final_state.get("iteration", 0)

    print(f"\n   ✅ Agent completed:")
    print(f"      Steps        : {iteration}")
    print(f"      God nodes    : {len(state_accumulator.get('god_nodes', []))}")
    print(f"      Communities  : {len(state_accumulator.get('communities', {}))}")

    if not state_accumulator.get("finished"):
        print("      ⚠️  finish_analysis not called — report may be partial")

    # ── 3. Build structured JSON document from tool outputs ───────────────────
    print("\n📋 Building structured analysis JSON...")
    try:
        from codegrapher.agent.json_agent import build_analysis_json, build_analysis_json_string
        analysis_doc = build_analysis_json(
            state_accumulator=state_accumulator,
            repo_name=args.repo_path.resolve().name,
            provider_name=pname,
            total_files=total_files,
            total_lines=0,
            agent_steps=iteration,
            elapsed_seconds=elapsed,
            directory_tree=tree,
        )
        # Optionally persist JSON alongside the PDF for debugging / downstream use
        json_path = args.output.with_suffix(".json")
        json_path.write_text(build_analysis_json_string(analysis_doc), encoding="utf-8")
        print(f"   JSON saved   : {json_path}")
        state_accumulator["analysis_doc"] = analysis_doc
    except Exception as e:
        print(f"   ⚠️  JSON build failed: {e} — PDF will use raw state_accumulator")
        analysis_doc = None

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
        from codegrapher.output.pdf_generator import generate_pdf_from_json, generate_pdf
        if analysis_doc is not None:
            generate_pdf_from_json(output_path=output_path, doc=analysis_doc)
        else:
            # Fallback: use legacy shim (builds JSON internally)
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
