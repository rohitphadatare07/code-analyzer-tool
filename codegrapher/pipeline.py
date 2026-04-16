"""
CodeGrapher pipeline orchestrator.

Stages:
  1. LLM provider creation         (codegrapher.agent.providers)
  2. Exploration loop               (codegrapher.agent.graph)
       └─ 6 tools: scan / AST / cluster / analyze / read_file / search_code
       └─ Exits naturally when agent stops calling tools
       └─ No finish_analysis — no throttle loop risk
  3. Synthesis                      (codegrapher.agent.synthesiser)
       └─ One dedicated LLM call outside the agent loop
       └─ Receives compact compressed context (~1000 tokens)
       └─ Has its own retry + exponential backoff
       └─ Produces finish_data dict
  4. JSON document builder          (codegrapher.agent.json_agent)
       └─ Merges finish_data + graph outputs → structured JSON
  5. PDF report                     (codegrapher.output.pdf_generator)
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

    # ── 2. Exploration loop ───────────────────────────────────────────────────
    print(f"\n🔍 Starting exploration loop...")
    print(f"   Repository  : {args.repo_path.resolve()}")
    print(f"   Max steps   : {getattr(args, 'max_iterations', 40)}")
    print(f"\n   Agent will:")
    print(f"   → Run AST extraction + Leiden clustering")
    print(f"   → Read key files and search for patterns")
    print(f"   → Stop when exploration is complete\n")

    try:
        from codegrapher.agent.graph import run_agent
        state_accumulator, final_state = run_agent(
            llm=llm,
            repo_root=args.repo_path,
            max_iterations=getattr(args, "max_iterations", 40),
            verbose=getattr(args, "verbose", False),
        )
    except Exception as e:
        print(f"error during exploration loop: {e}", file=sys.stderr)
        if getattr(args, "verbose", False):
            import traceback
            traceback.print_exc()
        sys.exit(1)

    iteration = final_state.get("iteration", 0)
    messages = final_state.get("messages", [])

    print(f"\n   ✅ Exploration completed:")
    print(f"      Steps        : {iteration}")
    print(f"      God nodes    : {len(state_accumulator.get('god_nodes', []))}")
    print(f"      Communities  : {len(state_accumulator.get('communities', {}))}")
    print(f"      Messages     : {len(messages)}")

    # ── 3. Synthesis — separate LLM call with retry/backoff ───────────────────
    print("\n🧠 Synthesising analysis (separate call, retry-safe)...")
    try:
        from codegrapher.agent.synthesiser import build_synthesis_context, synthesise

        context = build_synthesis_context(
            state_accumulator=state_accumulator,
            messages=messages,
        )
        print(f"   Context size : {len(context):,} chars (~{len(context)//4} tokens)")

        finish_data = synthesise(
            llm=llm,
            context=context,
            max_retries=4,
            base_delay=2.0,
            verbose=True,
        )
        state_accumulator["finish_data"] = finish_data
        print(f"   ✅ Synthesis complete")

    except Exception as e:
        print(f"   ⚠️  Synthesis failed: {e} — report will be partial", file=sys.stderr)
        state_accumulator["finish_data"] = {}

    # ── 4. Collect stats ──────────────────────────────────────────────────────
    scan = state_accumulator.get("scan_result", {})
    total_files = scan.get("total_files", 0)
    tree = scan.get("directory_tree", "")
    elapsed = time.time() - start

    # ── 5. Build structured JSON document ─────────────────────────────────────
    print("\n📋 Building structured analysis JSON...")
    analysis_doc = None
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
        json_path = args.output.with_suffix(".json")
        json_path.write_text(build_analysis_json_string(analysis_doc), encoding="utf-8")
        print(f"   JSON saved : {json_path}")
    except Exception as e:
        print(f"   ⚠️  JSON build failed: {e}", file=sys.stderr)

    # ── 6. Generate PDF ───────────────────────────────────────────────────────
    print("\n📄 Generating PDF report...")
    output_path: Path = args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from codegrapher.output.pdf_generator import generate_pdf_from_json, generate_pdf
        if analysis_doc is not None:
            generate_pdf_from_json(output_path=output_path, doc=analysis_doc)
        else:
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
