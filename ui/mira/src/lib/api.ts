const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8100"

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { credentials: "include" })
  if (!res.ok) {
    if (res.status === 401) window.location.href = "/login"
    throw new Error(`API error ${res.status}: ${await res.text()}`)
  }
  return res.json() as Promise<T>
}

// ── Types ──

export interface RepoListItem {
  owner: string
  repo: string
  status: string
  index_mode: string
  file_count: number
  file_count_estimate: number
  installation_id: number
  error: string
  last_indexed: string | null
}

export interface SymbolModel {
  name: string
  kind: string
  signature: string
}

export interface FileModel {
  path: string
  language: string
  summary: string
  symbols: SymbolModel[]
  imports: string[]
}

export interface RepoDetail {
  owner: string
  repo: string
  file_count: number
  files: FileModel[]
  symbols_count: number
  imports_count: number
  external_refs_count: number
  last_indexed: string | null
}

export interface ImportEdge {
  source: string
  target: string
}

export interface DependentEdge {
  path: string
  dependent_path: string
}

export interface DependencyGraph {
  imports: ImportEdge[]
  dependents: DependentEdge[]
}

export interface ExternalRefModel {
  file_path: string
  kind: string
  target: string
  description: string
}

export interface PackageModel {
  name: string
  kind: string
  version: string
  file_path: string
  is_dev: boolean
}

export interface PackageSearchHit {
  owner: string
  repo: string
  name: string
  kind: string
  version: string
  file_path: string
  is_dev: boolean
}

export interface VulnerabilityModel {
  package_name: string
  ecosystem: string
  package_version: string
  cve_id: string
  summary: string
  severity: "critical" | "high" | "moderate" | "low" | "unknown"
  advisory_url: string
  fixed_in: string
  last_seen_at: number
}

export interface OrgVulnerabilityModel extends VulnerabilityModel {
  owner: string
  repo: string
}

export interface VulnerabilitySummary {
  total: number
  critical: number
  high: number
  moderate: number
  low: number
  unknown: number
}

export interface LearnedRuleModel {
  rule_text: string
  source_signal: string
  category: string
  path_pattern: string
  sample_count: number
  updated_at: number
}

export interface OrgLearnedRuleModel extends LearnedRuleModel {
  owner: string
  repo: string
}

export interface RepoEdgeModel {
  source_repo: string
  target_repo: string
  kind: string
  ref_count: number
}

export interface RepoGroupModel {
  name: string
  repos: string[]
  confidence: number
  evidence: string[]
}

export interface RelationshipsResponse {
  edges: RepoEdgeModel[]
  groups: RepoGroupModel[]
}

export interface RelatedRepoModel {
  repo: string
  relationship_type: string
  edge_count: number
}

export interface ReviewEventModel {
  id: number
  pr_number: number
  pr_title: string
  pr_url: string
  comments_posted: number
  blockers: number
  warnings: number
  suggestions: number
  files_reviewed: number
  lines_changed: number
  tokens_used: number
  duration_ms: number
  categories: string
  created_at: number
}

export interface ReviewStatsModel {
  total_reviews: number
  total_comments: number
  total_blockers: number
  total_warnings: number
  total_suggestions: number
  total_files_reviewed: number
  total_lines_changed: number
  total_tokens: number
  avg_duration_ms: number
  categories: Record<string, number>
  avg_comments_per_pr: number
}

export interface OrgStatsModel {
  total_repos: number
  total_files: number
  total_edges: number
  total_groups: number
  review_stats: ReviewStatsModel
}

export interface ReviewContextModel {
  id: number
  title: string
  content: string
  created_at: number
  updated_at: number
}

export interface OverrideModel {
  source_repo: string
  target_repo: string
  status: string
  created_at: number
}

export interface CustomEdgeModel {
  id: number
  source_repo: string
  target_repo: string
  reason: string
  created_at: number
}

export interface RuleModel {
  id: number
  title: string
  content: string
  enabled: boolean
  created_at: number
  updated_at: number
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`)
  return res.json() as Promise<T>
}

async function putJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`)
  return res.json() as Promise<T>
}

async function deleteJson(path: string): Promise<void> {
  const res = await fetch(`${API_BASE}${path}`, { method: "DELETE", credentials: "include" })
  if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`)
}

async function patchJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { method: "PATCH", credentials: "include" })
  if (!res.ok) throw new Error(`API error ${res.status}: ${await res.text()}`)
  return res.json() as Promise<T>
}

// ── API functions ──

export const api = {
  getSetupStatus: () =>
    fetchJson<{ setup_complete: boolean; repo_count: number }>("/api/setup/status"),

  syncRepos: () =>
    postJson<{ synced: number; removed: number }>("/api/repos/sync", {}),

  listPendingUninstalls: () =>
    fetchJson<{ installation_id: number; owner: string }[]>(
      "/api/uninstalls/pending",
    ),

  keepUninstallData: (installation_id: number) =>
    postJson<{ ok: boolean }>(
      `/api/uninstalls/${installation_id}/keep`,
      {},
    ),

  deleteUninstallData: (installation_id: number) =>
    postJson<{ ok: boolean; removed: number }>(
      `/api/uninstalls/${installation_id}/delete`,
      {},
    ),

  getModels: () =>
    fetchJson<{
      indexing_model: string
      review_model: string
      indexing_options: { value: string; label: string; recommended?: boolean }[]
      review_options: { value: string; label: string; recommended?: boolean }[]
    }>("/api/settings/models"),

  getCostEstimate: () =>
    fetchJson<{
      estimated_usd: number
      input_tokens: number
      output_tokens: number
      model: string
      file_count: number
    }>("/api/indexing/estimate"),

  saveModels: (indexing_model: string, review_model: string) =>
    putJson<{ ok: boolean }>("/api/settings/models", {
      indexing_model,
      review_model,
    }),

  completeSetup: (
    repos: { owner: string; repo: string; enabled: boolean }[],
    index_mode: string,
  ) =>
    postJson<{ status: string; repos: number }>("/api/setup/complete", {
      repos,
      index_mode,
    }),

  getOrgStats: (period?: "day" | "week" | "month") =>
    fetchJson<OrgStatsModel>(period ? `/api/stats?period=${period}` : "/api/stats"),

  getIndexingStatus: () =>
    fetchJson<
      {
        repo: string
        status: string
        files_total: number
        files_done: number
        started_at: number
        finished_at: number
        error: string
      }[]
    >("/api/indexing/status"),

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

  listRepos: () => fetchJson<RepoListItem[]>("/api/repos"),

  getRepo: (owner: string, repo: string) =>
    fetchJson<RepoDetail>(`/api/repos/${owner}/${repo}`),

  getFiles: (owner: string, repo: string) =>
    fetchJson<FileModel[]>(`/api/repos/${owner}/${repo}/files`),

  getDependencies: (owner: string, repo: string) =>
    fetchJson<DependencyGraph>(`/api/repos/${owner}/${repo}/dependencies`),

  getExternalRefs: (owner: string, repo: string) =>
    fetchJson<ExternalRefModel[]>(`/api/repos/${owner}/${repo}/external-refs`),

  getPackages: (owner: string, repo: string) =>
    fetchJson<PackageModel[]>(`/api/repos/${owner}/${repo}/packages`),

  searchPackages: (params: {
    name?: string
    version?: string
    kind?: string
    is_dev?: boolean
  }) => {
    const qs = new URLSearchParams()
    if (params.name) qs.set("name", params.name)
    if (params.version) qs.set("version", params.version)
    if (params.kind) qs.set("kind", params.kind)
    if (params.is_dev !== undefined) qs.set("is_dev", String(params.is_dev))
    return fetchJson<PackageSearchHit[]>(`/api/packages/search?${qs.toString()}`)
  },

  getRepoVulnerabilities: (owner: string, repo: string) =>
    fetchJson<VulnerabilityModel[]>(`/api/repos/${owner}/${repo}/vulnerabilities`),

  getVulnerabilitiesSummary: () =>
    fetchJson<VulnerabilitySummary>(`/api/vulnerabilities/summary`),

  listOrgVulnerabilities: () =>
    fetchJson<OrgVulnerabilityModel[]>(`/api/vulnerabilities`),

  listLearnedRules: () =>
    fetchJson<OrgLearnedRuleModel[]>(`/api/learned-rules`),

  listRepoLearnedRules: (owner: string, repo: string) =>
    fetchJson<LearnedRuleModel[]>(`/api/repos/${owner}/${repo}/learned-rules`),

  getBlastRadius: (owner: string, repo: string, paths?: string[]) =>
    fetchJson<{
      internal: {
        path: string
        summary: string
        affected_symbols: string[]
        depth: number
      }[]
      cross_repo: {
        repo: string
        files: {
          path: string
          kind: string
          target: string
          description: string
        }[]
        edge_kind: string
      }[]
    }>(
      `/api/repos/${owner}/${repo}/blast-radius${paths && paths.length ? `?changed_paths=${encodeURIComponent(paths.join(","))}` : ""}`,
    ),

  getRelationships: () => fetchJson<RelationshipsResponse>("/api/relationships"),

  getRelatedRepos: (owner: string, repo: string) =>
    fetchJson<RelatedRepoModel[]>(
      `/api/relationships/${owner}/${repo}`,
    ),

  // Indexing
  triggerIndex: (owner: string, repo: string, full = false) =>
    postJson<{ status: string }>(`/api/repos/${owner}/${repo}/index?full=${full}`, {}),

  cancelIndex: (owner: string, repo: string) =>
    deleteJson(`/api/repos/${owner}/${repo}/index`),

  // Review events
  listReviews: (owner: string, repo: string, limit = 50) =>
    fetchJson<ReviewEventModel[]>(`/api/repos/${owner}/${repo}/reviews?limit=${limit}`),

  // Review context
  listContext: (owner: string, repo: string) =>
    fetchJson<ReviewContextModel[]>(`/api/repos/${owner}/${repo}/context`),

  createContext: (owner: string, repo: string, title: string, content: string) =>
    postJson<ReviewContextModel>(`/api/repos/${owner}/${repo}/context`, { title, content }),

  updateContext: (owner: string, repo: string, id: number, title: string, content: string) =>
    putJson<ReviewContextModel>(`/api/repos/${owner}/${repo}/context/${id}`, { title, content }),

  deleteContext: (owner: string, repo: string, id: number) =>
    deleteJson(`/api/repos/${owner}/${repo}/context/${id}`),

  // Relationship overrides
  setOverride: (source_repo: string, target_repo: string, status: "confirmed" | "denied") =>
    postJson<OverrideModel>("/api/relationships/overrides", { source_repo, target_repo, status }),

  deleteOverride: (source_repo: string, target_repo: string) =>
    deleteJson(`/api/relationships/overrides?source_repo=${source_repo}&target_repo=${target_repo}`),

  listOverrides: () => fetchJson<OverrideModel[]>("/api/relationships/overrides"),

  // Custom edges
  addCustomEdge: (source_repo: string, target_repo: string, reason: string) =>
    postJson<CustomEdgeModel>("/api/relationships/custom", { source_repo, target_repo, reason }),

  deleteCustomEdge: (id: number) => deleteJson(`/api/relationships/custom/${id}`),

  listCustomEdges: () => fetchJson<CustomEdgeModel[]>("/api/relationships/custom"),

  // User management (admin only)
  listUsers: () =>
    fetchJson<{ id: number; username: string; is_admin: boolean }[]>(
      "/api/auth/users",
    ),

  createUser: (username: string, password: string, is_admin: boolean) =>
    postJson<{ id: number; username: string; is_admin: boolean }>(
      "/api/auth/users",
      { username, password, is_admin },
    ),

  deleteUser: (id: number) => deleteJson(`/api/auth/users/${id}`),

  // Global rules
  listGlobalRules: () =>
    fetchJson<RuleModel[]>("/api/rules/global"),

  createGlobalRule: (title: string, content: string) =>
    postJson<RuleModel>("/api/rules/global", { title, content }),

  updateGlobalRule: (id: number, title: string, content: string) =>
    putJson<RuleModel>(`/api/rules/global/${id}`, { title, content }),

  deleteGlobalRule: (id: number) => deleteJson(`/api/rules/global/${id}`),

  toggleGlobalRule: (id: number) =>
    patchJson<RuleModel>(`/api/rules/global/${id}/toggle`),

  // Per-repo rules
  listRepoRules: (owner: string, repo: string) =>
    fetchJson<RuleModel[]>(`/api/repos/${owner}/${repo}/rules`),

  createRepoRule: (owner: string, repo: string, title: string, content: string) =>
    postJson<RuleModel>(`/api/repos/${owner}/${repo}/rules`, { title, content }),

  updateRepoRule: (owner: string, repo: string, id: number, title: string, content: string) =>
    putJson<RuleModel>(`/api/repos/${owner}/${repo}/rules/${id}`, { title, content }),

  deleteRepoRule: (owner: string, repo: string, id: number) =>
    deleteJson(`/api/repos/${owner}/${repo}/rules/${id}`),
}
