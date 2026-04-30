import { ExternalLink, ShieldAlert } from "lucide-react"
import { Link } from "react-router"

import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
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
                {loading ? "Loading…" : `${total} open ${total === 1 ? "advisory" : "advisories"}`}
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
          {!loading && total === 0 ? (
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
                  <TableHead className="w-[100px] pl-6">Severity</TableHead>
                  <TableHead>Package</TableHead>
                  <TableHead className="w-[120px]">Version</TableHead>
                  <TableHead>Advisory</TableHead>
                  <TableHead className="hidden md:table-cell">Fixed in</TableHead>
                  <TableHead className="pr-6">Repo</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(vulns ?? []).map((v, i) => (
                  <TableRow key={`${v.owner}-${v.repo}-${v.cve_id}-${v.package_name}-${i}`}>
                    <TableCell className="pl-6">
                      <Badge
                        variant="outline"
                        className={`text-[10px] ${SEVERITY_STYLES[v.severity] ?? ""}`}
                      >
                        {SEVERITY_LABELS[v.severity] ?? v.severity}
                      </Badge>
                    </TableCell>
                    <TableCell className="font-mono text-sm">
                      {v.package_name}
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {v.package_version || "—"}
                    </TableCell>
                    <TableCell>
                      {v.advisory_url ? (
                        <a
                          href={v.advisory_url}
                          target="_blank"
                          rel="noreferrer"
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
                    <TableCell className="hidden font-mono text-xs text-muted-foreground md:table-cell">
                      {v.fixed_in || "—"}
                    </TableCell>
                    <TableCell className="pr-6">
                      <Link
                        to={`/repos/${v.owner}/${v.repo}`}
                        className="text-sm hover:underline"
                      >
                        {v.owner}/{v.repo}
                      </Link>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
