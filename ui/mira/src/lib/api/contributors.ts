import { fetchJson, postJson } from "./http"
import type {
  ContributorDetail,
  ContributorListItem,
  ContributorSort,
  ContributorSummary,
  StatsPeriod,
} from "./types"

// Contributor leaderboards, per-contributor detail, and backfill triggers.
export const contributorsApi = {
  listContributors: (
    sort: ContributorSort = "commits",
    period?: StatsPeriod,
    includeBots = false
  ) => {
    const params = new URLSearchParams({ sort })
    if (period) params.set("period", period)
    if (includeBots) params.set("include_bots", "true")
    return fetchJson<ContributorListItem[]>(
      `/api/contributors?${params.toString()}`
    )
  },

  getContributor: (login: string, period?: StatsPeriod) => {
    const qs = period ? `?period=${period}` : ""
    return fetchJson<ContributorDetail>(
      `/api/contributors/${encodeURIComponent(login)}${qs}`
    )
  },

  getContributorsSummary: (days = 7) =>
    fetchJson<ContributorSummary>(`/api/contributors/summary?days=${days}`),

  refreshContributors: () =>
    postJson<{ status: string }>("/api/contributors/refresh", {}),

  refreshContributorsRepo: (owner: string, repo: string) =>
    postJson<{ status: string }>(
      `/api/contributors/${owner}/${repo}/refresh`,
      {}
    ),
}
