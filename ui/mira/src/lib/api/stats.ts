import { fetchJson } from "./http"
import type { OrgStatsModel } from "./types"

// Org-wide stats and time-series.
export const statsApi = {
  getOrgStats: (period?: "day" | "week" | "month") =>
    fetchJson<OrgStatsModel>(
      period ? `/api/stats?period=${period}` : "/api/stats"
    ),

  getTimeseries: (period: "day" | "week" | "month" = "day") =>
    fetchJson<
      {
        date: string
        reviews: number
        comments: number
        blockers: number
        warnings: number
        suggestions: number
        lines_changed: number
        tokens_used: number
        categories: Record<string, number>
      }[]
    >(`/api/stats/timeseries?period=${period}`),
}
