import { CONTRIB_CELL_RING, CONTRIB_GAUGE_HEAT, CONTRIB_GAUGE_LEVELS, contribLevel } from "./contrib-colors"

// Ascending bar heights — a compact signal-strength-style gauge.
const HEIGHTS = ["h-1.5", "h-2", "h-2.5", "h-3", "h-3.5"]

/**
 * A tiny 5-bar gauge. The number of lit bars (and their rising height) encodes
 * `value` relative to `max`; lit bars ramp through a theme-aware scale. `tone`
 * picks the ramp: "green" (more is good) or "heat" (more is worse, e.g. a
 * backlog). Unlit bars use the empty shade.
 */
export function BarGauge({
  value,
  max,
  label,
  tone = "green",
}: {
  value: number
  max: number
  label?: string
  tone?: "green" | "heat"
}) {
  const levels = tone === "heat" ? CONTRIB_GAUGE_HEAT : CONTRIB_GAUGE_LEVELS
  const filled = contribLevel(value, max)
  const title = label ?? `${value.toLocaleString()}`
  return (
    <span className="inline-flex items-end gap-0.5" title={title} aria-label={title}>
      {HEIGHTS.map((h, i) => (
        <span
          key={i}
          className={`w-1 rounded-[1px] ${h} ${CONTRIB_CELL_RING} ${
            i < filled ? levels[Math.min(i + 1, 4)] : levels[0]
          }`}
        />
      ))}
    </span>
  )
}
