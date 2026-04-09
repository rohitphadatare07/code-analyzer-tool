# RepoAnalyzer

AI-powered code repository analyzer that generates comprehensive PDF reports with architecture diagrams, flow diagrams, component analysis, and detailed code overviews — using your choice of LLM provider.

## Features

- 🔍 **Deep code analysis** — scans all code, config, and documentation files
- 🤖 **Multi-LLM support** — Anthropic Claude, OpenAI GPT, AWS Bedrock (Claude, Llama, Mistral, Titan, Nova), Ollama (local), Google Gemini
- 📊 **Visual diagrams** — auto-generated architecture, data flow, and component diagrams (Mermaid)
- 📄 **Rich PDF output** — cover page, TOC, executive summary, tech stack, components, API endpoints, file-by-file breakdown, and improvement suggestions
- ⚡ **Fast** — analyzes most repos in under 60 seconds
- 🎯 **CLI-first** — simple command-line interface

## Installation

```bash
pip install -e .

# For AWS Bedrock support:
pip install -e ".[bedrock]"
```

## Usage

```bash
# Basic usage with Anthropic Claude (default)
repoanalyzer /path/to/your/repo

# Specify provider and model
repoanalyzer /path/to/repo --provider anthropic --model claude-sonnet-4-20250514
repoanalyzer /path/to/repo --provider openai --model gpt-4o
repoanalyzer /path/to/repo --provider bedrock --model anthropic.claude-3-5-sonnet-20241022-v2:0
repoanalyzer /path/to/repo --provider ollama --model llama3.2
repoanalyzer /path/to/repo --provider gemini --model gemini-2.0-flash

# Custom output path
repoanalyzer /path/to/repo --output ./reports/myrepo.pdf

# Exclude patterns
repoanalyzer /path/to/repo --exclude "*.test.*" --exclude "migrations"

# Limit scope
repoanalyzer /path/to/repo --max-files 100 --max-file-size 50000

# Verbose output
repoanalyzer /path/to/repo --verbose
```

## Supported LLM Providers

### Anthropic Claude
```bash
export ANTHROPIC_API_KEY=your_key
repoanalyzer . --provider anthropic --model claude-sonnet-4-20250514
```

### OpenAI
```bash
export OPENAI_API_KEY=your_key
repoanalyzer . --provider openai --model gpt-4o
```

### AWS Bedrock
Supports: Claude, Llama, Mistral, Amazon Titan, Amazon Nova
```bash
export AWS_REGION=us-east-1
export AWS_ACCESS_KEY_ID=your_key
export AWS_SECRET_ACCESS_KEY=your_secret

# Claude on Bedrock
repoanalyzer . --provider bedrock --model anthropic.claude-3-5-sonnet-20241022-v2:0

# Llama on Bedrock
repoanalyzer . --provider bedrock --model meta.llama3-70b-instruct-v1:0

# Mistral on Bedrock
repoanalyzer . --provider bedrock --model mistral.mixtral-8x7b-instruct-v0:1

# Amazon Nova
repoanalyzer . --provider bedrock --model amazon.nova-pro-v1:0
```

Or use IAM roles / AWS profiles — boto3 uses standard AWS credential chain.

### Ollama (Local)
```bash
ollama serve
ollama pull llama3.2
repoanalyzer . --provider ollama --model llama3.2
```

### Google Gemini
```bash
export GEMINI_API_KEY=your_key
repoanalyzer . --provider gemini --model gemini-2.0-flash
```

## PDF Report Structure

1. **Cover Page** — repo name, purpose, stats (files, lines, languages)
2. **Table of Contents**
3. **Executive Summary** — purpose, design patterns, testing, deployment
4. **Technology Stack** — tech badges, dependency list, language breakdown
5. **Architecture Overview** — style, data flow, database models
6. **System Architecture Diagram** — auto-generated Mermaid diagram
7. **Data Flow Diagram** — sequence diagram of main flows
8. **Component Diagram** — module dependency graph
9. **Key Components** — description, responsibilities, dependencies per component
10. **API Endpoints** — table of all detected endpoints
11. **Code Quality & Security** — quality observations, security notes
12. **File-by-File Analysis** — purpose, classes, functions, complexity for each file
13. **Improvement Recommendations** — actionable suggestions
14. **Repository File Tree** — full directory structure

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `repo_path` | (required) | Path to the repository |
| `--provider` | `anthropic` | LLM provider |
| `--model` | provider default | Model name/ID |
| `--output` | `<repo>-report.pdf` | Output PDF path |
| `--exclude` | none | Glob patterns to exclude (repeatable) |
| `--max-files` | 200 | Max files to analyze |
| `--max-file-size` | 100000 | Skip files larger than N bytes |
| `--ollama-url` | `http://localhost:11434` | Ollama server URL |
| `--verbose` | false | Detailed progress output |

## Requirements

- Python 3.9+
- `reportlab` (PDF generation)
- `boto3` (optional, for AWS Bedrock)
- Network access to mermaid.ink for diagram rendering (falls back gracefully)

## Architecture

```
repoanalyzer/
├── __main__.py      # CLI entry point & argument parsing
├── providers.py     # LLM provider abstraction (Anthropic, OpenAI, Bedrock, Ollama, Gemini)
├── scanner.py       # Repository file walker & content collector
├── analyzer.py      # LLM-powered analysis pipeline
├── diagrams.py      # Mermaid → PNG conversion
├── pdf_generator.py # ReportLab PDF builder
└── pipeline.py      # Orchestration
```
