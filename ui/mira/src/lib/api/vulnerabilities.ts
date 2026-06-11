import { fetchJson } from "./http"
import type {
  OrgVulnerabilityModel,
  VulnerabilityModel,
  VulnerabilitySummary,
} from "./types"

// Dependency vulnerabilities (per-repo and org-wide).
export const vulnerabilitiesApi = {
  getRepoVulnerabilities: (owner: string, repo: string) =>
    fetchJson<VulnerabilityModel[]>(
      `/api/repos/${owner}/${repo}/vulnerabilities`
    ),

  getVulnerabilitiesSummary: () =>
    fetchJson<VulnerabilitySummary>(`/api/vulnerabilities/summary`),

  listOrgVulnerabilities: () =>
    fetchJson<OrgVulnerabilityModel[]>(`/api/vulnerabilities`),
}
