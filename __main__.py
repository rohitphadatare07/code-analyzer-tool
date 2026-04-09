"""repoanalyzer CLI - analyze any code repository and generate a PDF report."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="repoanalyzer",
        description="Analyze a code repository and generate a comprehensive PDF report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  repoanalyzer /path/to/repo
  repoanalyzer /path/to/repo --provider anthropic --model claude-sonnet-4-20250514
  repoanalyzer /path/to/repo --provider openai --model gpt-4o
  repoanalyzer /path/to/repo --provider bedrock --model anthropic.claude-3-5-sonnet-20241022-v2:0
  repoanalyzer /path/to/repo --provider ollama --model llama3.2
  repoanalyzer /path/to/repo --output ./my-report.pdf
  repoanalyzer /path/to/repo --exclude "*.test.*" --exclude "node_modules"

Supported providers:
  anthropic   - Claude models (default)
  openai      - GPT models
  bedrock     - AWS Bedrock models (requires AWS credentials)
  ollama      - Local models via Ollama
  gemini      - Google Gemini models

Environment variables:
  ANTHROPIC_API_KEY     - for anthropic provider
  OPENAI_API_KEY        - for openai provider
  AWS_REGION            - for bedrock provider (default: us-east-1)
  AWS_ACCESS_KEY_ID     - for bedrock provider
  AWS_SECRET_ACCESS_KEY - for bedrock provider
  GEMINI_API_KEY        - for gemini provider
        """,
    )

    parser.add_argument(
        "repo_path",
        type=Path,
        help="Path to the repository folder to analyze",
    )
    parser.add_argument(
        "--provider",
        default="anthropic",
        choices=["anthropic", "openai", "bedrock", "ollama", "gemini"],
        help="LLM provider to use (default: anthropic)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name/ID (uses provider default if not specified)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output PDF path (default: <repo_name>-report.pdf in current directory)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Glob patterns to exclude (can be used multiple times)",
    )
    parser.add_argument(
        "--max-file-size",
        type=int,
        default=100_000,
        metavar="BYTES",
        help="Skip files larger than this size in bytes (default: 100000)",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=200,
        metavar="N",
        help="Maximum number of files to analyze (default: 200)",
    )
    parser.add_argument(
        "--ollama-url",
        default="http://localhost:11434",
        help="Ollama base URL (default: http://localhost:11434)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print detailed progress",
    )

    args = parser.parse_args()

    # Validate repo path
    if not args.repo_path.exists():
        print(f"error: repository path does not exist: {args.repo_path}", file=sys.stderr)
        sys.exit(1)
    if not args.repo_path.is_dir():
        print(f"error: path is not a directory: {args.repo_path}", file=sys.stderr)
        sys.exit(1)

    # Set default output path
    if args.output is None:
        repo_name = args.repo_path.resolve().name
        args.output = Path(f"{repo_name}-report.pdf")

    # Set default model per provider
    default_models = {
        "anthropic": "claude-sonnet-4-20250514",
        "openai": "gpt-4o",
        "bedrock": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "ollama": "llama3.2",
        "gemini": "gemini-2.0-flash",
    }
    if args.model is None:
        args.model = default_models[args.provider]

    print(f"\n🔍 RepoAnalyzer")
    print(f"   Repository : {args.repo_path.resolve()}")
    print(f"   Provider   : {args.provider}")
    print(f"   Model      : {args.model}")
    print(f"   Output     : {args.output}")
    print()

    from repoanalyzer.pipeline import run_pipeline
    run_pipeline(args)


if __name__ == "__main__":
    main()
