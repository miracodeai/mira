import { ShieldAlert, Search } from "lucide-react"
import { useMemo, useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import type { PackageModel, VulnerabilityModel } from "@/lib/api"

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
  }
  return colors[kind] ?? "text-muted-foreground border-border"
}

type DevFilter = "all" | "prod" | "dev"

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

  // Group vulnerabilities by (ecosystem, package, version) for fast lookup
  // when rendering each table row.
  const vulnsByKey = useMemo(() => {
    const map = new Map<string, VulnerabilityModel[]>()
    for (const v of vulnerabilities) {
      const key = `${v.ecosystem}::${v.package_name}::${v.package_version}`
      const list = map.get(key)
      if (list) list.push(v)
      else map.set(key, [v])
    }
    return map
  }, [vulnerabilities])

  const highestSeverity = (key: string): string | null => {
    const list = vulnsByKey.get(key)
    if (!list || list.length === 0) return null
    return [...list].sort(
      (a, b) => (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9),
    )[0].severity
  }

  const availableKinds = useMemo(() => {
    const kinds = new Set<string>()
    for (const p of packages) kinds.add(p.kind)
    return [...kinds].sort()
  }, [packages])

  const devCounts = useMemo(() => {
    const prod = packages.filter((p) => !p.is_dev).length
    return { prod, dev: packages.length - prod }
  }, [packages])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return packages.filter((p) => {
      if (activeKind && p.kind !== activeKind) return false
      if (devFilter === "prod" && p.is_dev) return false
      if (devFilter === "dev" && !p.is_dev) return false
      if (!q) return true
      return (
        p.name.toLowerCase().includes(q) ||
        p.version.toLowerCase().includes(q) ||
        p.file_path.toLowerCase().includes(q)
      )
    })
  }, [packages, query, activeKind, devFilter])

  if (packages.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-6 text-center">
        <p className="text-sm font-medium">No packages detected</p>
        <p className="mt-1 text-sm text-muted-foreground">
          Mira parses package.json, requirements.txt, pyproject.toml, go.mod,
          and Dockerfile. Re-index this repo to populate.
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
              ["prod", "Prod", devCounts.prod],
              ["dev", "Dev", devCounts.dev],
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
              filtered.map((p, i) => {
                const key = `${p.kind}::${p.name}::${p.version}`
                const vulns = vulnsByKey.get(key) ?? []
                const sev = highestSeverity(key)
                return (
                  <tr
                    key={`${p.kind}-${p.name}-${p.file_path}-${i}`}
                    className="border-t"
                  >
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
                        {p.is_dev && (
                          <span className="text-[10px] tracking-wide text-muted-foreground">
                            dev
                          </span>
                        )}
                        {sev && (
                          <Badge
                            variant="outline"
                            className={`gap-1 text-[10px] ${SEVERITY_STYLE[sev] ?? ""}`}
                            title={vulns
                              .map((v) => `${v.cve_id}: ${v.summary}`)
                              .join("\n")}
                          >
                            <ShieldAlert className="h-3 w-3" />
                            {vulns.length} {sev}
                          </Badge>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted-foreground">
                      {p.version || "—"}
                    </td>
                    <td className="px-4 py-2.5 font-mono text-xs text-muted-foreground">
                      {p.file_path}
                    </td>
                  </tr>
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
