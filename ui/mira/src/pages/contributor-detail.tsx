import { ChevronLeft } from "lucide-react"
import { Link, useParams } from "react-router"

import { BarGauge } from "@/components/dashboard/bar-gauge"
import { ContributionHeatmap } from "@/components/dashboard/contribution-heatmap"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { api } from "@/lib/api"
import { useAsync } from "@/lib/hooks"

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <Card>
      <CardContent className="p-4">
        <p className="text-2xl font-semibold tabular-nums">{value}</p>
        <p className="text-xs text-muted-foreground">{label}</p>
      </CardContent>
    </Card>
  )
}

function fmtDuration(secs: number | null): string {
  if (secs == null) return "—"
  if (secs < 60) return "<1m"
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h`
  return `${Math.floor(hours / 24)}d`
}

export function ContributorDetailPage() {
  const { login } = useParams<{ login: string }>()
  const { data, loading, error } = useAsync(() => api.getContributor(login!), [login])
  // Review responsiveness for this person (leads the page).
  const { data: reviewers } = useAsync(() => api.getReviewers(30).catch(() => []), [login])

  if (loading) {
    return (
      <div className="space-y-6 p-6">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-28 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    )
  }

  if (error || !data) {
    return (
      <div className="space-y-4 p-6">
        <Link
          to="/contributors"
          className="inline-flex items-center text-sm text-muted-foreground hover:text-foreground"
        >
          <ChevronLeft className="h-4 w-4" />
          Reviewers
        </Link>
        <p className="text-sm text-destructive">{error ?? "Contributor not found"}</p>
      </div>
    )
  }

  const { contributor: c, heatmap, repos, quality } = data
  const initials = c.login.slice(0, 2).toUpperCase()
  const acceptPct = Math.round(quality.accept_rate * 100)
  const maxRepoCommits = Math.max(1, ...repos.map((r) => r.commits))
  const me = (reviewers ?? []).find((r) => r.reviewer === c.login)

  return (
    <div className="space-y-6 p-6">
      <Link
        to="/contributors"
        className="inline-flex items-center text-sm text-muted-foreground hover:text-foreground"
      >
        <ChevronLeft className="h-4 w-4" />
        Reviewers
      </Link>

      {/* Header */}
      <div className="flex items-center gap-4">
        <Avatar className="h-14 w-14">
          {c.avatar_url && <AvatarImage src={c.avatar_url} alt={c.login} />}
          <AvatarFallback>{initials}</AvatarFallback>
        </Avatar>
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            {c.login}
            {c.is_bot && <Badge variant="outline">bot</Badge>}
          </h1>
          {c.display_name && <p className="text-sm text-muted-foreground">{c.display_name}</p>}
        </div>
      </div>

      {/* Review responsiveness — leads the page */}
      <div className="space-y-2">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Reviewing
        </p>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Stat label="Pending reviews" value={me?.pending ?? 0} />
          <Stat
            label="Median response"
            value={
              me?.median_response_secs == null ? "—" : `~${fmtDuration(me.median_response_secs)}`
            }
          />
          <Stat label="Reviews given (30d)" value={me?.reviews ?? 0} />
          <Stat label="Reviews given (all time)" value={c.reviews.toLocaleString()} />
        </div>
      </div>

      {/* Authoring — secondary */}
      <div className="space-y-2">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Authoring
        </p>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
          <Stat label="Commits" value={c.commits.toLocaleString()} />
          <Stat label="PRs opened" value={c.prs_opened.toLocaleString()} />
          <Stat label="PRs merged" value={c.prs_merged.toLocaleString()} />
          <Stat label="Lines added" value={c.additions.toLocaleString()} />
          <Stat label="Repos" value={c.repos_touched} />
        </div>
      </div>

      {/* Heatmap */}
      <Card>
        <CardHeader>
          <CardTitle>Contribution activity</CardTitle>
          <CardDescription>Commits, PRs, and reviews over the last year</CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          <ContributionHeatmap days={heatmap} />
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* Per-repo breakdown */}
        <Card>
          <CardHeader>
            <CardTitle>Where they contribute</CardTitle>
            <CardDescription>Activity per repository</CardDescription>
          </CardHeader>
          <CardContent>
            {repos.length > 0 ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Repository</TableHead>
                    <TableHead className="text-right">Commits</TableHead>
                    <TableHead className="text-right">PRs</TableHead>
                    <TableHead className="text-right">Reviews</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {repos.map((r) => (
                    <TableRow key={`${r.owner}/${r.repo}`}>
                      <TableCell>
                        <Link
                          to={`/repos/${r.owner}/${r.repo}`}
                          className="font-medium hover:underline"
                        >
                          {r.owner}/{r.repo}
                        </Link>
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center justify-end gap-2">
                          <span className="tabular-nums">{r.commits}</span>
                          <BarGauge
                            value={r.commits}
                            max={maxRepoCommits}
                            label={`${r.commits} commits`}
                          />
                        </div>
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {r.prs_merged}/{r.prs_opened}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{r.reviews}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <p className="text-sm text-muted-foreground">No per-repo activity recorded.</p>
            )}
          </CardContent>
        </Card>

        {/* Review quality — Mira's differentiated signal */}
        <Card>
          <CardHeader>
            <CardTitle>Review quality</CardTitle>
            <CardDescription>How Mira&apos;s reviews landed on their PRs</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-3 gap-4">
              <Stat label="Blockers raised" value={quality.blockers.toLocaleString()} />
              <Stat label="Warnings raised" value={quality.warnings.toLocaleString()} />
              <Stat label="Suggestions" value={quality.suggestions.toLocaleString()} />
            </div>
            <div className="rounded-lg border p-4">
              <div className="flex items-baseline justify-between">
                <p className="text-sm font-medium">Feedback accept rate</p>
                <p className="text-sm tabular-nums text-muted-foreground">
                  {quality.feedback_accepted}/
                  {quality.feedback_accepted + quality.feedback_rejected} accepted
                </p>
              </div>
              <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-muted">
                <div className="h-full bg-primary" style={{ width: `${acceptPct}%` }} />
              </div>
              <p className="mt-1 text-xs text-muted-foreground">{acceptPct}% accepted</p>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
