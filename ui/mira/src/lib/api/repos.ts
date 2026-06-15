import { deleteJson, fetchJson, postJson, putJson } from "./http"
import type {
  DependencyGraph,
  ExternalRefModel,
  FileModel,
  PackageModel,
  RepoDetail,
  RepoListItem,
  ReviewContextModel,
  ReviewEventModel,
} from "./types"

// Repositories: detail, files, dependencies, indexing, reviews, and the
// per-repo review-context entries.
export const reposApi = {
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
      `/api/repos/${owner}/${repo}/blast-radius${paths && paths.length ? `?changed_paths=${encodeURIComponent(paths.join(","))}` : ""}`
    ),

  // Indexing
  triggerIndex: (owner: string, repo: string, full = false) =>
    postJson<{ status: string }>(
      `/api/repos/${owner}/${repo}/index?full=${full}`,
      {}
    ),

  cancelIndex: (owner: string, repo: string) =>
    deleteJson(`/api/repos/${owner}/${repo}/index`),

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

  // Review events
  listReviews: (owner: string, repo: string, limit = 50) =>
    fetchJson<ReviewEventModel[]>(
      `/api/repos/${owner}/${repo}/reviews?limit=${limit}`
    ),

  // Review context
  listContext: (owner: string, repo: string) =>
    fetchJson<ReviewContextModel[]>(`/api/repos/${owner}/${repo}/context`),

  createContext: (
    owner: string,
    repo: string,
    title: string,
    content: string
  ) =>
    postJson<ReviewContextModel>(`/api/repos/${owner}/${repo}/context`, {
      title,
      content,
    }),

  updateContext: (
    owner: string,
    repo: string,
    id: number,
    title: string,
    content: string
  ) =>
    putJson<ReviewContextModel>(`/api/repos/${owner}/${repo}/context/${id}`, {
      title,
      content,
    }),

  deleteContext: (owner: string, repo: string, id: number) =>
    deleteJson(`/api/repos/${owner}/${repo}/context/${id}`),
}
