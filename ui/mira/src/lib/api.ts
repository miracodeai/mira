// API client. The methods are organized into per-domain modules under
// `lib/api/`; this file composes them into the single `api` object and
// re-exports the shared types, so `import { api, SomeType } from "@/lib/api"`
// keeps working everywhere.

import { packagesApi } from "./api/packages"
import { relationshipsApi } from "./api/relationships"
import { reposApi } from "./api/repos"
import { rulesApi } from "./api/rules"
import { settingsApi } from "./api/settings"
import { statsApi } from "./api/stats"
import { systemApi } from "./api/system"
import { usersApi } from "./api/users"
import { vulnerabilitiesApi } from "./api/vulnerabilities"
import { webhooksApi } from "./api/webhooks"

export * from "./api/types"

export const api = {
  ...systemApi,
  ...settingsApi,
  ...statsApi,
  ...reposApi,
  ...packagesApi,
  ...vulnerabilitiesApi,
  ...relationshipsApi,
  ...rulesApi,
  ...usersApi,
  ...webhooksApi,
}
