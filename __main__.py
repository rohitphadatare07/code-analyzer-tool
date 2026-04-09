"""repoanalyzer v2 — Agentic code repository analyzer."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="repoanalyzer",
        description="Agentic AI-powered code repository analyzer — the LLM drives the analysis.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
How it works (agentic):
  The LLM agent autonomously explores the repository using tools:
  it lists directories, reads files, searches for patterns,
  records findings, generates diagrams, and decides when it's done.
  You don't tell it what to read — it figures that out itself.

Examples:
  repoanalyzer /path/to/repo
  repoanalyzer /path/to/repo --provider anthropic --model claude-sonnet-4-20250514
  repoanalyzer /path/to/repo --provider openai --model gpt-4o
  repoanalyzer /path/to/repo --provider bedrock --model anthropic.claude-3-5-sonnet-20241022-v2:0
  repoanalyzer /path/to/repo --provider bedrock --model amazon.nova-pro-v1:0
  repoanalyzer /path/to/repo --provider bedrock --model meta.llama3-70b-instruct-v1:0
  repoanalyzer /path/to/repo --provider ollama --model llama3.2
  repoanalyzer /path/to/repo --provider gemini --model gemini-2.0-flash
  repoanalyzer /path/to/repo --output ./reports/myrepo.pdf --max-iterations 30

Bedrock models with tool use support:
  anthropic.claude-3-5-sonnet-20241022-v2:0   (best quality)
  anthropic.claude-3-haiku-20240307-v1:0      (fast)
  amazon.nova-pro-v1:0                         (Amazon's flagship)
  amazon.nova-lite-v1:0                        (fast & cheap)
  meta.llama3-1-70b-instruct-v1:0             (open source)
  mistral.mistral-large-2402-v1:0             (Mistral)

Environment variables:
  ANTHROPIC_API_KEY     for anthropic provider
  OPENAI_API_KEY        for openai provider
  AWS_REGION            for bedrock (default: us-east-1)
  AWS_ACCESS_KEY_ID     for bedrock
  AWS_SECRET_ACCESS_KEY for bedrock
  GEMINI_API_KEY        for gemini provider
        """,
    )

    parser.add_argument("repo_path", type=Path, help="Path to the repository folder to analyze")
    parser.add_argument(
        "--provider", default="anthropic",
        choices=["anthropic", "openai", "bedrock", "ollama", "gemini"],
        help="LLM provider (default: anthropic)",
    )
    parser.add_argument("--model", default=None, help="Model name (uses provider default if omitted)")
    parser.add_argument("--output", type=Path, default=None, help="Output PDF path")
    parser.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                        help="Glob patterns to exclude (repeatable)")
    parser.add_argument("--max-file-size", type=int, default=100_000, metavar="BYTES")
    parser.add_argument("--max-files", type=int, default=200, metavar="N")
    parser.add_argument("--max-iterations", type=int, default=40, metavar="N",
                        help="Max agent loop iterations (default: 40)")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    if not args.repo_path.exists():
        print(f"error: path does not exist: {args.repo_path}", file=sys.stderr)
        sys.exit(1)
    if not args.repo_path.is_dir():
        print(f"error: not a directory: {args.repo_path}", file=sys.stderr)
        sys.exit(1)

    if args.output is None:
        repo_name = args.repo_path.resolve().name
        args.output = Path(f"{repo_name}-report.pdf")

    defaults = {
        "anthropic": "claude-sonnet-4-20250514",
        "openai": "gpt-4o",
        "bedrock": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "ollama": "llama3.2",
        "gemini": "gemini-2.0-flash",
    }
    if args.model is None:
        args.model = defaults[args.provider]

    print(f"\n🔍 RepoAnalyzer v2 (Agentic)")
    print(f"   Repository  : {args.repo_path.resolve()}")
    print(f"   Provider    : {args.provider}")
    print(f"   Model       : {args.model}")
    print(f"   Output      : {args.output}")
    print(f"   Max iters   : {args.max_iterations}")
    print()

    from repoanalyzer_v2.pipeline import run_pipeline
    run_pipeline(args)


if __name__ == "__main__":
    main()
