// Contribution colour scale, shared by the heatmap and the inline bar gauges so
// intensity reads identically everywhere. Index 0 is "empty", 1→4 ramp from
// light to dark green. The green ramp is FIXED across themes (one palette, no
// `dark:` variant) so the gauges/heatmap don't change colour when the theme
// flips. Only the empty cell adapts, so it blends with the page background
// instead of looking filled.
export const CONTRIB_LEVELS = [
  "bg-[#ebedf0] dark:bg-[#2d333b]",
  "bg-[#9be9a8]",
  "bg-[#40c463]",
  "bg-[#30a14e]",
  "bg-[#216e3a]",
]

// Gauge ramp — graduated AND theme-aware (its own `dark:` variants). Bars ramp
// light→dark in light mode and dark→bright in dark mode, so "more" reads as the
// deeper end on light backgrounds and the brighter end on dark ones. Index 0 is
// the empty/unlit shade.
export const CONTRIB_GAUGE_LEVELS = [
  "bg-[#ebedf0] dark:bg-[#2d333b]",
  "bg-[#9be9a8] dark:bg-[#0e4429]",
  "bg-[#40c463] dark:bg-[#006d32]",
  "bg-[#30a14e] dark:bg-[#26a641]",
  "bg-[#216e3a] dark:bg-[#39d353]",
]

// Heat ramp for gauges where "more is worse" — e.g. a reviewer's pending
// backlog. A proper severity scale: green (fine) → yellow → orange → red (bad).
// These hues read on both themes, so only the empty shade needs a dark variant.
export const CONTRIB_GAUGE_HEAT = [
  "bg-[#ebedf0] dark:bg-[#2d333b]",
  "bg-[#22c55e]",
  "bg-[#eab308]",
  "bg-[#f97316]",
  "bg-[#ef4444]",
]

// A faint inset ring gives every square/bar a crisp edge against either
// background — without it, low-intensity cells melt into the page in dark mode.
export const CONTRIB_CELL_RING = "ring-1 ring-inset ring-black/[0.06] dark:ring-white/[0.08]"

/** Map a value to a 1–5 bucket relative to a max (0 stays 0). */
export function contribLevel(value: number, max: number): number {
  if (value <= 0) return 0
  return Math.min(5, Math.max(1, Math.ceil((value / Math.max(max, 1)) * 5)))
}
