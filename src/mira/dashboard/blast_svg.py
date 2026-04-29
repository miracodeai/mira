"""Generate blast radius SVG as a knowledge graph with connected nodes."""

from __future__ import annotations

import math
import random

_COLORS = {
    "text": "#e4e4e7",
    "text_muted": "#71717a",
    "bg": "#09090b",
    "edge": "#27272a",
    "edge_highlight": "#3f3f46",
}

_NODE_STYLES = {
    "changed": ("#f97316", "#431407", "#f9731640"),  # orange
    "internal": ("#3b82f6", "#172554", "#3b82f640"),  # blue
    "cross-repo": ("#8b5cf6", "#2e1065", "#8b5cf640"),  # purple
}


def generate_blast_svg(
    changed_files: list[str],
    internal_deps: list[str],
    cross_repo_deps: list[str],
    edges: list[tuple[str, str]] | None = None,
    width: int = 600,
    height: int = 380,
) -> str:
    cx, cy = width // 2, height // 2 - 10

    # Build node list with positions
    nodes: list[dict] = []
    node_map: dict[str, dict] = {}

    # Seed random for consistent layout per set of inputs
    seed = hash(tuple(changed_files + internal_deps + cross_repo_deps)) % 10000
    rng = random.Random(seed)

    # Place changed files in centre cluster
    for i, f in enumerate(changed_files[:4]):
        spread = 35
        angle = (2 * math.pi * i / max(len(changed_files[:4]), 1)) - math.pi / 2
        r = spread if len(changed_files) > 1 else 0
        x = cx + r * math.cos(angle) + rng.uniform(-5, 5)
        y = cy + r * math.sin(angle) + rng.uniform(-5, 5)
        node = {"x": x, "y": y, "label": _short(f), "kind": "changed", "id": f}
        nodes.append(node)
        node_map[f] = node

    # Place internal deps in middle zone
    r_int = 100
    for i, f in enumerate(internal_deps[:6]):
        n = max(len(internal_deps[:6]), 1)
        angle = (2 * math.pi * i / n) - math.pi / 2 + rng.uniform(-0.2, 0.2)
        r = r_int + rng.uniform(-15, 15)
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        node = {"x": x, "y": y, "label": _short(f), "kind": "internal", "id": f}
        nodes.append(node)
        node_map[f] = node

    # Place cross-repo in outer zone
    r_cross = 155
    for i, f in enumerate(cross_repo_deps[:4]):
        n = max(len(cross_repo_deps[:4]), 1)
        angle = (2 * math.pi * i / n) + rng.uniform(-0.15, 0.15)
        r = r_cross + rng.uniform(-10, 10)
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        node = {"x": x, "y": y, "label": _short(f), "kind": "cross-repo", "id": f}
        nodes.append(node)
        node_map[f] = node

    # Build edges — connect each dependent to the nearest changed file
    graph_edges: list[tuple[dict, dict, str]] = []

    for node in nodes:
        if node["kind"] == "internal":
            # Connect to nearest changed node
            nearest = _nearest(node, [n for n in nodes if n["kind"] == "changed"])
            if nearest:
                graph_edges.append((node, nearest, "internal"))
        elif node["kind"] == "cross-repo":
            # Connect to nearest internal or changed node
            targets = [n for n in nodes if n["kind"] in ("changed", "internal")]
            nearest = _nearest(node, targets)
            if nearest:
                graph_edges.append((node, nearest, "cross-repo"))

    # Also add edges between changed files
    changed_nodes = [n for n in nodes if n["kind"] == "changed"]
    for i in range(len(changed_nodes) - 1):
        graph_edges.append((changed_nodes[i], changed_nodes[i + 1], "internal"))

    # Use provided edges if available
    if edges:
        for src, tgt in edges:
            if src in node_map and tgt in node_map:
                graph_edges.append((node_map[src], node_map[tgt], "internal"))

    # ── Render SVG ──
    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}">'
    )
    parts.append(f'<rect width="{width}" height="{height}" fill="{_COLORS["bg"]}" rx="12"/>')

    # Background mesh — faint hexagonal grid for depth
    parts.append(_bg_mesh(width, height, rng))

    # Edges
    for n1, n2, kind in graph_edges:
        stroke = _NODE_STYLES.get(kind, _NODE_STYLES["internal"])[2]
        parts.append(
            f'<line x1="{n1["x"]}" y1="{n1["y"]}" x2="{n2["x"]}" y2="{n2["y"]}" '
            f'stroke="{stroke}" stroke-width="1.5"/>'
        )

    # Glow behind changed nodes
    for node in nodes:
        if node["kind"] == "changed":
            parts.append(
                f'<circle cx="{node["x"]}" cy="{node["y"]}" r="20" '
                f'fill="{_NODE_STYLES["changed"][2]}" opacity="0.4"/>'
            )

    # Nodes
    for node in nodes:
        parts.append(_render_node(node))

    # Legend
    ly = height - 16
    lx = 16
    for kind, count, label in [
        ("changed", len(changed_files), "core"),
        ("internal", len(internal_deps), "dependent"),
        ("cross-repo", len(cross_repo_deps), "cross-repo"),
    ]:
        if count == 0:
            continue
        stroke = _NODE_STYLES[kind][0]
        parts.append(f'<circle cx="{lx}" cy="{ly}" r="4" fill="{stroke}"/>')
        text = f"{count} {label}"
        parts.append(
            f'<text x="{lx + 10}" y="{ly + 3.5}" fill="{_COLORS["text_muted"]}" font-size="10" font-family="system-ui">{text}</text>'
        )
        lx += len(text) * 6.5 + 28

    parts.append(
        f'<text x="{width - 14}" y="20" fill="{_COLORS["text_muted"]}" font-size="10" font-family="system-ui" text-anchor="end">Blast Radius</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts)


def _bg_mesh(width: int, height: int, rng: random.Random) -> str:
    """Draw a subtle background mesh for depth."""
    parts = ['<g opacity="0.08">']
    # Scattered dots
    for _ in range(40):
        x = rng.uniform(30, width - 30)
        y = rng.uniform(30, height - 40)
        r = rng.uniform(1, 2)
        parts.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="#a1a1aa"/>')
    # A few connecting lines between nearby dots
    dots = [(rng.uniform(30, width - 30), rng.uniform(30, height - 40)) for _ in range(20)]
    for i in range(len(dots)):
        for j in range(i + 1, len(dots)):
            dx = dots[i][0] - dots[j][0]
            dy = dots[i][1] - dots[j][1]
            dist = math.sqrt(dx * dx + dy * dy)
            if dist < 80:
                parts.append(
                    f'<line x1="{dots[i][0]}" y1="{dots[i][1]}" '
                    f'x2="{dots[j][0]}" y2="{dots[j][1]}" '
                    f'stroke="#a1a1aa" stroke-width="0.5"/>'
                )
    parts.append("</g>")
    return "\n".join(parts)


def _nearest(node: dict, targets: list[dict]) -> dict | None:
    if not targets:
        return None
    return min(targets, key=lambda t: (t["x"] - node["x"]) ** 2 + (t["y"] - node["y"]) ** 2)


def _render_node(node: dict) -> str:
    x, y = node["x"], node["y"]
    label = node["label"]
    kind = node["kind"]
    stroke, bg, _ = _NODE_STYLES.get(kind, ("#71717a", "#27272a", "#71717a40"))

    # Square with rounded corners (like the reference image)
    size = max(len(label) * 6 + 12, 44)
    half = size / 2

    return (
        f'<rect x="{x - half}" y="{y - 11}" width="{size}" height="22" '
        f'rx="4" fill="{bg}" stroke="{stroke}" stroke-width="1.5"/>'
        f'<text x="{x}" y="{y + 3.5}" fill="{_COLORS["text"]}" font-size="9" '
        f'font-family="system-ui, monospace" text-anchor="middle">{label}</text>'
    )


def _short(name: str) -> str:
    if "/" in name:
        name = name.split("/")[-1]
    if len(name) > 14:
        ext = ""
        if "." in name:
            base, ext = name.rsplit(".", 1)
            ext = "." + ext
        max_base = 14 - len(ext) - 1
        name = name[:max_base] + "…" + ext
    return name
