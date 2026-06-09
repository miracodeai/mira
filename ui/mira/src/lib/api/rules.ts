import { deleteJson, fetchJson, patchJson, postJson, putJson } from "./http"
import type { LearnedRuleModel, OrgLearnedRuleModel, RuleModel } from "./types"

// Custom rules (global + per-repo) and learned rules.
export const rulesApi = {
  // Learned rules
  listLearnedRules: () =>
    fetchJson<OrgLearnedRuleModel[]>(`/api/learned-rules`),

  listRepoLearnedRules: (owner: string, repo: string) =>
    fetchJson<LearnedRuleModel[]>(`/api/repos/${owner}/${repo}/learned-rules`),

  // Global rules
  listGlobalRules: () => fetchJson<RuleModel[]>("/api/rules/global"),

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

  createRepoRule: (
    owner: string,
    repo: string,
    title: string,
    content: string
  ) =>
    postJson<RuleModel>(`/api/repos/${owner}/${repo}/rules`, {
      title,
      content,
    }),

  updateRepoRule: (
    owner: string,
    repo: string,
    id: number,
    title: string,
    content: string
  ) =>
    putJson<RuleModel>(`/api/repos/${owner}/${repo}/rules/${id}`, {
      title,
      content,
    }),

  deleteRepoRule: (owner: string, repo: string, id: number) =>
    deleteJson(`/api/repos/${owner}/${repo}/rules/${id}`),
}
