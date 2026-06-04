import { ChevronRight, ExternalLink, ShieldAlert } from "lucide-react"
import { Fragment, useMemo, useState } from "react"
import { Link } from "react-router"

import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { api, type OrgVulnerabilityModel } from "@/lib/api"
import { useAsync } from "@/lib/hooks"

const SEVERITY_LABELS: Record<string, string> = {
  critical: "Critical",
  high: "High",
  moderate: "Moderate",
  low: "Low",
  unknown: "Unknown",
}

const SEVERITY_STYLES: Record<string, string> = {
  critical: "border-red-500/50 text-red-400",
  high: "border-orange-500/50 text-orange-400",
  moderate: "border-yellow-500/50 text-yellow-400",
  low: "border-zinc-500/50 text-muted-foreground",
  unknown: "border-zinc-500/30 text-muted-foreground",
}

// critical > high > moderate > low > unknown — used to pick the row badge.
const SEVERITY_RANK: Record<string, number> = {
  critical: 4,
  high: 3,
  moderate: 2,
  low: 1,
  unknown: 0,
}

// Compare two version strings numerically when each segment parses as an int,
// falling back to lexicographic for non-numeric segments (e.g. `4.12.18-rc.1`).
// Returns >0 if a > b, <0 if a < b, 0 if equal. Empty strings sort lowest.
function compareVersions(a: string, b: string): number {
  if (!a && !b) return 0
  if (!a) return -1
  if (!b) return 1
  const partsA = a.split(/[.\-+]/)
  const partsB = b.split(/[.\-+]/)
  const len = Math.max(partsA.length, partsB.length)
  for (let i = 0; i < len; i++) {
    const pa = partsA[i] ?? ""
    const pb = partsB[i] ?? ""
    const na = Number(pa)
    const nb = Number(pb)
    if (!Number.isNaN(na) && !Number.isNaN(nb)) {
      if (na !== nb) return na - nb
    } else {
      if (pa !== pb) return pa < pb ? -1 : 1
    }
  }
  return 0
}

interface VulnGroup {
  key: string
  owner: string
  repo: string
  package_name: string
  package_version: string
  advisories: OrgVulnerabilityModel[]
  topSeverity: string
  maxFixedIn: string
}

function groupVulns(vulns: OrgVulnerabilityModel[]): VulnGroup[] {
  const map = new Map<string, VulnGroup>()
  for (const v of vulns) {
    const key = `${v.owner}/${v.repo}::${v.package_name}@${v.package_version}`
    let g = map.get(key)
    if (!g) {
      g = {
        key,
        owner: v.owner,
        repo: v.repo,
        package_name: v.package_name,
        package_version: v.package_version,
        advisories: [],
        topSeverity: v.severity,
        maxFixedIn: v.fixed_in || "",
      }
      map.set(key, g)
    }
    g.advisories.push(v)
    if ((SEVERITY_RANK[v.severity] ?? 0) > (SEVERITY_RANK[g.topSeverity] ?? 0)) {
      g.topSeverity = v.severity
    }
    if (v.fixed_in && compareVersions(v.fixed_in, g.maxFixedIn) > 0) {
      g.maxFixedIn = v.fixed_in
    }
  }
  // Sort groups: highest severity first, then most advisories.
  return Array.from(map.values()).sort((a, b) => {
    const r = (SEVERITY_RANK[b.topSeverity] ?? 0) - (SEVERITY_RANK[a.topSeverity] ?? 0)
    if (r !== 0) return r
    return b.advisories.length - a.advisories.length
  })
}

export function VulnerabilitiesPage() {
  const { data: vulns, loading } = useAsync<OrgVulnerabilityModel[]>(
    () => api.listOrgVulnerabilities().catch(() => []),
    [],
  )

  const total = vulns?.length ?? 0
  const counts = (vulns ?? []).reduce<Record<string, number>>((acc, v) => {
    acc[v.severity] = (acc[v.severity] ?? 0) + 1
    return acc
  }, {})

  const groups = useMemo(() => groupVulns(vulns ?? []), [vulns])
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const toggle = (key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Vulnerabilities</h1>
        <p className="text-sm text-muted-foreground">
          Open advisories across every indexed repo. Sourced from OSV.dev and
          refreshed hourly.
        </p>
      </div>

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-base">
                {loading ? (
                  <Skeleton className="h-5 w-40" />
                ) : (
                  `${total} open ${total === 1 ? "advisory" : "advisories"}`
                )}
              </CardTitle>
              {total > 0 && (
                <CardDescription className="flex flex-wrap gap-x-4 gap-y-1 pt-1 text-xs">
                  {(["critical", "high", "moderate", "low", "unknown"] as const).map(
                    (sev) =>
                      counts[sev] ? (
                        <span key={sev}>
                          <span className={`font-semibold ${SEVERITY_STYLES[sev].split(" ")[1]}`}>
                            {counts[sev]}
                          </span>{" "}
                          {SEVERITY_LABELS[sev]}
                        </span>
                      ) : null,
                  )}
                </CardDescription>
              )}
            </div>
          </div>
        </CardHeader>
        <CardContent className="px-0 pb-0">
          {loading ? (
            <div className="space-y-3 px-6 py-4">
              {Array.from({ length: 5 }).map((_, i) => (
                <div key={i} className="flex items-center gap-4">
                  <Skeleton className="h-5 w-16" />
                  <Skeleton className="h-4 flex-1 max-w-[200px]" />
                  <Skeleton className="hidden h-4 w-20 md:block" />
                  <Skeleton className="h-4 w-32" />
                </div>
              ))}
            </div>
          ) : total === 0 ? (
            <div className="flex flex-col items-center gap-2 px-6 py-12 text-center">
              <ShieldAlert className="h-8 w-8 text-muted-foreground" />
              <p className="text-sm font-medium">No known vulnerabilities</p>
              <p className="text-sm text-muted-foreground">
                Every indexed package is clean. The OSV poller will refresh this
                hourly.
              </p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[40px] pl-6"></TableHead>
                  <TableHead className="w-[100px]">Severity</TableHead>
                  <TableHead>Package</TableHead>
                  <TableHead className="w-[120px]">Version</TableHead>
                  <TableHead>Advisories</TableHead>
                  <TableHead className="hidden md:table-cell">Upgrade to</TableHead>
                  <TableHead className="pr-6">Repo</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {groups.map((g) => {
                  const isOpen = expanded.has(g.key)
                  const advisoryCount = g.advisories.length
                  return (
                    <Fragment key={g.key}>
                      <TableRow
                        className="cursor-pointer"
                        onClick={() => toggle(g.key)}
                      >
                        <TableCell className="pl-6">
                          <ChevronRight
                            className={`h-4 w-4 text-muted-foreground transition-transform ${isOpen ? "rotate-90" : ""}`}
                          />
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant="outline"
                            className={`text-[10px] ${SEVERITY_STYLES[g.topSeverity] ?? ""}`}
                          >
                            {SEVERITY_LABELS[g.topSeverity] ?? g.topSeverity}
                          </Badge>
                        </TableCell>
                        <TableCell className="font-mono text-sm">
                          {g.package_name}
                        </TableCell>
                        <TableCell className="font-mono text-xs text-muted-foreground">
                          {g.package_version || "—"}
                        </TableCell>
                        <TableCell className="text-sm">
                          {advisoryCount} {advisoryCount === 1 ? "advisory" : "advisories"}
                        </TableCell>
                        <TableCell className="hidden font-mono text-xs md:table-cell">
                          {g.maxFixedIn || "—"}
                        </TableCell>
                        <TableCell className="pr-6">
                          <Link
                            to={`/repos/${g.owner}/${g.repo}`}
                            onClick={(e) => e.stopPropagation()}
                            className="text-sm hover:underline"
                          >
                            {g.owner}/{g.repo}
                          </Link>
                        </TableCell>
                      </TableRow>
                      {isOpen &&
                        g.advisories.map((v, i) => (
                          <TableRow
                            key={`${g.key}-${v.cve_id}-${i}`}
                            className="border-t-0 bg-muted/30 hover:bg-muted/40"
                          >
                            <TableCell className="pl-6"></TableCell>
                            <TableCell>
                              <Badge
                                variant="outline"
                                className={`text-[10px] ${SEVERITY_STYLES[v.severity] ?? ""}`}
                              >
                                {SEVERITY_LABELS[v.severity] ?? v.severity}
                              </Badge>
                            </TableCell>
                            <TableCell colSpan={2}>
                              {v.advisory_url ? (
                                <a
                                  href={v.advisory_url}
                                  target="_blank"
                                  rel="noreferrer"
                                  onClick={(e) => e.stopPropagation()}
                                  className="inline-flex items-center gap-1 font-mono text-xs hover:underline"
                                >
                                  {v.cve_id || "advisory"}
                                  <ExternalLink className="h-3 w-3" />
                                </a>
                              ) : (
                                <span className="font-mono text-xs">{v.cve_id || "—"}</span>
                              )}
                              {v.summary && (
                                <p className="mt-0.5 line-clamp-1 text-xs text-muted-foreground">
                                  {v.summary}
                                </p>
                              )}
                            </TableCell>
                            <TableCell className="text-xs text-muted-foreground"></TableCell>
                            <TableCell className="hidden font-mono text-xs text-muted-foreground md:table-cell">
                              {v.fixed_in || "—"}
                            </TableCell>
                            <TableCell className="pr-6"></TableCell>
                          </TableRow>
                        ))}
                    </Fragment>
                  )
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
