"""
codegrapher CLI — Agentic repository analyzer.

Usage:
    codegrapher /path/to/repo [options]

Run `codegrapher --help` for full option list.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="codegrapher",
        description=(
            "Agentic repository analyzer.\n"
            "LangGraph ReAct loop + graphify AST/graph libraries + offline PDF report."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
─────────────────────────────────────────────────────────────────
 PROVIDER EXAMPLES
─────────────────────────────────────────────────────────────────

  Anthropic Claude (default):
    export ANTHROPIC_API_KEY=sk-ant-...
    codegrapher /path/to/repo

  OpenAI:
    export OPENAI_API_KEY=sk-...
    codegrapher /path/to/repo --provider openai --model gpt-4o

  AWS Bedrock — Claude:
    export AWS_REGION=us-east-1
    codegrapher /path/to/repo --provider bedrock \\
      --model anthropic.claude-3-5-sonnet-20241022-v2:0

  AWS Bedrock — Amazon Nova Pro:
    codegrapher /path/to/repo --provider bedrock \\
      --model amazon.nova-pro-v1:0

  AWS Bedrock — Meta Llama 70B:
    codegrapher /path/to/repo --provider bedrock \\
      --model meta.llama3-1-70b-instruct-v1:0

  AWS Bedrock — Mistral Large:
    codegrapher /path/to/repo --provider bedrock \\
      --model mistral.mistral-large-2402-v1:0

  Ollama (local, no internet needed at runtime):
    ollama serve && ollama pull llama3.2
    codegrapher /path/to/repo --provider ollama --model llama3.2

  Google Gemini:
    export GEMINI_API_KEY=...
    codegrapher /path/to/repo --provider gemini --model gemini-2.0-flash

  Custom / self-hosted (any OpenAI-compatible API):
    codegrapher /path/to/repo \\
      --provider custom \\
      --api-url https://my-llm-server.internal/v1 \\
      --api-key my-secret-key \\
      --model my-model-name

    Works with: vLLM, LM Studio, Together AI, Groq,
                Azure OpenAI, Fireworks, or any OpenAI-compatible endpoint.

─────────────────────────────────────────────────────────────────
 DIAGRAM RENDERING (offline-capable)
─────────────────────────────────────────────────────────────────

  Diagrams are generated from the real graphify graph data — no LLM
  guessing. They are rendered in this priority order:

    1. Playwright + local mermaid.js  (offline, best quality)
       pip install playwright && playwright install chromium
       npm install -g @mermaid-js/mermaid-cli

    2. mmdc CLI                       (offline, needs Chrome configured)

    3. mermaid.ink API                (online fallback)

    4. Pure Python SVG renderer       (always works, zero dependencies)

─────────────────────────────────────────────────────────────────
 AWS CREDENTIALS
─────────────────────────────────────────────────────────────────

  Standard boto3 credential chain:
    export AWS_ACCESS_KEY_ID=...
    export AWS_SECRET_ACCESS_KEY=...
    export AWS_REGION=us-east-1

  Or use AWS profiles:
    aws configure --profile myprofile
    export AWS_PROFILE=myprofile

  Or use IAM instance roles (no env vars needed on EC2/ECS).
""",
    )

    # ── Positional ─────────────────────────────────────────────────────────────
    parser.add_argument(
        "repo_path", type=Path,
        help="Path to the repository folder to analyze",
    )

    # ── LLM provider ──────────────────────────────────────────────────────────
    parser.add_argument(
        "--provider", "-p", default="anthropic",
        choices=["anthropic", "openai", "bedrock", "ollama", "gemini", "custom"],
        help="LLM provider (default: anthropic)",
    )
    parser.add_argument(
        "--model", "-m", default=None,
        help="Model name/ID. Defaults: anthropic=claude-sonnet-4-20250514, "
             "openai=gpt-4o, bedrock=anthropic.claude-3-5-sonnet-20241022-v2:0, "
             "ollama=llama3.2, gemini=gemini-2.0-flash",
    )

    # ── Bedrock ────────────────────────────────────────────────────────────────
    parser.add_argument(
        "--aws-region", default=None, dest="aws_region",
        metavar="REGION",
        help="AWS region for Bedrock (default: AWS_REGION env or us-east-1)",
    )

    # ── Custom provider ────────────────────────────────────────────────────────
    parser.add_argument(
        "--api-url", default=None, dest="api_url",
        metavar="URL",
        help="Base URL for custom/ollama provider (e.g. https://my-llm/v1)",
    )
    parser.add_argument(
        "--api-key", default=None, dest="api_key",
        metavar="KEY",
        help="API key for custom provider (or set CUSTOM_API_KEY env var)",
    )

    # ── Output ─────────────────────────────────────────────────────────────────
    parser.add_argument(
        "--output", "-o", type=Path, default=None,
        metavar="FILE",
        help="Output PDF path (default: <repo_name>-codegrapher.pdf)",
    )

    # ── Agent tuning ───────────────────────────────────────────────────────────
    parser.add_argument(
        "--max-iterations", type=int, default=50, metavar="N",
        help="Max LangGraph agent loop steps (default: 50)",
    )

    # ── Debug ──────────────────────────────────────────────────────────────────
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show detailed agent step output",
    )

    args = parser.parse_args()

    # ── Validate ──────────────────────────────────────────────────────────────
    if not args.repo_path.exists():
        print(f"error: path does not exist: {args.repo_path}", file=sys.stderr)
        sys.exit(1)
    if not args.repo_path.is_dir():
        print(f"error: not a directory: {args.repo_path}", file=sys.stderr)
        sys.exit(1)

    # ── Defaults ──────────────────────────────────────────────────────────────
    if args.output is None:
        repo_name = args.repo_path.resolve().name
        args.output = Path(f"{repo_name}-codegrapher.pdf")

    model_defaults = {
        "anthropic": "claude-sonnet-4-20250514",
        "openai":    "gpt-4o",
        "bedrock":   "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "ollama":    "llama3.2",
        "gemini":    "gemini-2.0-flash",
        "custom":    None,
    }
    if args.model is None:
        args.model = model_defaults.get(args.provider)

    # ── Banner ────────────────────────────────────────────────────────────────
    print()
    print("╔══════════════════════════════════════════════╗")
    print("║         CodeGrapher — Agentic Analyzer       ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"  Repository   : {args.repo_path.resolve()}")
    print(f"  Provider     : {args.provider}")
    print(f"  Model        : {args.model or '(required for custom)'}")
    if args.api_url:
        print(f"  API URL      : {args.api_url}")
    print(f"  Output       : {args.output}")
    print(f"  Max steps    : {args.max_iterations}")
    print()

    from codegrapher.pipeline import run_pipeline
    run_pipeline(args)


if __name__ == "__main__":
    main()
