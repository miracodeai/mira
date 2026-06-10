import { AlertTriangle, ArrowDown, ArrowUp, ExternalLink, RefreshCw, Search } from "lucide-react"
import { type ReactNode, useState } from "react"
import { useNavigate } from "react-router"
import { BarGauge } from "@/components/dashboard/bar-gauge"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { DataTable, DataTablePagination } from "@/components/ui/data-table"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { type Column, useDataTable } from "@/components/ui/use-data-table"
import { useAuth } from "@/lib/auth"
import { api, type OpenPr, type ReviewerStat } from "@/lib/api"
import { useAsync } from "@/lib/hooks"

// ── formatting helpers ──

function fmtDuration(secs: number | null): string {
  if (secs == null) return "—"
  if (secs < 60) return "<1m"
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) {
    const m = mins % 60
    return m ? `${hours}h ${m}m` : `${hours}h`
  }
  const days = Math.floor(hours / 24)
  const h = hours % 24
  return h ? `${days}d ${h}h` : `${days}d`
}

/** Lower duration = better, so a decrease is green. */
function DurationTrend({ current, previous }: { current: number | null; previous: number | null }) {
  if (current == null || previous == null || previous === 0) {
    return <span className="text-muted-foreground">no prior data</span>
  }
  const delta = current - previous
  if (delta === 0) return <span className="text-muted-foreground">no change vs prev 7d</span>
  const faster = delta < 0
  const pct = Math.round((Math.abs(delta) / previous) * 100)
  const Icon = faster ? ArrowDown : ArrowUp
  return (
    <span className="inline-flex items-center gap-1">
      <span
        className={`inline-flex items-center gap-0.5 font-medium ${
          faster ? "text-emerald-600 dark:text-emerald-500" : "text-red-600 dark:text-red-500"
        }`}
      >
        <Icon className="h-3.5 w-3.5" />
        {pct}%
      </span>
      <span className="text-muted-foreground">vs prev 7d</span>
    </span>
  )
}

function StatCard({
  label,
  value,
  footer,
  loading,
}: {
  label: string
  value: string | number
  footer?: ReactNode
  loading: boolean
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription>{label}</CardDescription>
        <CardTitle className="text-4xl tabular-nums">
          {loading ? <Skeleton className="h-9 w-20" /> : value}
        </CardTitle>
      </CardHeader>
      <CardFooter className="text-sm text-muted-foreground">
        {loading ? <Skeleton className="h-4 w-32" /> : footer}
      </CardFooter>
    </Card>
  )
}

function StatusBadge({ status }: { status: string }) {
  if (status === "changes_requested") return <Badge variant="destructive">Changes requested</Badge>
  if (status === "approved") {
    return (
      <Badge className="border-transparent bg-emerald-600/15 text-emerald-700 dark:text-emerald-400">
        Approved
      </Badge>
    )
  }
  if (status === "commented") return <Badge variant="secondary">Commented</Badge>
  return <Badge variant="outline">Awaiting review</Badge>
}

function ReviewerCell({ login, avatar }: { login: string; avatar: string }) {
  return (
    <div className="flex items-center gap-3">
      <Avatar className="h-8 w-8">
        {avatar && <AvatarImage src={avatar} alt={login} />}
        <AvatarFallback>{login.slice(0, 2).toUpperCase()}</AvatarFallback>
      </Avatar>
      <span className="text-sm font-medium">{login}</span>
    </div>
  )
}

// ── Reviewer responsiveness (the bottleneck) ──

function ReviewersCard() {
  const navigate = useNavigate()
  const [search, setSearch] = useState("")
  const { data, loading, error } = useAsync(() => api.getReviewers(30), [])

  const filtered = (data ?? []).filter((r) => r.reviewer.toLowerCase().includes(search.toLowerCase()))
  const maxPending = Math.max(1, ...(data ?? []).map((r) => r.pending))
  const maxReviews = Math.max(1, ...(data ?? []).map((r) => r.reviews))

  const columns: Column<ReviewerStat>[] = [
    {
      key: "reviewer",
      header: "Reviewer",
      sortable: true,
      sortValue: (r) => r.reviewer.toLowerCase(),
      cell: (r) => <ReviewerCell login={r.reviewer} avatar={r.avatar_url} />,
    },
    {
      key: "pending",
      header: "Pending reviews",
      align: "right",
      sortable: true,
      sortValue: (r) => r.pending,
      cell: (r) => (
        <div className="flex items-center justify-end gap-2">
          <span className="tabular-nums">{r.pending}</span>
          <BarGauge
            value={r.pending}
            max={maxPending}
            tone="heat"
            label={`${r.pending} PRs awaiting their review`}
          />
        </div>
      ),
    },
    {
      key: "median_response_secs",
      header: "Median response",
      align: "right",
      sortable: true,
      sortValue: (r) => r.median_response_secs,
      cell: (r) => <span className="tabular-nums">{fmtDuration(r.median_response_secs)}</span>,
    },
    {
      key: "reviews",
      header: "Reviews (30d)",
      align: "right",
      sortable: true,
      sortValue: (r) => r.reviews,
      cell: (r) => (
        <div className="flex items-center justify-end gap-2">
          <span className="tabular-nums">{r.reviews}</span>
          <BarGauge value={r.reviews} max={maxReviews} label={`${r.reviews} reviews in 30d`} />
        </div>
      ),
    },
  ]

  const table = useDataTable({
    rows: filtered,
    columns,
    initialSort: { key: "pending", dir: "desc" },
    pageSize: 10,
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Reviewer responsiveness</CardTitle>
        <CardDescription>
          Who&apos;s the bottleneck — pending review queue and how fast people respond once asked
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="relative max-w-sm">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search reviewers..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9"
          />
        </div>
        {error ? (
          <p className="text-sm text-destructive">{error}</p>
        ) : (
          <>
            <DataTable
              table={table}
              rowKey={(r) => r.reviewer}
              loading={loading}
              onRowClick={(r) => navigate(`/contributors/${encodeURIComponent(r.reviewer)}`)}
              emptyMessage="No review activity yet."
            />
            <DataTablePagination table={table} />
          </>
        )}
      </CardContent>
    </Card>
  )
}

// ── Open PRs (stale + status board) ──

function OpenPrsCard() {
  const [staleOnly, setStaleOnly] = useState(false)
  const { data, loading, error } = useAsync(() => api.getOpenPrs(3), [])

  const rows = (data ?? []).filter((p) => !staleOnly || p.stale)

  const columns: Column<OpenPr>[] = [
    {
      key: "title",
      header: "Pull request",
      sortable: true,
      sortValue: (p) => p.title.toLowerCase(),
      cell: (p) => (
        <div className="min-w-0">
          <a
            href={p.url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 font-medium hover:underline"
            onClick={(e) => e.stopPropagation()}
          >
            <span className="truncate">{p.title || `PR #${p.number}`}</span>
            <ExternalLink className="h-3 w-3 shrink-0 text-muted-foreground" />
          </a>
          <p className="text-xs text-muted-foreground">
            {p.repo} #{p.number} · by {p.author}
            {p.draft && " · draft"}
          </p>
        </div>
      ),
    },
    {
      key: "status",
      header: "Status",
      sortable: true,
      sortValue: (p) => p.status,
      cell: (p) => <StatusBadge status={p.status} />,
    },
    {
      key: "waiting_on",
      header: "Waiting on",
      cell: (p) =>
        p.waiting_on.length ? (
          <span className="text-sm">{p.waiting_on.join(", ")}</span>
        ) : (
          <span className="text-sm text-muted-foreground">—</span>
        ),
    },
    {
      key: "age_secs",
      header: "Age",
      align: "right",
      sortable: true,
      sortValue: (p) => p.age_secs,
      cell: (p) => <span className="tabular-nums">{fmtDuration(p.age_secs)}</span>,
    },
    {
      key: "idle_secs",
      header: "Idle",
      align: "right",
      sortable: true,
      sortValue: (p) => p.idle_secs,
      cell: (p) => (
        <span className="inline-flex items-center justify-end gap-1.5 tabular-nums">
          {p.stale && <AlertTriangle className="h-3.5 w-3.5 text-amber-500" />}
          {fmtDuration(p.idle_secs)}
        </span>
      ),
    },
  ]

  const table = useDataTable({
    rows,
    columns,
    initialSort: { key: "age_secs", dir: "desc" },
    pageSize: 10,
  })

  const staleCount = (data ?? []).filter((p) => p.stale).length

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-4">
          <div>
            <CardTitle>Open pull requests</CardTitle>
            <CardDescription>
              How long PRs have been open and sitting idle, and who they&apos;re waiting on
            </CardDescription>
          </div>
          <div className="flex gap-1">
            {(["all", "stale"] as const).map((mode) => {
              const active = (mode === "stale") === staleOnly
              return (
                <button
                  key={mode}
                  onClick={() => setStaleOnly(mode === "stale")}
                  className={`inline-flex h-8 items-center rounded-md border px-3 text-xs font-medium ${
                    active
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-input bg-background text-muted-foreground hover:bg-accent"
                  }`}
                >
                  {mode === "all" ? "All open" : `Stale (${staleCount})`}
                </button>
              )
            })}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {error ? (
          <p className="text-sm text-destructive">{error}</p>
        ) : (
          <>
            <DataTable
              table={table}
              rowKey={(p) => `${p.owner}/${p.repo}#${p.number}`}
              loading={loading}
              emptyMessage={staleOnly ? "No stale PRs — nice." : "No open PRs."}
            />
            <DataTablePagination table={table} />
          </>
        )}
      </CardContent>
    </Card>
  )
}

// ── Page ──

export function ContributorsPage() {
  const { user } = useAuth()
  const [refreshing, setRefreshing] = useState(false)
  const [refreshError, setRefreshError] = useState<string | null>(null)

  const { data: summary, loading: summaryLoading } = useAsync(
    () => api.getReviewSummary(7, 3).catch(() => null),
    [],
  )

  const onRefresh = async () => {
    setRefreshing(true)
    setRefreshError(null)
    try {
      await api.refreshContributors()
    } catch (err) {
      setRefreshError(err instanceof Error ? err.message : "Refresh failed")
    } finally {
      setRefreshing(false)
    }
  }

  const cur = summary?.current
  const prev = summary?.previous

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Review health</h1>
          <p className="text-sm text-muted-foreground">
            Who&apos;s reviewing, where the bottlenecks are, and which PRs are stuck
          </p>
        </div>
        {user?.is_admin && (
          <Button variant="outline" size="sm" onClick={onRefresh} disabled={refreshing}>
            <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
            Refresh from GitHub
          </Button>
        )}
      </div>

      {refreshError && <p className="text-sm text-destructive">{refreshError}</p>}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <StatCard
          label="Open PRs"
          value={summary?.open_prs ?? 0}
          footer={`${summary?.awaiting_review ?? 0} awaiting first review`}
          loading={summaryLoading}
        />
        <StatCard
          label="Stale PRs"
          value={summary?.stale_prs ?? 0}
          footer="idle more than 3 days"
          loading={summaryLoading}
        />
        <StatCard
          label="Approved & merged"
          value={summary?.approved_merged ?? 0}
          footer={`of ${summary?.merged ?? 0} merged this week`}
          loading={summaryLoading}
        />
        <StatCard
          label="Median time to first review"
          value={fmtDuration(cur?.time_to_first_review_secs ?? null)}
          footer={
            <DurationTrend
              current={cur?.time_to_first_review_secs ?? null}
              previous={prev?.time_to_first_review_secs ?? null}
            />
          }
          loading={summaryLoading}
        />
        <StatCard
          label="Median time to merge"
          value={fmtDuration(cur?.time_to_merge_secs ?? null)}
          footer={
            <DurationTrend
              current={cur?.time_to_merge_secs ?? null}
              previous={prev?.time_to_merge_secs ?? null}
            />
          }
          loading={summaryLoading}
        />
      </div>

      <ReviewersCard />
      <OpenPrsCard />
    </div>
  )
}
