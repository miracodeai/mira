import { deleteJson, fetchJson, postJson } from "./http"
import type {
  CustomEdgeModel,
  OverrideModel,
  RelatedRepoModel,
  RelationshipsResponse,
} from "./types"

// Cross-repo relationships, manual overrides, and custom edges.
export const relationshipsApi = {
  getRelationships: () =>
    fetchJson<RelationshipsResponse>("/api/relationships"),

  getRelatedRepos: (owner: string, repo: string) =>
    fetchJson<RelatedRepoModel[]>(`/api/relationships/${owner}/${repo}`),

  // Relationship overrides
  setOverride: (
    source_repo: string,
    target_repo: string,
    status: "confirmed" | "denied"
  ) =>
    postJson<OverrideModel>("/api/relationships/overrides", {
      source_repo,
      target_repo,
      status,
    }),

  deleteOverride: (source_repo: string, target_repo: string) =>
    deleteJson(
      `/api/relationships/overrides?source_repo=${encodeURIComponent(source_repo)}&target_repo=${encodeURIComponent(target_repo)}`
    ),

  listOverrides: () =>
    fetchJson<OverrideModel[]>("/api/relationships/overrides"),

  // Custom edges
  addCustomEdge: (source_repo: string, target_repo: string, reason: string) =>
    postJson<CustomEdgeModel>("/api/relationships/custom", {
      source_repo,
      target_repo,
      reason,
    }),

  deleteCustomEdge: (id: number) =>
    deleteJson(`/api/relationships/custom/${id}`),

  listCustomEdges: () =>
    fetchJson<CustomEdgeModel[]>("/api/relationships/custom"),
}
