import { ChevronRight, ShieldAlert, Search } from "lucide-react"
import { Fragment, useMemo, useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import type { PackageModel, PackageVersionModel, VulnerabilityModel } from "@/lib/api"

const SEVERITY_RANK: Record<string, number> = {
  critical: 0,
  high: 1,
  moderate: 2,
  low: 3,
  unknown: 4,
}

const SEVERITY_STYLE: Record<string, string> = {
  critical: "text-red-300 border-red-500/60 bg-red-500/10",
  high: "text-orange-300 border-orange-500/60 bg-orange-500/10",
  moderate: "text-yellow-300 border-yellow-500/60 bg-yellow-500/10",
  low: "text-zinc-300 border-zinc-500/60 bg-zinc-500/10",
  unknown: "text-muted-foreground border-border",
}

const KIND_LABELS: Record<string, string> = {
  npm: "npm",
  pip: "pip",
  docker: "Docker",
  go: "Go",
  rust: "Cargo",
  composer: "Composer",
}

function kindBadgeVariant(kind: string) {
  // Keep monochrome-ish to match the app; use outline variants with colored
  // text via className so each kind is still distinguishable at a glance.
  const colors: Record<string, string> = {
    npm: "text-red-400 border-red-500/40",
    pip: "text-yellow-400 border-yellow-500/40",
    docker: "text-blue-400 border-blue-500/40",
    go: "text-cyan-400 border-cyan-500/40",
    rust: "text-orange-400 border-orange-500/40",
    composer: "text-purple-400 border-purple-500/40",
  }
  return colors[kind] ?? "text-muted-foreground border-border"
}

type DevFilter = "all" | "prod" | "dev"

function indexedVersions(pkg: PackageModel): PackageVersionModel[] {
  return pkg.versions?.length
    ? pkg.versions
    : [
        {
          version: pkg.version,
          sources: [
            { name: pkg.name, file_path: pkg.file_path, is_dev: pkg.is_dev },
          ],
        },
      ]
}

function vulnerabilityKey(kind: string, name: string, version: string): string {
  return JSON.stringify([kind, name, version])
}

function VulnerabilityBadge({ items }: { items: VulnerabilityModel[] }) {
  if (!items.length) return null
  const { severity } = items.reduce((highest, item) =>
    (SEVERITY_RANK[item.severity] ?? 9) < (SEVERITY_RANK[highest.severity] ?? 9)
      ? item
      : highest
  )
  const count = new Set(items.map((item) => item.cve_id)).size
  return (
    <Badge
      variant="outline"
      className={`gap-1 text-[10px] ${SEVERITY_STYLE[severity] ?? ""}`}
      title={items
        .map(
          (item) => `${item.package_version} · ${item.cve_id}: ${item.summary}`
        )
        .join("\n")}
    >
      <ShieldAlert className="h-3 w-3" />
      {count} {severity}
    </Badge>
  )
}

export function DependenciesTable({
  packages,
  vulnerabilities = [],
}: {
  packages: PackageModel[]
  vulnerabilities?: VulnerabilityModel[]
}) {
  const [query, setQuery] = useState("")
  const [activeKind, setActiveKind] = useState<string | null>(null)
  const [devFilter, setDevFilter] = useState<DevFilter>("all")
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())

  // Group vulnerabilities by (ecosystem, package, version) for fast lookup
  // when rendering each table row.
  const vulnsByKey = useMemo(() => {
    const map = new Map<string, VulnerabilityModel[]>()
    for (const v of vulnerabilities) {
      const key = vulnerabilityKey(v.ecosystem, v.package_name, v.package_version)
      const list = map.get(key)
      if (list) list.push(v)
      else map.set(key, [v])
    }
    return map
  }, [vulnerabilities])

  const availableKinds = useMemo(() => {
    const kinds = new Set<string>()
    for (const p of packages) kinds.add(p.kind)
    return [...kinds].sort()
  }, [packages])

  const devCounts = useMemo(() => {
    let prod = 0
    let dev = 0
    for (const pkg of packages) {
      const sources = indexedVersions(pkg).flatMap((version) => version.sources)
      if (sources.some((source) => !source.is_dev)) prod++
      if (sources.some((source) => source.is_dev)) dev++
    }
    return { prod, dev }
  }, [packages])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return packages.flatMap((pkg) => {
      if (activeKind && pkg.kind !== activeKind) return []
      const matchesName = pkg.name.toLowerCase().includes(q)
      const versions = indexedVersions(pkg).flatMap((version) => {
        const matchesVersion =
          matchesName || version.version.toLowerCase().includes(q)
        const sources = version.sources.filter((source) => {
          if (devFilter === "prod" && source.is_dev) return false
          if (devFilter === "dev" && !source.is_dev) return false
          return matchesVersion || source.file_path.toLowerCase().includes(q)
        })
        return sources.length ? [{ ...version, sources }] : []
      })
      return versions.length ? [{ pkg, versions }] : []
    })
  }, [packages, query, activeKind, devFilter])

  const toggleExpanded = (key: string) => {
    setExpanded((current) => {
      const next = new Set(current)
      if (!next.delete(key)) next.add(key)
      return next
    })
  }

  if (packages.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-6 text-center">
        <p className="text-sm font-medium">No packages detected</p>
        <p className="mt-1 text-sm text-muted-foreground">
          Mira parses package.json, requirements.txt, pyproject.toml, go.mod,
          composer.json, and Dockerfile. Re-index this repo to populate.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[200px] max-w-sm">
          <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search packages..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="pl-8"
          />
        </div>
        <div className="flex flex-wrap items-center gap-1">
          <button
            type="button"
            onClick={() => setActiveKind(null)}
            className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
              activeKind === null
                ? "bg-foreground text-background"
                : "text-muted-foreground hover:bg-muted"
            }`}
          >
            All ({packages.length})
          </button>
          {availableKinds.map((kind) => {
            const count = packages.filter((p) => p.kind === kind).length
            const active = activeKind === kind
            return (
              <button
                key={kind}
                type="button"
                onClick={() => setActiveKind(active ? null : kind)}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                  active
                    ? "bg-foreground text-background"
                    : "text-muted-foreground hover:bg-muted"
                }`}
              >
                {KIND_LABELS[kind] ?? kind} ({count})
              </button>
            )
          })}
        </div>
        <div className="ml-auto inline-flex rounded-md border">
          {(
            [
              ["all", "All", packages.length],
              ["prod", "Has prod sources", devCounts.prod],
              ["dev", "Has dev sources", devCounts.dev],
            ] as const
          ).map(([value, label, count]) => {
            const active = devFilter === value
            return (
              <button
                key={value}
                type="button"
                onClick={() => setDevFilter(value)}
                className={`px-2.5 py-1 text-xs font-medium transition-colors first:rounded-l-md last:rounded-r-md ${
                  active
                    ? "bg-foreground text-background"
                    : "text-muted-foreground hover:bg-muted"
                }`}
              >
                {label}{" "}
                <span
                  className={
                    active ? "opacity-70" : "text-muted-foreground/70"
                  }
                >
                  {count}
                </span>
              </button>
            )
          })}
        </div>
      </div>

      {/* Table */}
      <div className="overflow-hidden rounded-lg border">
        <table className="w-full text-sm">
          <thead className="bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
            <tr>
              <th className="w-20 px-4 py-2 text-left font-medium">Kind</th>
              <th className="px-4 py-2 text-left font-medium">Package</th>
              <th className="w-40 px-4 py-2 text-left font-medium">Version</th>
              <th className="px-4 py-2 text-left font-medium">Source</th>
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 ? (
              <tr>
                <td colSpan={4} className="px-4 py-6 text-center text-muted-foreground">
                  No packages match your filter.
                </td>
              </tr>
            ) : (
              filtered.map(({ pkg: p, versions }) => {
                const key = JSON.stringify([p.kind, p.name.toLowerCase()])
                const version = versions[0]
                const source =
                  version.sources.find(
                    (item) =>
                      item.name === p.name &&
                      item.file_path === p.file_path &&
                      item.is_dev === p.is_dev
                  ) ?? version.sources[0]
                const allDev = versions.every((entry) =>
                  entry.sources.every((item) => item.is_dev)
                )
                const expandable =
                  versions.length > 1 || version.sources.length > 1
                const isExpanded = expanded.has(key)
                const vulnerabilitiesFor = (entry: PackageVersionModel) =>
                  [...new Set(entry.sources.map((item) => item.name))].flatMap(
                    (name) =>
                      vulnsByKey.get(
                        vulnerabilityKey(p.kind, name, entry.version)
                      ) ?? []
                  )
                return (
                  <Fragment key={key}>
                    <tr className="border-t">
                      <td className="px-4 py-2.5">
                        <Badge
                          variant="outline"
                          className={`text-[10px] ${kindBadgeVariant(p.kind)}`}
                        >
                          {KIND_LABELS[p.kind] ?? p.kind}
                        </Badge>
                      </td>
                      <td className="px-4 py-2.5 font-mono text-xs">
                        <div className="flex items-center gap-2">
                          <span className="font-medium">{p.name}</span>
                          {allDev && (
                            <span className="text-[10px] tracking-wide text-muted-foreground">
                              dev
                            </span>
                          )}
                          <VulnerabilityBadge
                            items={versions.flatMap(vulnerabilitiesFor)}
                          />
                        </div>
                      </td>
                      <td className="px-4 py-2.5 font-mono text-xs break-all text-muted-foreground">
                        {expandable ? (
                          <button
                            type="button"
                            className="inline-flex items-center gap-1.5 rounded-sm text-left hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                            aria-expanded={isExpanded}
                            onClick={() => toggleExpanded(key)}
                          >
                            <ChevronRight
                              aria-hidden="true"
                              className={`size-3.5 shrink-0 transition-transform ${isExpanded ? "rotate-90" : ""}`}
                            />
                            <span className="sr-only">
                              {isExpanded ? "Hide" : "Show"} versions and
                              sources for {p.name}{" "}
                            </span>
                            <span className="min-w-0 break-all">
                              {version.version || "—"}
                            </span>
                            {versions.length > 1 && (
                              <span className="shrink-0 text-[10px]">
                                +{versions.length - 1}
                              </span>
                            )}
                          </button>
                        ) : (
                          version.version || "—"
                        )}
                      </td>
                      <td className="px-4 py-2.5 font-mono text-xs break-all text-muted-foreground">
                        {source.file_path}
                      </td>
                    </tr>
                    {expandable && isExpanded && (
                      <tr className="border-t bg-muted/20">
                        <td colSpan={4} className="px-4 py-3">
                          <dl className="space-y-2 text-xs">
                            {versions.map((entry) => (
                              <div
                                key={entry.version}
                                className="grid gap-1.5 sm:grid-cols-[10rem_minmax(0,1fr)] sm:gap-3"
                              >
                                <dt className="flex min-w-0 flex-wrap items-start gap-2 font-mono">
                                  <span className="break-all">
                                    {entry.version || "—"}
                                  </span>
                                  <VulnerabilityBadge
                                    items={vulnerabilitiesFor(entry)}
                                  />
                                </dt>
                                <dd>
                                  <ul className="space-y-1 text-muted-foreground">
                                    {entry.sources.map((item) => (
                                      <li
                                        key={JSON.stringify([
                                          item.file_path,
                                          item.name,
                                          item.is_dev,
                                        ])}
                                        className="flex flex-wrap items-baseline gap-2"
                                      >
                                        <span className="font-mono break-all">
                                          {item.file_path}
                                        </span>
                                        {item.name !== p.name && (
                                          <span className="font-mono break-all">
                                            {item.name}
                                          </span>
                                        )}
                                        {item.is_dev && (
                                          <span className="text-[10px]">
                                            dev
                                          </span>
                                        )}
                                      </li>
                                    ))}
                                  </ul>
                                </dd>
                              </div>
                            ))}
                          </dl>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })
            )}
          </tbody>
        </table>
      </div>

      {filtered.length > 0 && filtered.length < packages.length && (
        <p className="text-xs text-muted-foreground">
          Showing {filtered.length} of {packages.length}
        </p>
      )}
    </div>
  )
}
