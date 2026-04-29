# codegrapher.core package
"""
codegrapher.core — graphify AST extraction + graph analysis libraries.

These are the vendored graphify library modules that power the agent's tools:
  detect   — file discovery and classification
  extract  — tree-sitter AST extraction (15+ languages)
  build    — NetworkX graph assembly
  cluster  — Leiden/Louvain community detection
  analyze  — god nodes, surprising connections, suggested questions
  cache    — per-file extraction cache (skip unchanged files)
  validate — graph schema validation
  security — safe URL fetching, path traversal guards
"""