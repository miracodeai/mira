import { fetchJson } from "./http"
import type { OpenPr, ReviewerStat, ReviewSummary } from "./types"

// Review-throughput health, open-PR queue, and per-reviewer stats.
export const reviewInsightsApi = {
  getReviewSummary: (days = 7, staleDays = 3) =>
    fetchJson<ReviewSummary>(
      `/api/review-insights/summary?days=${days}&stale_days=${staleDays}`
    ),

  getOpenPrs: (staleDays = 3) =>
    fetchJson<OpenPr[]>(`/api/review-insights/open-prs?stale_days=${staleDays}`),

  getReviewers: (days = 30) =>
    fetchJson<ReviewerStat[]>(`/api/review-insights/reviewers?days=${days}`),
}
