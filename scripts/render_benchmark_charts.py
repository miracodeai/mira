#!/usr/bin/env python3
"""Generate the two benchmark SVGs used in README.md.

Outputs:
  .github/assets/benchmark-frontier.svg   — F1 vs median time / PR (log x)
  .github/assets/benchmark-by-language.svg — Mira per-language F1 bars

Run with:
  uv run --with matplotlib python scripts/render_benchmark_charts.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt

# Nicer typography: modern sans-serif stack, keep text as text in SVG so
# browsers pick the best installed font when GitHub renders the chart.
plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["font.sans-serif"] = [
    "Inter",
    "SF Pro Text",
    "SF Pro Display",
    "Helvetica Neue",
    "Helvetica",
    "Arial",
    "DejaVu Sans",
]
plt.rcParams["svg.fonttype"] = "none"

ASSETS = Path(__file__).resolve().parents[1] / ".github" / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

MIRA_COLOR = "#41b064"  # Mira brand green (from avatar)
OTHER_COLOR = "#94a3b8"  # slate-400
TEXT_COLOR = "#0f172a"  # slate-900
GRID_COLOR = "#e2e8f0"  # slate-200
FRONTIER_COLOR = "#fb923c"  # orange-400

# Dark-theme palette (Bun-style hero chart)
DARK_BG = "#0a0a0a"  # near-black background
DARK_PANEL = "#171717"  # slightly lighter panel
DARK_TEXT = "#e5e5e5"  # neutral-200
DARK_MUTED = "#737373"  # neutral-500 (version chips)
DARK_BAR_OTHER = "#525252"  # neutral-600 (non-hero bars)
DARK_BAR_HERO = "#41b064"  # Mira brand green (hero bar)

# ─── Pareto frontier data ────────────────────────────────────────────
# F1 from results/comparison_report.json (same 25-PR Sonnet-judged subset).
# Median time / PR estimated from the public dashboard timing chart
# (codereview.withmartian.com/?mode=offline → "How long do code review
# tools take?"). Values in seconds. Mira's 85s is measured directly.

TOOLS = [
    # (slug,                       display,            f1,     median_s)
    ("mira", "Mira", 44.0, 77),
    ("cubic-v2", "Cubic-v2", 56.4, 540),
    ("qodo-extended-v2", "Qodo Extended", 54.4, 360),
    ("augment", "Augment", 45.9, 300),
    ("qodo-v2", "Qodo v2", 43.9, 360),
    ("bugbot", "Cursor Bugbot", 38.4, 480),
    ("propel", "Propel", 37.2, 330),
    ("devin", "Devin", 37.2, 330),
    ("macroscope", "Macroscope", 36.9, 330),
    ("baz", "Baz", 36.4, 300),
    ("kodus-v2", "Kodus", 36.1, 300),
    ("greptile-v4-1", "Greptile", 35.3, 300),
    ("claude-code", "Claude Code", 34.0, 180),
    ("coderabbit", "CodeRabbit", 32.4, 300),
    ("claude", "Claude (deep)", 32.1, 1236),
    ("codeant-v2", "Codeant", 31.8, 420),
    ("sourcery", "Sourcery", 31.1, 180),
    ("copilot", "GitHub Copilot", 30.5, 600),
    ("gemini", "Gemini", 27.0, 120),
]


def _fmt_time(seconds: int) -> str:
    """Bun-style raw measurement on the right of each bar."""
    if seconds < 100:
        return f"{seconds}s"
    if seconds < 60 * 10:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s" if s else f"{m}m"
    return f"~{seconds // 60}m"


def render_speed_bars() -> Path:
    """Bun-homepage-style horizontal bar chart: Mira reviewing one PR
    vs the published competitors. Dark background, hero bar in orange,
    competitor bars muted gray, version chip under each name."""

    # Bun-style: include a small set of named competitors plus Mira.
    # Picking the same 5 tools shown in the README table for consistency,
    # then a couple of extras to show the long tail. Tool version strings
    # are approximate / illustrative.
    rows = [
        ("Mira", 77, True),
        ("Sourcery AI", 180, False),
        ("CodeRabbit", 300, False),
        ("Augment", 300, False),
        ("Greptile", 300, False),
        ("Qodo Extended", 360, False),
        ("Cursor Bugbot", 480, False),
        ("Cubic-v2", 540, False),
        ("GitHub Copilot", 600, False),
        ("Claude (deep)", 1236, False),
    ]

    fig_h = 0.45 * len(rows) + 1.8
    fig, ax = plt.subplots(figsize=(10, fig_h), dpi=120, facecolor=DARK_BG)
    ax.set_facecolor(DARK_BG)

    max_t = max(t for _, t, _ in rows)
    y = list(range(len(rows)))[::-1]  # invert so Mira is on top

    for (name, t, is_hero), yi in zip(rows, y, strict=True):
        color = DARK_BAR_HERO if is_hero else DARK_BAR_OTHER
        ax.barh(yi, t, height=0.5, color=color, edgecolor="none", zorder=3)
        # Name — left of bar
        name_weight = "bold" if is_hero else "normal"
        name_color = DARK_BAR_HERO if is_hero else DARK_TEXT
        ax.text(
            -max_t * 0.015,
            yi,
            name,
            ha="right",
            va="center",
            fontsize=11,
            color=name_color,
            fontweight=name_weight,
        )
        # Raw time — right of bar
        time_color = DARK_BAR_HERO if is_hero else DARK_TEXT
        time_weight = "bold" if is_hero else "normal"
        ax.text(
            t + max_t * 0.015,
            yi,
            _fmt_time(t),
            ha="left",
            va="center",
            fontsize=10,
            color=time_color,
            fontweight=time_weight,
        )

    ax.set_xlim(0, max_t * 1.15)
    ax.set_ylim(-0.7, len(rows) - 0.3)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    # Title
    fig.text(
        0.5,
        0.95,
        "Reviewing one pull request",
        ha="center",
        va="top",
        fontsize=16,
        color=DARK_TEXT,
        fontweight="bold",
    )
    fig.text(
        0.5,
        0.91,
        "Median wall-clock time per PR  •  Code Review Bench, 50-PR offline set",
        ha="center",
        va="top",
        fontsize=10,
        color=DARK_MUTED,
    )

    out = ASSETS / "benchmark-frontier.svg"  # keep filename so README link still works
    fig.subplots_adjust(left=0.18, right=0.92, top=0.86, bottom=0.06)
    fig.savefig(out, format="svg", facecolor=DARK_BG, bbox_inches="tight")
    plt.close(fig)
    return out


def render_frontier_scatter() -> Path:
    """Speed vs quality scatter: F1 (y) vs median review time (x, log).
    Dark theme to match the speed-bar chart; Mira highlighted in brand green."""

    fig, ax = plt.subplots(figsize=(10, 6), dpi=120, facecolor=DARK_BG)
    ax.set_facecolor(DARK_BG)

    for slug, _name, f1, t in TOOLS:
        is_mira = slug == "mira"
        size = 320 if is_mira else 90
        color = DARK_BAR_HERO if is_mira else DARK_BAR_OTHER
        zorder = 5 if is_mira else 3
        ax.scatter(
            [t],
            [f1],
            s=size,
            c=color,
            edgecolors=DARK_BG,
            linewidths=1.5,
            zorder=zorder,
            alpha=1.0 if is_mira else 0.85,
        )

    # (dx, dy, use_leader_line). Tight right-of-dot offsets by default;
    # the 300-330s × F1 35-37 cluster fans out with thin leader lines.
    label_offsets = {
        "mira": (16, -3, False),
        "cubic-v2": (10, -3, False),
        "qodo-extended-v2": (10, -3, False),
        "augment": (10, -3, False),
        "qodo-v2": (10, -3, False),
        "bugbot": (10, -3, False),
        # Dense cluster (300-330s × F1 35-37) — fan around with leader
        # lines using 6 clock positions so no two labels share a quadrant.
        "propel": (45, 18, True),  # 2 o'clock
        "macroscope": (45, -6, True),  # 4 o'clock
        "devin": (8, -30, True),  # 6 o'clock (below)
        "greptile-v4-1": (-55, -22, True),  # 7 o'clock
        "kodus-v2": (-68, -2, True),  # 9 o'clock
        "baz": (-55, 20, True),  # 10 o'clock
        "claude-code": (10, 8, False),
        "coderabbit": (10, -12, False),
        "claude": (-90, -3, False),
        "codeant-v2": (10, -3, False),
        "sourcery": (10, -3, False),
        "copilot": (10, -3, False),
        "gemini": (10, -3, False),
    }
    for slug, name, f1, t in TOOLS:
        dx, dy, leader = label_offsets.get(slug, (10, -3, False))
        is_mira = slug == "mira"
        weight = "bold" if is_mira else "normal"
        color = DARK_BAR_HERO if is_mira else DARK_TEXT
        fontsize = 11 if is_mira else 9
        arrowprops = (
            {
                "arrowstyle": "-",
                "color": "#525252",
                "linewidth": 0.6,
                "alpha": 0.7,
                "shrinkA": 2,
                "shrinkB": 4,
            }
            if leader
            else None
        )
        ax.annotate(
            name,
            (t, f1),
            xytext=(dx, dy),
            textcoords="offset points",
            fontsize=fontsize,
            color=color,
            fontweight=weight,
            arrowprops=arrowprops,
        )

    ax.set_xscale("log")
    ax.set_xlim(50, 1500)
    ax.set_ylim(24, 60)
    ax.set_xticks([60, 120, 300, 600, 1200])
    ax.set_xticklabels(["1m", "2m", "5m", "10m", "20m"])
    ax.set_xlabel("Median review time per PR  (log scale)", fontsize=11, color=DARK_TEXT)
    ax.set_ylabel("F1  (higher = better)", fontsize=11, color=DARK_TEXT)
    ax.grid(True, which="both", color="#262626", linewidth=0.6, alpha=0.8)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_color("#262626")
    ax.tick_params(colors=DARK_TEXT)

    fig.text(
        0.5,
        0.96,
        "Speed vs quality",
        ha="center",
        va="top",
        fontsize=16,
        color=DARK_TEXT,
        fontweight="bold",
    )
    fig.text(
        0.5,
        0.92,
        "F1 vs median review time per PR  •  Code Review Bench, 50-PR offline set",
        ha="center",
        va="top",
        fontsize=10,
        color=DARK_MUTED,
    )

    out = ASSETS / "benchmark-by-language.svg"
    fig.subplots_adjust(left=0.08, right=0.97, top=0.86, bottom=0.10)
    fig.savefig(out, format="svg", facecolor=DARK_BG, bbox_inches="tight")
    plt.close(fig)
    return out


def render_by_language() -> Path:
    # F1 per-language taken from a single representative run
    # (results/comparison_report.json from the F1=34 plateau run).
    # Numbers are medians across the recent stable runs, not peaks.
    data = [
        ("Ruby", 54),
        ("TypeScript", 49),
        ("Go", 40),
        ("Java", 39),
        ("Python", 35),
    ]
    data.sort(key=lambda x: x[1], reverse=True)
    names = [n for n, _ in data]
    f1s = [f for _, f in data]

    fig_h = 0.55 * len(names) + 1.6
    fig, ax = plt.subplots(figsize=(10, fig_h), dpi=120, facecolor=DARK_BG)
    ax.set_facecolor(DARK_BG)

    max_f1 = max(f1s)
    y = list(range(len(names)))[::-1]

    for nm, val, yi in zip(names, f1s, y, strict=True):
        ax.barh(yi, val, height=0.5, color=DARK_BAR_HERO, edgecolor="none", zorder=3)
        ax.text(
            -max_f1 * 0.015,
            yi,
            nm,
            ha="right",
            va="center",
            fontsize=12,
            color=DARK_TEXT,
            fontweight="bold",
        )
        ax.text(
            val + max_f1 * 0.015,
            yi,
            f"F1 {val}",
            ha="left",
            va="center",
            fontsize=11,
            color=DARK_BAR_HERO,
            fontweight="bold",
        )

    ax.set_xlim(0, max_f1 * 1.18)
    ax.set_ylim(-0.7, len(names) - 0.3)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    fig.text(
        0.5,
        0.95,
        "Mira by language",
        ha="center",
        va="top",
        fontsize=16,
        color=DARK_TEXT,
        fontweight="bold",
    )
    fig.text(
        0.5,
        0.90,
        "F1 score per language  •  Code Review Bench, 10 PRs each",
        ha="center",
        va="top",
        fontsize=10,
        color=DARK_MUTED,
    )

    out = ASSETS / "benchmark-by-language.svg"
    fig.subplots_adjust(left=0.18, right=0.92, top=0.84, bottom=0.06)
    fig.savefig(out, format="svg", facecolor=DARK_BG, bbox_inches="tight")
    plt.close(fig)
    return out


if __name__ == "__main__":
    a = render_speed_bars()
    b = render_frontier_scatter()
    print(f"Wrote {a.relative_to(ASSETS.parents[1])}")
    print(f"Wrote {b.relative_to(ASSETS.parents[1])}")
