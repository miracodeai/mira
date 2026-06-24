// Shared API response/request types. Re-exported from `lib/api.ts`, so
// `import { RepoListItem } from "@/lib/api"` continues to work.

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
  loc?: number
}

export interface RepoDetail {
  owner: string
  repo: string
  file_count: number
  files: FileModel[]
  symbols_count: number
  imports_count: number
  external_refs_count: number
  lines_count: number
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
  id: number
  rule_text: string
  source_signal: string
  category: string
  path_pattern: string
  sample_count: number
  active: boolean
  status: "pending" | "approved" | "rejected"
  created_by: string
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

export interface ActivityEventModel extends ReviewEventModel {
  owner: string
  repo: string
}

export interface ActivityResponse {
  events: ActivityEventModel[]
  repos: string[]
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
