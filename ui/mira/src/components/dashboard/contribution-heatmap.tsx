import { useMemo } from "react"

import type { HeatmapDay } from "@/lib/api"
import { CONTRIB_CELL_RING, CONTRIB_LEVELS } from "./contrib-colors"

// GitHub-style yearly contribution grid: 7 rows (Sun→Sat) × ~53 week columns.
// Built as a plain CSS grid — Recharts has no calendar primitive. Colours match
// GitHub's green contribution scale (shared with the inline bar gauges) and
// carry a per-cell ring so squares stay legible in both light and dark themes.

const LEVEL_CLASS = CONTRIB_LEVELS

const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

function level(total: number): number {
  if (total <= 0) return 0
  if (total <= 2) return 1
  if (total <= 5) return 2
  if (total <= 9) return 3
  return 4
}

// UTC 'YYYY-MM-DD' — must match the backend's event_day bucketing exactly.
function ymdUTC(d: Date): string {
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(
    d.getUTCDate(),
  ).padStart(2, "0")}`
}

interface Cell {
  key: string
  date: Date
  day: HeatmapDay | null
}

export function ContributionHeatmap({ days }: { days: HeatmapDay[] }) {
  const { columns, monthLabels, total } = useMemo(() => {
    const byDay = new Map(days.map((d) => [d.day, d]))
    const totalContribs = days.reduce((sum, d) => sum + d.total, 0)

    const now = new Date()
    const end = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()))
    const start = new Date(end)
    start.setUTCDate(start.getUTCDate() - 364)
    // Back up to Sunday so the first column is a full week.
    start.setUTCDate(start.getUTCDate() - start.getUTCDay())

    const cols: Cell[][] = []
    const labels: { col: number; label: string }[] = []
    const cursor = new Date(start)
    let prevMonth = -1
    while (cursor <= end) {
      const week: Cell[] = []
      for (let i = 0; i < 7; i++) {
        const key = ymdUTC(cursor)
        week.push({ key, date: new Date(cursor), day: byDay.get(key) ?? null })
        cursor.setUTCDate(cursor.getUTCDate() + 1)
      }
      // Month label appears on the column where a new month first shows up.
      const firstMonth = week[0].date.getUTCMonth()
      if (firstMonth !== prevMonth) {
        labels.push({ col: cols.length, label: MONTHS[firstMonth] })
        prevMonth = firstMonth
      }
      cols.push(week)
    }
    return { columns: cols, monthLabels: labels, total: totalContribs }
  }, [days])

  const monthByCol = new Map(monthLabels.map((m) => [m.col, m.label]))

  return (
    <div className="space-y-2">
      <div className="inline-flex flex-col gap-1 text-muted-foreground">
        {/* Month labels — one slot per week column, offset past weekday labels. */}
        <div className="flex gap-1 pl-7">
          {columns.map((_, ci) => (
            <div key={ci} className="relative h-3 w-3">
              {monthByCol.has(ci) && (
                <span className="absolute left-0 top-0 whitespace-nowrap text-[10px] leading-3">
                  {monthByCol.get(ci)}
                </span>
              )}
            </div>
          ))}
        </div>

        <div className="flex gap-1">
          {/* Weekday labels (Mon / Wed / Fri). */}
          <div className="mr-1 flex w-6 flex-col gap-1 text-[10px] leading-3">
            {["", "Mon", "", "Wed", "", "Fri", ""].map((d, i) => (
              <div key={i} className="h-3">
                {d}
              </div>
            ))}
          </div>

          {columns.map((week, ci) => (
            <div key={ci} className="flex flex-col gap-1">
              {week.map((cell) => {
                const t = cell.day?.total ?? 0
                const title = `${t} contribution${t === 1 ? "" : "s"} on ${cell.key}`
                return (
                  <div
                    key={cell.key}
                    title={title}
                    className={`h-3 w-3 rounded-sm ${CONTRIB_CELL_RING} ${LEVEL_CLASS[level(t)]}`}
                  />
                )
              })}
            </div>
          ))}
        </div>
      </div>

      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>{total.toLocaleString()} contributions in the last year</span>
        <span className="flex items-center gap-1">
          Less
          {LEVEL_CLASS.map((cls, i) => (
            <span key={i} className={`h-3 w-3 rounded-sm ${CONTRIB_CELL_RING} ${cls}`} />
          ))}
          More
        </span>
      </div>
    </div>
  )
}
