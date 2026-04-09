"""Main pipeline - orchestrates scanning, analysis, and PDF generation."""
from __future__ import annotations

import sys
import time
from argparse import Namespace
from pathlib import Path


def run_pipeline(args: Namespace) -> None:
    """Run the full repo analysis pipeline."""
    start = time.time()

    # ── Step 1: Create provider ───────────────────────────────────────────────
    print("⚙️  Initializing LLM provider...")
    try:
        from repoanalyzer.providers import create_provider
        llm = create_provider(
            provider=args.provider,
            model=args.model,
            ollama_url=getattr(args, "ollama_url", "http://localhost:11434"),
        )
        print(f"   Provider: {llm.name()}")
    except (ValueError, ImportError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    # ── Step 2: Scan repository ───────────────────────────────────────────────
    print("\n📂 Scanning repository...")
    from repoanalyzer.scanner import scan_repository, format_scan_summary
    scan = scan_repository(
        root=args.repo_path,
        exclude_patterns=getattr(args, "exclude", []),
        max_file_size=getattr(args, "max_file_size", 100_000),
        max_files=getattr(args, "max_files", 200),
        verbose=getattr(args, "verbose", False),
    )
    print(f"   {format_scan_summary(scan)}")

    if not scan.files:
        print("error: no analyzable files found in repository", file=sys.stderr)
        sys.exit(1)

    if scan.skipped_files:
        print(f"   Skipped {len(scan.skipped_files)} files")
        if getattr(args, "verbose", False):
            for s in scan.skipped_files[:5]:
                print(f"     - {s}")

    # ── Step 3: Analyze with LLM ──────────────────────────────────────────────
    print("\n🤖 Analyzing with LLM...")
    from repoanalyzer.analyzer import run_analysis
    try:
        result = run_analysis(llm, scan)
    except ConnectionError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"error during analysis: {e}", file=sys.stderr)
        if getattr(args, "verbose", False):
            import traceback
            traceback.print_exc()
        sys.exit(1)

    # ── Step 4: Generate PDF ──────────────────────────────────────────────────
    print("\n📄 Generating PDF report...")
    from repoanalyzer.pdf_generator import generate_pdf

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
        if getattr(args, "verbose", False):
            import traceback
            traceback.print_exc()
        sys.exit(1)

    elapsed = time.time() - start
    size_kb = output_path.stat().st_size // 1024

    print(f"\n✅ Done in {elapsed:.1f}s")
    print(f"   Output: {output_path.resolve()}")
    print(f"   Size:   {size_kb} KB")
    print()
