#!/usr/bin/env python3
"""Render the README benchmark chart from .github/assets/benchmark-results.json.

Outputs a ranked horizontal bar chart of F1 per reviewer on the same PRs,
Mira highlighted, in a light and a dark variant (README picks one with a
<picture> element). Pure standard library: no matplotlib.

  python scripts/render_benchmark_charts.py
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape

ASSETS = Path(__file__).resolve().parents[1] / ".github" / "assets"
RESULTS = ASSETS / "benchmark-results.json"

# GitHub's README surfaces; bar colours checked for >= 3:1 against them
# (Mira: 4.1:1 light, 7.5:1 dark). The other bars are deliberately
# recessive context; every bar carries its value at the tip and its name at
# the left, so identity never rests on colour alone.
THEMES = {
    "light": {
        "surface": "#ffffff",
        "text": "#1f2328",
        "muted": "#57606a",
        "grid": "#d0d7de",
        "mira": "#2f8f4e",
        "other": "#d0d7de",
    },
    "dark": {
        "surface": "#0d1117",
        "text": "#e6edf3",
        "muted": "#8b949e",
        "grid": "#30363d",
        "mira": "#3fb950",
        "other": "#484f58",
    },
}

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
WIDTH = 760
LEFT = 170  # room for reviewer names
RIGHT = 60  # room for the value at the tip
TOP = 64
BOTTOM = 44
ROW = 26  # band per bar
BAR = 16  # bar thickness (<= 24px; the band's leftover is air)
X_MAX = 70.0  # F1 axis ceiling


def _rows(results: dict) -> list[tuple[str, float, bool]]:
    rows = [(f"Mira · {results['model_label']}", 100 * results["mira"]["f1"], True)]
    rows += [(c["name"], 100 * c["f1"], False) for c in results["comparisons"]]
    rows.sort(key=lambda r: -r[1])
    return rows


def render(results: dict, theme: str) -> str:
    t = THEMES[theme]
    rows = _rows(results)
    height = TOP + ROW * len(rows) + BOTTOM
    plot_w = WIDTH - LEFT - RIGHT
    x = lambda v: LEFT + plot_w * v / X_MAX  # noqa: E731

    out: list[str] = []
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" '
        f'aria-label="F1 per code reviewer on {results["sample_size"]} held-out pull requests; Mira ranks first">'
    )
    out.append(f'<rect width="{WIDTH}" height="{height}" fill="{t["surface"]}"/>')
    out.append(
        f'<text x="{LEFT}" y="26" font-family="{FONT}" font-size="15" font-weight="600" '
        f'fill="{t["text"]}">F1 on {results["sample_size"]} held-out PRs, Martian Code Review Bench</text>'
    )
    out.append(
        f'<text x="{LEFT}" y="46" font-family="{FONT}" font-size="12" fill="{t["muted"]}">'
        f"{escape(results['chart_subtitle'])}</text>"
    )
    # Gridlines: hairline, solid, one step off the surface; ticks below the plot.
    axis_y = TOP + ROW * len(rows)
    for v in (0, 20, 40, 60):
        gx = x(v)
        out.append(
            f'<line x1="{gx:.1f}" y1="{TOP - 6}" x2="{gx:.1f}" y2="{axis_y}" '
            f'stroke="{t["grid"]}" stroke-width="1"/>'
        )
        out.append(
            f'<text x="{gx:.1f}" y="{axis_y + 18}" font-family="{FONT}" font-size="11" '
            f'fill="{t["muted"]}" text-anchor="middle">{v}</text>'
        )
    out.append(
        f'<text x="{LEFT}" y="{axis_y + 36}" font-family="{FONT}" font-size="11" '
        f'fill="{t["muted"]}">F1 = harmonic mean of precision and recall · core profile · '
        f"Claude Sonnet 4.5 judge</text>"
    )
    for i, (name, f1, is_mira) in enumerate(rows):
        y = TOP + ROW * i + (ROW - BAR) / 2
        w = max(0.0, x(f1) - LEFT)
        fill = t["mira"] if is_mira else t["other"]
        weight = "600" if is_mira else "400"
        # Rounded at the data end, square at the baseline: draw the bar with a
        # clip so only the right corners round.
        r = 4 if w >= 8 else 0
        path = (
            f"M{LEFT},{y} h{w - r:.1f} a{r},{r} 0 0 1 {r},{r} v{BAR - 2 * r} "
            f"a{r},{r} 0 0 1 -{r},{r} h-{w - r:.1f} z"
        )
        out.append(f'<path d="{path}" fill="{fill}"/>')
        out.append(
            f'<text x="{LEFT - 10}" y="{y + BAR / 2 + 4}" font-family="{FONT}" font-size="12" '
            f'font-weight="{weight}" fill="{t["text"]}" text-anchor="end">{escape(name)}</text>'
        )
        out.append(
            f'<text x="{x(f1) + 6:.1f}" y="{y + BAR / 2 + 4}" font-family="{FONT}" font-size="11" '
            f'font-weight="{weight}" fill="{t["muted"]}">{f1:.1f}</text>'
        )
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main() -> None:
    results = json.loads(RESULTS.read_text())
    results.setdefault("model_label", "DeepSeek v4.1 flash")
    results.setdefault(
        "chart_subtitle",
        "Same PRs and judge for every reviewer; competitor reviews as published · Nov 2026",
    )
    for theme in THEMES:
        target = ASSETS / f"benchmark-f1-{theme}.svg"
        target.write_text(render(results, theme))
        print(target)


if __name__ == "__main__":
    main()
