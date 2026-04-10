from setuptools import setup, find_packages

with open("README.md", encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="codegrapher",
    version="1.0.0",
    description=(
        "Agentic repository analyzer — LangGraph ReAct loop, graphify AST/graph "
        "extraction, offline Mermaid diagrams, multi-LLM support, PDF report."
    ),
    long_description=long_description,
    long_description_content_type="text/markdown",
    python_requires=">=3.10",
    packages=find_packages(),
    install_requires=[
        # ── LangGraph agentic orchestration ──────────────────────────────────
        "langgraph>=0.2.0",
        "langchain-core>=0.3.0",

        # ── LLM providers (install only what you need) ────────────────────────
        "langchain-anthropic>=0.3.0",   # Anthropic Claude
        "langchain-openai>=0.2.0",       # OpenAI + any OpenAI-compatible API

        # ── Graph analysis (graphify core deps) ───────────────────────────────
        "networkx>=3.0",                 # graph construction + algorithms
        "tree-sitter>=0.21.0",           # AST parsing core
        # tree-sitter language bindings (installed separately — see README)

        # ── PDF generation ────────────────────────────────────────────────────
        "reportlab>=4.0.0",

        # ── Diagram rendering ─────────────────────────────────────────────────
        "playwright>=1.40.0",            # headless browser for offline Mermaid
        # Note: after pip install, run: playwright install chromium
    ],
    extras_require={
        # AWS Bedrock support
        "bedrock": [
            "boto3>=1.26.0",
            "langchain-aws>=0.2.0",
        ],
        # Google Gemini support
        "gemini": [
            "langchain-google-genai>=2.0.0",
        ],
        # All providers
        "all": [
            "boto3>=1.26.0",
            "langchain-aws>=0.2.0",
            "langchain-google-genai>=2.0.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "codegrapher=codegrapher.__main__:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Topic :: Software Development :: Libraries",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
    ],
)
