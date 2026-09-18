export function normalizeRepoOwner(owner: string): string {
  for (const platform of ["gitlab", "forgejo"]) {
    const prefix = `_${platform}/`
    if (owner.startsWith(prefix)) return owner.slice(prefix.length)
  }
  return owner
}

export function splitRepoKey(
  repoKey: string
): [owner: string, repo: string] | null {
  const segments = repoKey.split("/")
  const repo = segments.pop()
  if (!repo || segments.length === 0 || segments.some((segment) => !segment))
    return null
  return [segments.join("/"), repo]
}
