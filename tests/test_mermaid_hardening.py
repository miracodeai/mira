"""Tests for the Mermaid hardening guard (``mira.llm.mermaid``).

The guard exists because LLM-generated diagrams reach GitHub inside a
```` ```mermaid ```` fence, where anything the parser rejects renders as
"Unable to render rich display". No prompt instruction reliably prevents
the breakage, so ``harden_mermaid`` validates against a strict grammar
subset and either re-emits a canonical, parser-safe diagram or returns
``None`` (→ the diagram is dropped from the comment).
"""

from __future__ import annotations

from mira.llm.mermaid import harden_mermaid
from mira.models import WalkthroughResult


class TestHardenMermaidFlowcharts:
    def test_repairs_nested_quotes_single_node(self):
        """PR#41 failure mode: `engine["core/"engine.py""]`."""
        out = harden_mermaid('graph LR\n    engine["core/"engine.py""]\n')
        assert out is not None
        assert 'engine["core/engine.py"]' in out

    def test_repairs_nested_quotes_multiple_nodes(self):
        out = harden_mermaid('graph LR\n    a["x/"a.py""]\n    b["y/"b.py""]\n    a --> b\n')
        assert out is not None
        assert 'a["x/a.py"]' in out
        assert 'b["y/b.py"]' in out

    def test_repairs_label_with_extra_garbage(self):
        """Real failure mode from PR#41 — quotes interleaved with path text."""
        out = harden_mermaid('graph LR\n    prompts["llm/prompts/"review.py" + "footguns.py""]\n')
        assert out is not None
        assert 'prompts["llm/prompts/review.py + footguns.py"]' in out

    def test_repairs_literal_backslash_n(self):
        """The reported GitHub failure: double-escaped newlines in the JSON
        string arrive as a literal ``\\n``, producing
        ``graph LR\\n siteSettings["\\w ...`` — one line, backslash in the
        label — which makes Mermaid's lexer emit NODE_STRING instead of
        NEWLINE and GitHub show "Unable to render rich display"."""
        raw = 'graph LR\\n siteSettings["\\w site-settings"] --> theme'
        out = harden_mermaid(raw)
        assert out is not None
        # Real newlines, clean label, structure intact.
        assert out.startswith("graph LR\n")
        assert 'siteSettings["w site-settings"]' in out
        assert "-->" in out
        assert "\\" not in out

    def test_repairs_crlf_and_strips_fences(self):
        raw = '```mermaid\r\ngraph LR\r\n  A["a"] --> B["b"]\r\n```'
        out = harden_mermaid(raw)
        assert out == 'graph LR\n  A["a"] --> B["b"]'

    def test_preserves_well_formed_diagram(self):
        raw = 'graph LR\n    engine["core/engine.py"]\n    engine --> jit'
        out = harden_mermaid(raw)
        assert out is not None
        assert out == 'graph LR\n  engine["core/engine.py"]\n  engine --> jit'

    def test_preserves_unquoted_node_ids(self):
        out = harden_mermaid("graph LR\n    a --> b\n    b --> c")
        assert out == "graph LR\n  a --> b\n  b --> c"

    def test_preserves_flowchart_keyword_and_direction(self):
        out = harden_mermaid('flowchart TB\n  A["a"] --> B["b"]')
        assert out is not None
        assert out.startswith("graph TB")

    def test_preserves_subgraphs(self):
        raw = 'graph LR\n  subgraph Backend\n    A["a"] --> B["b"]\n  end\n  C["c"] --> A'
        out = harden_mermaid(raw)
        assert out is not None
        assert 'subgraph "Backend"' in out
        assert [ln for ln in out.splitlines() if ln == "end"] == ["end"]

    def test_preserves_node_shapes(self):
        raw = 'graph LR\n  A["start"] --> B("rounded") --> C{"decision?"} --> D["done"]'
        out = harden_mermaid(raw)
        assert out is not None
        assert 'B("rounded")' in out
        assert 'C{"decision?"}' in out

    def test_preserves_edge_labels_and_arrow_variants(self):
        raw = (
            'graph LR\n  A["a"] -->|imports| B["b"]\n'
            '  C["c"] -- uses --> D["d"]\n'
            '  E["e"] -.-> F["f"]\n  G["g"] ==> H["h"]'
        )
        out = harden_mermaid(raw)
        assert out is not None
        assert "-->|imports|" in out
        assert "-- uses -->" in out
        assert "-.->" in out
        assert "==>" in out

    def test_keeps_compact_arrow_typography(self):
        """`A-->B` is the most common Mermaid typography. A dash-swallowing
        node id (`A--`) used to leave `>B`, fail the edge regex, and drop
        the whole diagram; the id must stop before the arrow's dashes."""
        assert harden_mermaid("graph LR\n  A-->B") == "graph LR\n  A --> B"

    def test_keeps_compact_arrow_chain(self):
        assert harden_mermaid("graph LR\n  A-->B-->C") == "graph LR\n  A --> B --> C"

    def test_keeps_compact_arrow_with_open_head(self):
        assert harden_mermaid("graph LR\n  A--o B") == "graph LR\n  A --o B"

    def test_keeps_dashed_node_id_before_arrow(self):
        """A dash *inside* a node id (`a-b`) is part of the id, but the
        arrow's dashes never join it."""
        assert harden_mermaid("graph LR\n  a-b-->c") == "graph LR\n  a-b --> c"

    def test_keeps_dashed_node_id_with_label(self):
        out = harden_mermaid('graph LR\n  a-b["x"] --> c')
        assert out == 'graph LR\n  a-b["x"] --> c'

    def test_drops_reserved_end_as_node_id(self):
        """`end` is Mermaid's reserved subgraph terminator: re-emitting it
        as a bare node id yields a diagram the parser rejects. A plain node
        named `end` has no label to quote it as, so the diagram is
        rejected rather than guessed (matches the module's conservative
        None-on-doubt policy)."""
        assert harden_mermaid("graph LR\n  A --> end") is None
        assert harden_mermaid("graph LR\n  end --> A") is None

    def test_keeps_multiword_text_edges(self):
        """`-- label -->` text-form arrows: a multi-word label used to hit a
        dead regex branch and drop the diagram; it must round-trip."""
        out = harden_mermaid("graph LR\n  A -- reads and writes --> B")
        assert out == "graph LR\n  A -- reads and writes --> B"

    def test_keeps_text_edge_arrowheads_and_rejects_invalid(self):
        """A text edge may carry one arrowhead (`--o`/`--x`/`-->`); a
        doubled head (`-->o`) is not a valid Mermaid closing token."""
        assert harden_mermaid("graph LR\n  A -- uses -->o B") is None
        assert harden_mermaid("graph LR\n  A -- uses --o B") == "graph LR\n  A -- uses --o B"

    def test_dash_runs_stay_links_not_text_edges(self):
        """A plain run of dashes is a link, never a text edge: the text-edge
        branch must not swallow it with a dash as its 'label'."""
        out = harden_mermaid("graph LR\n  A --- B\n  C ---- D")
        assert out == "graph LR\n  A --- B\n  C ---- D"

    def test_preserves_classdef_and_classes(self):
        raw = 'graph LR\n  classDef cls fill:#f00\n  A["a"]:::cls --> B["b"]'
        out = harden_mermaid(raw)
        assert out is not None
        assert "classDef cls" in out
        assert 'A["a"]:::cls' in out

    def test_preserves_comments_and_blank_lines(self):
        raw = 'graph LR\n  %% wiring\n\n  A["a"] --> B["b"]'
        out = harden_mermaid(raw)
        assert out == 'graph LR\n  A["a"] --> B["b"]'

    def test_drops_garbage(self):
        assert harden_mermaid("hello world\n  foo bar") is None

    def test_drops_unbalanced_label(self):
        assert harden_mermaid('graph LR\n  a["oops --> b') is None

    def test_drops_dangling_edge(self):
        assert harden_mermaid('graph LR\n  A["a"] -->') is None

    def test_drops_header_only(self):
        assert harden_mermaid("graph LR") is None

    def test_drops_end_without_subgraph(self):
        assert harden_mermaid('graph LR\n  end\n  A["a"]') is None

    def test_drops_unbalanced_subgraph(self):
        assert harden_mermaid('graph LR\n  subgraph X\n  A["a"]') is None

    def test_drops_empty_and_none(self):
        assert harden_mermaid("") is None
        assert harden_mermaid(None) is None
        assert harden_mermaid("   \n  ") is None

    def test_drops_unknown_diagram_type(self):
        assert harden_mermaid("gantt\n  title X\n  a: 2024-01-01, 1d") is None

    def test_drops_oversized_diagram(self):
        raw = "graph LR\n" + "\n".join(f'A{i}["x"] --> B{i}' for i in range(60))
        assert harden_mermaid(raw) is None


class TestHardenMermaidSequence:
    def test_preserves_simple_messages(self):
        raw = "sequenceDiagram\n  A->>B: call\n  B-->>A: reply"
        out = harden_mermaid(raw)
        assert out is not None
        assert out.startswith("sequenceDiagram\n")
        assert "A->>B: call" in out
        assert "B-->>A: reply" in out

    def test_drops_stray_deactivate_marker(self):
        """`B-->>-A` on an inactive participant throws at render time —
        the marker is dropped, the message kept."""
        raw = "sequenceDiagram\n  A->>B: call\n  B-->>-A: done"
        out = harden_mermaid(raw)
        assert out is not None
        assert "B-->>A: done" in out
        assert "-" not in out.split("B-->>A: done")[1]

    def test_keeps_balanced_activation(self):
        # `A->>+B` activates the receiver B; the reply `B-->>-A` deactivates
        # its sender B. Both markers are provably safe and kept.
        raw = "sequenceDiagram\n  A->>+B: call\n  B-->>-A: done"
        out = harden_mermaid(raw)
        assert out is not None
        assert "A->>+B: call" in out
        assert "B-->>-A: done" in out

    def test_drops_unbalanced_deactivate(self):
        # `A->>+B` activates B, but the reply's `-` deactivates the sender
        # (A), which was never active — keep the message, drop the marker.
        raw = "sequenceDiagram\n  A->>+B: call\n  A-->>-B: done"
        out = harden_mermaid(raw)
        assert out is not None
        assert "A->>+B: call" in out
        assert "A-->>B: done" in out

    def test_drops_activate_deactivate_statements(self):
        raw = "sequenceDiagram\n  activate A\n  A->>B: hi\n  deactivate A"
        out = harden_mermaid(raw)
        assert out is not None
        assert "activate" not in out
        assert "A->>B: hi" in out

    def test_drops_sender_side_marker(self):
        """`A+->>B` (activation marker on the sender) is not valid Mermaid."""
        assert harden_mermaid("sequenceDiagram\n  A+->>B: call") is None

    def test_drops_message_without_receiver(self):
        assert harden_mermaid("sequenceDiagram\n  A->: hi") is None

    def test_preserves_blocks_and_participants(self):
        raw = (
            "sequenceDiagram\n"
            "  participant A as Alice\n"
            "  A->>B: call\n"
            "  loop every time\n"
            "  B-->>A: ok\n"
            "  end\n"
            "  alt cond\n"
            "  A->>C: x\n"
            "  else\n"
            "  A->>C: y\n"
            "  end"
        )
        out = harden_mermaid(raw)
        assert out is not None
        for frag in ("participant A as Alice", "loop every time", "alt cond", "else"):
            assert frag in out

    def test_drops_unbalanced_blocks(self):
        assert harden_mermaid("sequenceDiagram\n  A->>B: hi\n  loop x\n  end\n  end") is None


class TestWalkthroughMarkdownDiagram:
    """End-to-end: broken diagrams must not reach the posted comment."""

    def _markdown_with(self, diagram: str | None) -> str:
        return WalkthroughResult(summary="S.", sequence_diagram=diagram).to_markdown()

    def test_good_diagram_fenced(self):
        md = self._markdown_with('graph LR\n  engine["core/engine.py"] --> jit')
        assert "```mermaid" in md
        assert 'engine["core/engine.py"]' in md

    def test_broken_diagram_dropped(self):
        md = self._markdown_with("not mermaid at all\n  foo bar")
        assert "```mermaid" not in md

    def test_literal_backslash_n_repaired_not_dropped(self):
        md = self._markdown_with('graph LR\\n siteSettings["\\w s"] --> theme')
        assert "```mermaid" in md
        assert "\\" not in md

    def test_nested_quotes_repaired_not_dropped(self):
        md = self._markdown_with('graph LR\n  engine["core/"engine.py""] --> jit')
        assert "```mermaid" in md
        assert 'engine["core/engine.py"]' in md

    def test_no_diagram_no_fence(self):
        md = self._markdown_with(None)
        assert "```mermaid" not in md

    def test_sequence_diagram_still_fenced(self):
        md = self._markdown_with("sequenceDiagram\n  A->>B: call")
        assert "```mermaid" in md
        assert "sequenceDiagram" in md
