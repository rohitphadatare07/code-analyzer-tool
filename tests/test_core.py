"""
Unit tests for codegrapher.core — graph pipeline without LLM.

Run: pytest tests/test_core.py -v
"""
import pytest
import networkx as nx
from pathlib import Path


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_graph():
    """A small realistic graph for testing."""
    G = nx.Graph()
    nodes = [
        ("cli",      {"label": "__main__",  "source_file": "app/__main__.py"}),
        ("router",   {"label": "Router",    "source_file": "app/router.py"}),
        ("handler",  {"label": "Handler",   "source_file": "app/handler.py"}),
        ("db",       {"label": "Database",  "source_file": "app/db.py"}),
        ("models",   {"label": "Models",    "source_file": "app/models.py"}),
        ("auth",     {"label": "AuthService","source_file": "app/auth.py"}),
        ("cache",    {"label": "Cache",     "source_file": "app/cache.py"}),
    ]
    for nid, attrs in nodes:
        G.add_node(nid, **attrs)
    edges = [
        ("cli", "router", "calls"),
        ("router", "handler", "calls"),
        ("handler", "db", "calls"),
        ("handler", "auth", "calls"),
        ("db", "models", "uses"),
        ("auth", "cache", "calls"),
        ("router", "auth", "calls"),
    ]
    for u, v, rel in edges:
        G.add_edge(u, v, relation=rel, confidence="EXTRACTED", confidence_score=1.0)
    return G


@pytest.fixture
def sample_communities():
    return {
        0: ["cli", "router"],
        1: ["handler", "auth", "cache"],
        2: ["db", "models"],
    }


@pytest.fixture
def sample_community_labels():
    return {0: "Entry", 1: "Business Logic", 2: "Data"}


@pytest.fixture
def sample_cohesion():
    return {0: 1.0, 1: 0.33, 2: 1.0}


# ── Core: build ───────────────────────────────────────────────────────────────

class TestBuild:
    def test_build_from_json_basic(self):
        from codegrapher.core.build import build_from_json
        data = {
            "nodes": [
                {"id": "a", "label": "A", "source_file": "a.py"},
                {"id": "b", "label": "B", "source_file": "b.py"},
            ],
            "edges": [
                {"source": "a", "target": "b", "relation": "calls",
                 "confidence": "EXTRACTED", "confidence_score": 1.0},
            ],
        }
        G = build_from_json(data)
        assert G.number_of_nodes() == 2
        assert G.number_of_edges() == 1

    def test_build_skips_dangling_edges(self):
        from codegrapher.core.build import build_from_json
        data = {
            "nodes": [{"id": "a", "label": "A", "source_file": "a.py"}],
            "edges": [
                {"source": "a", "target": "nonexistent", "relation": "imports",
                 "confidence": "EXTRACTED", "confidence_score": 1.0},
            ],
        }
        G = build_from_json(data)
        assert G.number_of_nodes() == 1
        assert G.number_of_edges() == 0

    def test_build_preserves_edge_direction(self):
        from codegrapher.core.build import build_from_json
        data = {
            "nodes": [
                {"id": "src", "label": "Src", "source_file": "s.py"},
                {"id": "tgt", "label": "Tgt", "source_file": "t.py"},
            ],
            "edges": [
                {"source": "src", "target": "tgt", "relation": "calls",
                 "confidence": "EXTRACTED", "confidence_score": 1.0},
            ],
        }
        G = build_from_json(data)
        edge_data = G.edges["src", "tgt"]
        assert edge_data["_src"] == "src"
        assert edge_data["_tgt"] == "tgt"


# ── Core: cluster ─────────────────────────────────────────────────────────────

class TestCluster:
    def test_cluster_returns_communities(self, sample_graph):
        from codegrapher.core.cluster import cluster
        communities = cluster(sample_graph)
        assert isinstance(communities, dict)
        assert len(communities) >= 1
        all_nodes = {n for nodes in communities.values() for n in nodes}
        assert all_nodes == set(sample_graph.nodes())

    def test_cluster_empty_graph(self):
        from codegrapher.core.cluster import cluster
        G = nx.Graph()
        communities = cluster(G)
        assert communities == {}

    def test_cohesion_score_complete_graph(self):
        from codegrapher.core.cluster import cohesion_score
        G = nx.complete_graph(4)
        nx.relabel_nodes(G, {i: f"n{i}" for i in range(4)}, copy=False)
        score = cohesion_score(G, list(G.nodes()))
        assert score == 1.0

    def test_cohesion_score_single_node(self):
        from codegrapher.core.cluster import cohesion_score
        G = nx.Graph()
        G.add_node("a")
        score = cohesion_score(G, ["a"])
        assert score == 1.0


# ── Core: analyze ─────────────────────────────────────────────────────────────

class TestAnalyze:
    def test_god_nodes_returns_top_n(self, sample_graph):
        from codegrapher.core.analyze import god_nodes
        result = god_nodes(sample_graph, top_n=3)
        assert len(result) <= 3
        assert all("label" in n and "edges" in n for n in result)

    def test_god_nodes_sorted_by_degree(self, sample_graph):
        from codegrapher.core.analyze import god_nodes
        result = god_nodes(sample_graph, top_n=5)
        degrees = [n["edges"] for n in result]
        assert degrees == sorted(degrees, reverse=True)

    def test_surprising_connections(self, sample_graph, sample_communities):
        from codegrapher.core.analyze import surprising_connections
        result = surprising_connections(sample_graph, sample_communities, top_n=3)
        assert isinstance(result, list)

    def test_graph_diff_detects_new_nodes(self):
        from codegrapher.core.analyze import graph_diff
        G_old = nx.Graph()
        G_old.add_node("a", label="A")
        G_new = nx.Graph()
        G_new.add_node("a", label="A")
        G_new.add_node("b", label="B")
        diff = graph_diff(G_old, G_new)
        assert len(diff["new_nodes"]) == 1
        assert diff["new_nodes"][0]["id"] == "b"


# ── Output: mermaid_converter ─────────────────────────────────────────────────

class TestMermaidConverter:
    def test_architecture_diagram_basic(self, sample_graph, sample_communities,
                                        sample_community_labels):
        from codegrapher.output.mermaid_converter import build_architecture_diagram
        code = build_architecture_diagram(
            G=sample_graph,
            communities=sample_communities,
            community_labels=sample_community_labels,
            god_node_ids={"cli", "router"},
        )
        assert "graph TD" in code
        assert "subgraph" in code

    def test_flow_diagram_basic(self, sample_graph, sample_communities,
                                sample_community_labels):
        from codegrapher.output.mermaid_converter import build_flow_diagram
        code = build_flow_diagram(
            G=sample_graph,
            communities=sample_communities,
            community_labels=sample_community_labels,
            god_node_ids={"cli"},
        )
        assert "sequenceDiagram" in code
        assert "participant" in code

    def test_component_diagram_basic(self, sample_graph, sample_communities,
                                     sample_community_labels, sample_cohesion):
        from codegrapher.output.mermaid_converter import build_component_diagram
        code = build_component_diagram(
            G=sample_graph,
            communities=sample_communities,
            community_labels=sample_community_labels,
            cohesion_scores=sample_cohesion,
            god_node_ids={"cli"},
        )
        assert "graph LR" in code

    def test_generate_all_diagrams(self, sample_graph, sample_communities,
                                   sample_community_labels, sample_cohesion):
        from codegrapher.output.mermaid_converter import generate_all_diagrams
        state = {
            "graph": sample_graph,
            "communities": sample_communities,
            "community_labels": sample_community_labels,
            "cohesion": sample_cohesion,
            "god_nodes": [{"id": "cli", "label": "__main__", "edges": 1}],
        }
        diagrams = generate_all_diagrams(state)
        assert len(diagrams) == 3
        types = {d["diagram_type"] for d in diagrams}
        assert types == {"architecture", "flow", "components"}

    def test_safe_id_sanitization(self):
        from codegrapher.output.mermaid_converter import _safe_id
        assert _safe_id("build_from_json()") == "build_from_json"
        assert _safe_id("123-bad") == "n_123_bad"
        assert " " not in _safe_id("has spaces")

    def test_fallback_on_empty_graph(self):
        from codegrapher.output.mermaid_converter import generate_all_diagrams
        state = {
            "graph": None,
            "communities": {},
            "community_labels": {},
            "cohesion": {},
            "god_nodes": [],
        }
        diagrams = generate_all_diagrams(state)
        assert len(diagrams) == 3
        for d in diagrams:
            assert len(d["mermaid_code"]) > 0


# ── Output: diagrams rendering ────────────────────────────────────────────────

class TestDiagramRendering:
    def test_always_returns_bytes(self):
        from codegrapher.output.diagrams import mermaid_to_png_bytes
        code = "graph TD\n  A --> B\n  B --> C"
        result = mermaid_to_png_bytes(code)
        assert isinstance(result, bytes)
        assert len(result) > 100

    def test_empty_code_returns_placeholder(self):
        from codegrapher.output.diagrams import mermaid_to_png_bytes
        result = mermaid_to_png_bytes("")
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_sequence_diagram_svg_fallback(self):
        from codegrapher.output.diagrams import _svg_sequence
        code = """sequenceDiagram
  participant A as Alice
  participant B as Bob
  A->>B: Hello
  B-->>A: Hi there"""
        svg = _svg_sequence(code)
        assert "<svg" in svg
        assert "Alice" in svg
        assert "Bob" in svg

    def test_graph_svg_fallback_td(self):
        from codegrapher.output.diagrams import _svg_graph
        code = "graph TD\n  A[Start] --> B[End]"
        svg = _svg_graph(code, "TD")
        assert "<svg" in svg
        assert "Start" in svg

    def test_graph_svg_fallback_lr(self):
        from codegrapher.output.diagrams import _svg_graph
        code = "graph LR\n  A --> B --> C"
        svg = _svg_graph(code, "LR")
        assert "<svg" in svg

    def test_is_svg_detection(self):
        from codegrapher.output.diagrams import is_svg
        assert is_svg(b"<svg xmlns...")
        assert not is_svg(b"\x89PNG\r\n")


# ── Integration: PDF generation ───────────────────────────────────────────────

class TestPDFGeneration:
    def test_pdf_generates_without_error(self, tmp_path):
        from codegrapher.output.pdf_generator import generate_pdf
        from codegrapher.output.mermaid_converter import generate_all_diagrams

        G = nx.Graph()
        G.add_node("a", label="A", source_file="main.py")
        G.add_node("b", label="B", source_file="utils.py")
        G.add_edge("a", "b", relation="calls", confidence="EXTRACTED", confidence_score=1.0)

        state = {
            "graph": G,
            "communities": {0: ["a"], 1: ["b"]},
            "community_labels": {0: "Main", 1: "Utils"},
            "cohesion": {0: 0.0, 1: 0.0},
            "god_nodes": [{"id": "a", "label": "A", "edges": 1}],
            "surprising_connections": [],
            "findings": [
                {"category": "tech_stack", "title": "Python",
                 "detail": "Main language", "files": [], "confidence": "high"},
            ],
            "finish_data": {
                "summary": "A test repo.",
                "purpose": "Testing PDF generation.",
                "architecture_style": "Library",
                "tech_stack": ["Python"],
                "key_components": [],
                "data_flow": ["Input → Output"],
                "api_endpoints": [],
                "database_models": [],
                "security_notes": [],
                "improvement_suggestions": [],
                "testing_approach": "pytest",
                "deployment_info": "pip install",
                "code_quality_notes": [],
            },
            "scan_result": {"total_files": 2, "directory_tree": "repo/\n  main.py"},
        }

        diagrams = generate_all_diagrams(state)
        state["diagrams"] = diagrams

        out = tmp_path / "test-report.pdf"
        generate_pdf(
            output_path=out,
            repo_name="test-repo",
            provider_name="test/model",
            state_accumulator=state,
            directory_tree="repo/\n  main.py",
            total_files=2,
            total_lines=100,
            agent_steps=5,
            agent_tool_calls=8,
            elapsed_seconds=10.0,
        )
        assert out.exists()
        assert out.stat().st_size > 5_000
