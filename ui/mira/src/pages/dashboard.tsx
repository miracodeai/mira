import { useEffect, useState } from "react"
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  XAxis,
  YAxis,
} from "recharts"

import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  type ChartConfig,
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
} from "@/components/ui/chart"
import { api } from "@/lib/api"
import { useAsync } from "@/lib/hooks"

export function DashboardPage() {
  const [period, setPeriod] = useState<"day" | "week" | "month">("day")

  const { data: stats } = useAsync(() => api.getOrgStats(), [])
  const { data: timeseries } = useAsync(
    () => api.getTimeseries(period),
    [period],
  )
  // Vulnerability widget — populated by the OSV poller. Swallow errors so a
  // missing endpoint or transient failure just hides the card.
  const { data: vulnSummary } = useAsync(
    () => api.getVulnerabilitiesSummary().catch(() => null),
    [],
  )

  const [indexingJobs, setIndexingJobs] = useState<
    { repo: string; status: string; files_done: number; started_at: number }[]
  >([])

  // Poll indexing status every 3s
  useEffect(() => {
    const poll = () => {
      api.getIndexingStatus().then(setIndexingJobs).catch(() => {})
    }
    poll()
    const interval = setInterval(poll, 3000)
    return () => clearInterval(interval)
  }, [])

  const activeJobs = indexingJobs.filter((j) => j.status === "indexing")

  const rs = stats?.review_stats

  // Derive severity & category totals from timeseries (period-aware)
  const periodStats = timeseries?.reduce(
    (acc, pt) => {
      acc.blockers += pt.blockers
      acc.warnings += pt.warnings ?? 0
      acc.suggestions += pt.suggestions ?? 0
      acc.comments += pt.comments
      if (pt.categories) {
        for (const [cat, cnt] of Object.entries(pt.categories)) {
          acc.categories[cat] = (acc.categories[cat] ?? 0) + cnt
        }
      }
      return acc
    },
    { blockers: 0, warnings: 0, suggestions: 0, comments: 0, categories: {} as Record<string, number> },
  )

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
        <p className="text-sm text-muted-foreground">
          Review activity and code quality metrics
        </p>
      </div>

      {/* Indexing status */}
      {activeJobs.length > 0 && (
        <Card className="relative overflow-hidden border-primary/30 bg-primary/5">
          {/* Shimmer bar */}
          <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-primary to-transparent [background-size:200%_100%] [animation:shimmer_2s_linear_infinite]" />
          <CardContent className="flex items-center gap-4 p-4">
            <div className="relative flex h-8 w-8 items-center justify-center">
              <div className="absolute inset-1 rounded-full border-2 border-primary/40 border-t-primary [animation:spin_1s_linear_infinite]" />
            </div>
            <div className="flex-1">
              <p className="text-sm font-medium">
                Indexing {activeJobs.length}{" "}
                {activeJobs.length === 1 ? "repository" : "repositories"}
              </p>
              <p className="truncate text-xs text-muted-foreground">
                {activeJobs.map((j) => j.repo).join(", ")}
              </p>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Stat cards */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>PRs Reviewed</CardDescription>
            <CardTitle className="text-4xl tabular-nums">
              {rs?.total_reviews ?? 0}
            </CardTitle>
          </CardHeader>
          <CardFooter className="flex-col items-start gap-1 text-sm">
            {rs && rs.total_reviews > 0 ? (
              <>
                <div className="font-medium">
                  Avg {rs.avg_comments_per_pr.toFixed(1)} comments per PR
                </div>
                <div className="text-muted-foreground">
                  {rs.total_files_reviewed} files scanned
                </div>
              </>
            ) : (
              <div className="text-muted-foreground">No reviews yet</div>
            )}
          </CardFooter>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Issues Found</CardDescription>
            <CardTitle className="text-4xl tabular-nums">
              {rs?.total_comments ?? 0}
            </CardTitle>
          </CardHeader>
          <CardFooter className="flex-col items-start gap-1 text-sm">
            <div className="font-medium">
              {rs?.total_blockers ?? 0} blockers, {rs?.total_warnings ?? 0}{" "}
              warnings
            </div>
            <div className="text-muted-foreground">
              {rs?.total_suggestions ?? 0} suggestions
            </div>
          </CardFooter>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Avg Review Time</CardDescription>
            <CardTitle className="text-4xl tabular-nums">
              {rs && rs.avg_duration_ms > 0
                ? `${(rs.avg_duration_ms / 1000).toFixed(1)}s`
                : "—"}
            </CardTitle>
          </CardHeader>
          <CardFooter className="flex-col items-start gap-1 text-sm">
            <div className="font-medium">
              {rs ? fmt(rs.total_lines_changed) : "0"} lines reviewed
            </div>
            <div className="text-muted-foreground">
              {rs ? fmt(rs.total_tokens) : "0"} tokens used
            </div>
          </CardFooter>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Repositories</CardDescription>
            <CardTitle className="text-4xl tabular-nums">
              {stats?.total_repos ?? 0}
            </CardTitle>
          </CardHeader>
          <CardFooter className="flex-col items-start gap-1 text-sm">
            <div className="font-medium">
              {stats?.total_files ?? 0} files indexed
            </div>
            <div className="text-muted-foreground">
              {stats?.total_edges ?? 0} cross-repo edges
            </div>
          </CardFooter>
        </Card>
      </div>

      {/* Security alerts — populated by the OSV poller. */}
      {vulnSummary && (
        <SecurityAlertsCard summary={vulnSummary} />
      )}

      {/* Period selector */}
      <div className="flex gap-1">
        {(["day", "week", "month"] as const).map((p) => (
          <button
            key={p}
            onClick={() => setPeriod(p)}
            className={`inline-flex h-8 items-center rounded-md border px-3 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
              period === p
                ? "border-primary bg-primary/10 text-primary"
                : "border-input bg-background text-muted-foreground hover:bg-accent hover:text-accent-foreground"
            }`}
          >
            {p === "day" ? "Daily" : p === "week" ? "Weekly" : "Monthly"}
          </button>
        ))}
      </div>

      {/* Line charts */}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Lines Reviewed</CardTitle>
            <CardDescription>Lines of code scanned per period</CardDescription>
          </CardHeader>
          <CardContent>
            {timeseries && timeseries.length > 0 ? (
              <LinesChart key={period} data={timeseries} useBars={period !== "day"} />
            ) : (
              <Empty />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Reviews Over Time</CardTitle>
            <CardDescription>PRs reviewed and issues found</CardDescription>
          </CardHeader>
          <CardContent>
            {timeseries && timeseries.length > 0 ? (
              <ReviewsChart key={period} data={timeseries} useBars={period !== "day"} />
            ) : (
              <Empty />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Token Usage</CardTitle>
            <CardDescription>LLM tokens consumed over time</CardDescription>
          </CardHeader>
          <CardContent>
            {timeseries && timeseries.length > 0 ? (
              <TokensChart key={period} data={timeseries} useBars={period !== "day"} />
            ) : (
              <Empty />
            )}
          </CardContent>
        </Card>

        {/* Severity stacked bar */}
        <Card>
          <CardHeader>
            <CardTitle>Issue Severity</CardTitle>
            <CardDescription>Comments by severity level per period</CardDescription>
          </CardHeader>
          <CardContent>
            {timeseries && timeseries.length > 0 ? (
              <SeverityStackedBar key={period} data={timeseries} />
            ) : (
              <Empty />
            )}
          </CardContent>
        </Card>
      </div>

      {/* Category bar chart — full width */}
      {periodStats && Object.keys(periodStats.categories).length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Issue Categories</CardTitle>
            <CardDescription>
              Most common issue types across all reviews
            </CardDescription>
          </CardHeader>
          <CardContent>
            <CategoryBarChart key={period} categories={periodStats.categories} />
          </CardContent>
        </Card>
      )}
    </div>
  )
}

// ── Helpers ──

function fmt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

function Empty() {
  return (
    <div className="flex h-[250px] items-center justify-center text-sm text-muted-foreground">
      No data yet
    </div>
  )
}

function formatDate(d: string) {
  if (d.includes("W")) {
    // "2026-W15" → "Week 15"
    const week = d.split("W")[1]
    return `Week ${parseInt(week)}`
  }
  if (d.length === 7) {
    // "2026-04" → "Apr 2026"
    const [y, m] = d.split("-")
    const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    return `${months[parseInt(m) - 1]} ${y}`
  }
  // day format — show M/D
  const parts = d.split("-")
  return `${parseInt(parts[1])}/${parseInt(parts[2])}`
}

// ── Reviews line chart ──

const reviewsConfig = {
  reviews: { label: "Reviews", color: "var(--chart-1)" },
  comments: { label: "Issues", color: "var(--chart-2)" },
} satisfies ChartConfig

type TSPoint = {
  date: string
  reviews: number
  comments: number
  blockers: number
  warnings: number
  suggestions: number
  lines_changed: number
  tokens_used: number
  categories: Record<string, number>
}

function ReviewsChart({ data, useBars }: { data: TSPoint[]; useBars: boolean }) {
  const xAxis = (
    <XAxis dataKey="date" tickLine={false} axisLine={false} tickMargin={8} tickFormatter={formatDate} />
  )
  if (useBars) {
    return (
      <ChartContainer config={reviewsConfig} className="h-[250px] w-full">
        <BarChart data={data}>
          <CartesianGrid vertical={false} />
          {xAxis}
          <ChartTooltip content={<ChartTooltipContent />} />
          <Bar dataKey="reviews" fill="var(--color-reviews)" radius={[4, 4, 0, 0]} />
          <Bar dataKey="comments" fill="var(--color-comments)" radius={[4, 4, 0, 0]} />
        </BarChart>
      </ChartContainer>
    )
  }
  return (
    <ChartContainer config={reviewsConfig} className="h-[250px] w-full">
      <LineChart data={data}>
        <CartesianGrid vertical={false} />
        {xAxis}
        <ChartTooltip content={<ChartTooltipContent />} />
        <Line type="monotone" dataKey="reviews" stroke="var(--color-reviews)" strokeWidth={2} dot={false} />
        <Line type="monotone" dataKey="comments" stroke="var(--color-comments)" strokeWidth={2} dot={false} />
      </LineChart>
    </ChartContainer>
  )
}

// ── Tokens chart ──

const tokensConfig = {
  tokens_used: { label: "Tokens", color: "var(--chart-3)" },
} satisfies ChartConfig

function TokensChart({ data, useBars }: { data: TSPoint[]; useBars: boolean }) {
  const xAxis = (
    <XAxis dataKey="date" tickLine={false} axisLine={false} tickMargin={8} tickFormatter={formatDate} />
  )
  if (useBars) {
    return (
      <ChartContainer config={tokensConfig} className="h-[250px] w-full">
        <BarChart data={data}>
          <CartesianGrid vertical={false} />
          {xAxis}
          <ChartTooltip content={<ChartTooltipContent />} />
          <Bar dataKey="tokens_used" fill="var(--color-tokens_used)" radius={[4, 4, 0, 0]} />
        </BarChart>
      </ChartContainer>
    )
  }
  return (
    <ChartContainer config={tokensConfig} className="h-[250px] w-full">
      <AreaChart data={data}>
        <CartesianGrid vertical={false} />
        {xAxis}
        <ChartTooltip content={<ChartTooltipContent />} />
        <Area type="monotone" dataKey="tokens_used" stroke="var(--color-tokens_used)" fill="var(--color-tokens_used)" fillOpacity={0.15} strokeWidth={2} />
      </AreaChart>
    </ChartContainer>
  )
}

// ── Lines chart ──

const linesConfig = {
  lines_changed: { label: "Lines", color: "var(--chart-4)" },
} satisfies ChartConfig

function LinesChart({ data, useBars }: { data: TSPoint[]; useBars: boolean }) {
  const xAxis = (
    <XAxis dataKey="date" tickLine={false} axisLine={false} tickMargin={8} tickFormatter={formatDate} />
  )
  if (useBars) {
    return (
      <ChartContainer config={linesConfig} className="h-[250px] w-full">
        <BarChart data={data}>
          <CartesianGrid vertical={false} />
          {xAxis}
          <ChartTooltip content={<ChartTooltipContent />} />
          <Bar dataKey="lines_changed" fill="var(--color-lines_changed)" radius={[4, 4, 0, 0]} />
        </BarChart>
      </ChartContainer>
    )
  }
  return (
    <ChartContainer config={linesConfig} className="h-[250px] w-full">
      <AreaChart data={data}>
        <CartesianGrid vertical={false} />
        {xAxis}
        <ChartTooltip content={<ChartTooltipContent />} />
        <Area type="monotone" dataKey="lines_changed" stroke="var(--color-lines_changed)" fill="var(--color-lines_changed)"
          fillOpacity={0.15}
          strokeWidth={2}
        />
      </AreaChart>
    </ChartContainer>
  )
}

// ── Severity stacked bar ──

const severityBarConfig = {
  blockers: { label: "Blockers", color: "oklch(0.95 0 0)" },
  warnings: { label: "Warnings", color: "oklch(0.65 0 0)" },
  suggestions: { label: "Suggestions", color: "oklch(0.40 0 0)" },
} satisfies ChartConfig

function SeverityStackedBar({ data }: { data: TSPoint[] }) {
  return (
    <ChartContainer config={severityBarConfig} className="h-[250px] w-full">
      <BarChart data={data}>
        <CartesianGrid vertical={false} />
        <XAxis
          dataKey="date"
          tickLine={false}
          axisLine={false}
          tickMargin={8}
          tickFormatter={formatDate}
        />
        <ChartTooltip content={<ChartTooltipContent />} />
        <Bar dataKey="blockers" stackId="severity" fill="var(--color-blockers)" radius={[0, 0, 0, 0]} />
        <Bar dataKey="warnings" stackId="severity" fill="var(--color-warnings)" radius={[0, 0, 0, 0]} />
        <Bar dataKey="suggestions" stackId="severity" fill="var(--color-suggestions)" radius={[4, 4, 0, 0]} />
      </BarChart>
    </ChartContainer>
  )
}

// ── Category bar chart ──

const CATEGORY_LABELS: Record<string, string> = {
  bug: "Bugs",
  security: "Security",
  performance: "Performance",
  "error-handling": "Error Handling",
  "race-condition": "Race Conditions",
  "resource-leak": "Resource Leaks",
  maintainability: "Maintainability",
  clarity: "Clarity",
  configuration: "Configuration",
  other: "Other",
}

const catConfig = {
  count: { label: "Issues", color: "var(--chart-2)" },
} satisfies ChartConfig

function CategoryBarChart({ categories }: { categories: Record<string, number> }) {
  const data = Object.entries(categories)
    .sort(([, a], [, b]) => b - a)
    .slice(0, 8)
    .map(([name, count]) => ({ name: CATEGORY_LABELS[name] || name, count }))

  return (
    <ChartContainer config={catConfig} className="h-[250px] w-full">
      <BarChart data={data} layout="vertical" margin={{ left: 20 }}>
        <YAxis dataKey="name" type="category" tickLine={false} axisLine={false} width={110} tick={{ fontSize: 12 }} />
        <XAxis type="number" hide />
        <ChartTooltip cursor={false} content={<ChartTooltipContent />} />
        <Bar dataKey="count" fill="var(--color-count)" radius={4} />
      </BarChart>
    </ChartContainer>
  )
}

function SecurityAlertsCard({
  summary,
}: {
  summary: import("@/lib/api").VulnerabilitySummary
}) {
  const buckets: { label: string; count: number; cls: string }[] = [
    { label: "Critical", count: summary.critical, cls: "text-red-300" },
    { label: "High", count: summary.high, cls: "text-orange-300" },
    { label: "Moderate", count: summary.moderate, cls: "text-yellow-300" },
    { label: "Low", count: summary.low, cls: "text-zinc-300" },
  ]

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <div>
            <CardDescription>Security alerts</CardDescription>
            <CardTitle className="text-4xl tabular-nums">
              {summary.total}
            </CardTitle>
          </div>
          <a
            href="/vulnerabilities"
            className="text-xs font-medium text-muted-foreground underline-offset-2 hover:underline"
          >
            View all →
          </a>
        </div>
      </CardHeader>
      <CardFooter>
        {summary.total === 0 ? (
          <div className="text-sm text-muted-foreground">
            No known vulnerabilities across the org.
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
            {buckets
              .filter((b) => b.count > 0)
              .map((b) => (
                <div key={b.label} className="flex items-center gap-1.5">
                  <span className={`tabular-nums font-semibold ${b.cls}`}>
                    {b.count}
                  </span>
                  <span className="text-muted-foreground">{b.label}</span>
                </div>
              ))}
          </div>
        )}
      </CardFooter>
    </Card>
  )
}
