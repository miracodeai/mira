import { fetchJson } from "./http"
import type { PackageSearchHit } from "./types"

// Cross-repo package search.
export const packagesApi = {
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
    return fetchJson<PackageSearchHit[]>(
      `/api/packages/search?${qs.toString()}`
    )
  },
}
