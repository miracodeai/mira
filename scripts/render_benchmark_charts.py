#!/usr/bin/env python3
"""Render the README benchmark charts from .github/assets/benchmark-results.json.

Two charts, each in a light and a dark variant (README picks one with a
<picture> element), pure standard library:

  benchmark-f1-{light,dark}.svg   ranked F1 per reviewer, Mira highlighted
  benchmark-pr-{light,dark}.svg   precision vs recall, every reviewer, F1 iso-lines

  python scripts/render_benchmark_charts.py
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape

ASSETS = Path(__file__).resolve().parents[1] / ".github" / "assets"
RESULTS = ASSETS / "benchmark-results.json"

# GitHub's README surfaces; the Mira colour clears 3:1 against both (4.1:1
# light, 7.5:1 dark). Other reviewers are deliberately recessive context and
# every mark carries its name and value in text, so identity never rests on
# colour alone.
THEMES = {
    "light": {
        "surface": "#ffffff",
        "text": "#1f2328",
        "muted": "#57606a",
        "grid": "#d0d7de",
        "mira": "#2f8f4e",
        "other": "#d0d7de",
        "other_dot": "#8c959f",
    },
    "dark": {
        "surface": "#0d1117",
        "text": "#e6edf3",
        "muted": "#8b949e",
        "grid": "#30363d",
        "mira": "#3fb950",
        "other": "#484f58",
        "other_dot": "#6e7681",
    },
}

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
WIDTH = 760


def _text(
    x: float,
    y: float,
    s: str,
    fill: str,
    size: int = 12,
    weight: str = "400",
    anchor: str = "start",
) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{escape(s)}</text>'
    )


def _bar(x: float, y: float, w: float, h: float, fill: str) -> str:
    """Horizontal bar, 4px rounded at the data end, square at the baseline."""
    r = 4 if w >= 8 else 0
    return (
        f'<path d="M{x:.1f},{y:.1f} h{w - r:.1f} a{r},{r} 0 0 1 {r},{r} v{h - 2 * r} '
        f'a{r},{r} 0 0 1 -{r},{r} h-{w - r:.1f} z" fill="{fill}"/>'
    )


def _frame(height: int, t: dict, label: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-label="{escape(label)}">',
        f'<rect width="{WIDTH}" height="{height}" fill="{t["surface"]}"/>',
    ]


# ─── Chart 1: ranked F1 ──────────────────────────────────────────────


def render_f1(results: dict, theme: str) -> str:
    t = THEMES[theme]
    rows = [(f"Mira · {results['model_label']}", 100 * results["mira"]["f1"], True)]
    rows += [(c["name"], 100 * c["f1"], False) for c in results["comparisons"]]
    rows.sort(key=lambda r: -r[1])
    left, right, top, bottom, row, bar, x_max = 170, 60, 64, 44, 26, 16, 70.0
    height = top + row * len(rows) + bottom
    plot_w = WIDTH - left - right
    x = lambda v: left + plot_w * v / x_max  # noqa: E731
    n = results["sample_size"]
    out = _frame(height, t, f"F1 per code reviewer on {n} pull requests; Mira ranks first")
    out.append(_text(left, 26, f"F1 on {n} PRs, Martian Code Review Bench", t["text"], 15, "600"))
    out.append(_text(left, 46, results["chart_subtitle"], t["muted"]))
    axis_y = top + row * len(rows)
    for v in (0, 20, 40, 60):
        out.append(
            f'<line x1="{x(v):.1f}" y1="{top - 6}" x2="{x(v):.1f}" y2="{axis_y}" '
            f'stroke="{t["grid"]}" stroke-width="1"/>'
        )
        out.append(_text(x(v), axis_y + 18, str(v), t["muted"], 11, anchor="middle"))
    out.append(
        _text(
            left,
            axis_y + 36,
            "F1 = harmonic mean of precision and recall · core profile · Claude Sonnet 4.5 judge",
            t["muted"],
            11,
        )
    )
    for i, (name, f1, is_mira) in enumerate(rows):
        y = top + row * i + (row - bar) / 2
        weight = "600" if is_mira else "400"
        out.append(_bar(left, y, max(0.0, x(f1) - left), bar, t["mira"] if is_mira else t["other"]))
        out.append(_text(left - 10, y + bar / 2 + 4, name, t["text"], 12, weight, "end"))
        out.append(_text(x(f1) + 6, y + bar / 2 + 4, f"{f1:.1f}", t["muted"], 11, weight))
    out.append("</svg>")
    return "\n".join(out) + "\n"


# ─── Chart 2: precision vs recall ────────────────────────────────────


def render_pr(results: dict, theme: str) -> str:
    t = THEMES[theme]
    left, right, top, bottom = 70, 30, 64, 56
    height = 460
    plot_w, plot_h = WIDTH - left - right, height - top - bottom
    lo, hi = 0.0, 80.0  # both axes in percent
    x = lambda r: left + plot_w * (r - lo) / (hi - lo)  # noqa: E731
    y = lambda p: top + plot_h * (1 - (p - lo) / (hi - lo))  # noqa: E731
    n = results["sample_size"]
    out = _frame(height, t, f"Precision against recall for every reviewer on {n} pull requests")
    out.append(_text(left, 26, f"Precision vs recall on {n} PRs", t["text"], 15, "600"))
    out.append(_text(left, 46, "Up and to the right is better; curves are constant F1", t["muted"]))
    for v in range(0, 81, 20):
        out.append(
            f'<line x1="{x(v):.1f}" y1="{top}" x2="{x(v):.1f}" y2="{top + plot_h}" '
            f'stroke="{t["grid"]}" stroke-width="1"/>'
        )
        out.append(
            f'<line x1="{left}" y1="{y(v):.1f}" x2="{left + plot_w}" y2="{y(v):.1f}" '
            f'stroke="{t["grid"]}" stroke-width="1"/>'
        )
        out.append(_text(x(v), top + plot_h + 18, f"{v}%", t["muted"], 11, anchor="middle"))
        out.append(_text(left - 8, y(v) + 4, f"{v}%", t["muted"], 11, anchor="end"))
    out.append(_text(left + plot_w, top + plot_h + 38, "Recall", t["muted"], 11, anchor="end"))
    out.append(
        f'<text x="{left - 44}" y="{top + plot_h / 2:.1f}" font-family="{FONT}" font-size="11" '
        f'fill="{t["muted"]}" text-anchor="middle" transform="rotate(-90 {left - 44} '
        f'{top + plot_h / 2:.1f})">Precision</text>'
    )
    # Constant-F1 curves: p = f*r / (2r - f), drawn only where both stay in range.
    for f in (30, 40, 50, 60):
        pts = []
        r = f / 2 + 0.5
        while r <= hi:
            p = f * r / (2 * r - f)
            if lo <= p <= hi:
                pts.append(f"{x(r):.1f},{y(p):.1f}")
            r += 0.5
        if pts:
            out.append(
                f'<polyline points="{" ".join(pts)}" fill="none" stroke="{t["grid"]}" '
                f'stroke-width="1"/>'
            )
            out.append(
                _text(
                    x(hi) - 2, y(f * hi / (2 * hi - f)) - 4, f"F1 {f}", t["muted"], 10, anchor="end"
                )
            )
    tools = sorted(results["comparisons"], key=lambda c: -c["f1"])
    # Direct labels only where they fit: the tools near Mira and the two
    # high-recall outliers; the rest are in the README table. Offsets keep
    # neighbours (Cubic / Qodo Extended / Augment) from overprinting.
    label_pos = {
        "Cubic v2": (8, 4, "start"),
        "Qodo Extended v2": (0, -10, "middle"),
        "Augment": (8, 12, "start"),
        "Devin": (8, 4, "start"),
        "Qodo v2": (8, 12, "start"),
        "Cursor Bugbot": (-8, -7, "end"),
        "GitHub Copilot v2": (8, -5, "start"),
        "CodeRabbit": (8, 13, "start"),
    }
    labelled = set(label_pos)
    for c in tools:
        cx, cy = x(100 * c["recall"]), y(100 * c["precision"])
        if not (lo <= 100 * c["recall"] <= hi and lo <= 100 * c["precision"] <= hi):
            continue
        out.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{t["other_dot"]}" stroke="{t["surface"]}" stroke-width="2"/>'
        )
        if c["name"] in labelled:
            dx, dy, anchor = label_pos[c["name"]]
            out.append(_text(cx + dx, cy + dy, c["name"], t["muted"], 11, anchor=anchor))
    m = results["mira"]
    cx, cy = x(100 * m["recall"]), y(100 * m["precision"])
    out.append(
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="7" fill="{t["mira"]}" stroke="{t["surface"]}" stroke-width="2"/>'
    )
    out.append(_text(cx + 10, cy + 4, f"Mira · {results['model_label']}", t["text"], 12, "600"))
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main() -> None:
    results = json.loads(RESULTS.read_text())
    results.setdefault("model_label", "DeepSeek v4.1 flash")
    results.setdefault(
        "chart_subtitle",
        "Same PRs and judge for every reviewer; competitor reviews as published · Nov 2026",
    )
    charts = {"f1": render_f1, "pr": render_pr}
    for theme in THEMES:
        for name, fn in charts.items():
            target = ASSETS / f"benchmark-{name}-{theme}.svg"
            target.write_text(fn(results, theme))
            print(target)


if __name__ == "__main__":
    main()
