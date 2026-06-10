import { ArrowDown, ArrowUp, Info, RefreshCw, Search } from "lucide-react"
import { type ReactNode, useState } from "react"
import { useNavigate } from "react-router"
import { BarGauge } from "@/components/dashboard/bar-gauge"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
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
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { type Column, useDataTable } from "@/components/ui/use-data-table"
import { useAuth } from "@/lib/auth"
import { api, type ReviewerStat } from "@/lib/api"
import { useAsync } from "@/lib/hooks"

// ── formatting helpers ──

/** A column header with a dotted underline that reveals an explainer on hover. */
function HeaderTip({ label, tip }: { label: string; tip: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="underline decoration-dotted decoration-muted-foreground/60 underline-offset-4">
          {label}
        </span>
      </TooltipTrigger>
      <TooltipContent>{tip}</TooltipContent>
    </Tooltip>
  )
}

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
  tip,
}: {
  label: string
  value: string | number
  footer?: ReactNode
  loading: boolean
  tip?: string
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription className="flex items-center gap-1">
          {label}
          {tip && (
            <Tooltip>
              <TooltipTrigger asChild>
                <button type="button" className="text-muted-foreground hover:text-foreground">
                  <Info className="h-3.5 w-3.5" />
                </button>
              </TooltipTrigger>
              <TooltipContent>{tip}</TooltipContent>
            </Tooltip>
          )}
        </CardDescription>
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
      header: (
        <HeaderTip
          label="Median response"
          tip="Approximate for backfilled PRs (request time estimated from PR creation); it sharpens as live review events come in."
        />
      ),
      align: "right",
      sortable: true,
      sortValue: (r) => r.median_response_secs,
      cell: (r) => (
        <span className="tabular-nums">
          {r.median_response_secs == null ? "—" : `~${fmtDuration(r.median_response_secs)}`}
        </span>
      ),
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
    {
      key: "rubber_stamp_rate",
      header: (
        <HeaderTip
          label="Rubber-stamps"
          tip="Approvals with no substantive review: an empty or “LGTM” body and no real inline comments."
        />
      ),
      align: "right",
      sortable: true,
      sortValue: (r) => r.rubber_stamp_rate,
      cell: (r) =>
        r.approvals > 0 ? (
          <div className="flex items-center justify-end gap-2">
            <span className="tabular-nums">{Math.round(r.rubber_stamp_rate)}%</span>
            <BarGauge
              value={r.rubber_stamp_rate}
              max={100}
              tone="heat"
              label={`${r.rubber_stamps} of ${r.approvals} approvals were rubber-stamps (${Math.round(r.rubber_stamp_rate)}%)`}
            />
          </div>
        ) : (
          <span className="text-muted-foreground">—</span>
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
          Each reviewer&apos;s current queue and how quickly they respond once asked
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
          <h1 className="text-2xl font-semibold tracking-tight">Reviewers</h1>
          <p className="text-sm text-muted-foreground">
            Who&apos;s reviewing, how responsive they are, and where approvals get rubber-stamped
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

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
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
          label="Rubber-stamps"
          tip="Approvals with no substantive review: an empty or “LGTM” body and no real inline comments."
          value={summary?.rubber_stamps ?? 0}
          footer={
            summary && summary.approvals > 0
              ? `${Math.round((summary.rubber_stamps / summary.approvals) * 100)}% of ${summary.approvals} approvals`
              : "approvals with no real review"
          }
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
    </div>
  )
}
