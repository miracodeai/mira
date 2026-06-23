import {
  Activity as ActivityIcon,
  ChevronDown,
  ChevronsUpDown,
  ChevronUp,
  ExternalLink,
  Search,
  X,
} from "lucide-react"
import { useEffect, useMemo, useState } from "react"
import { CartesianGrid, Line, LineChart, XAxis } from "recharts"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { GitHubIcon } from "@/components/ui/github-icon"
import {
  type ChartConfig,
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
} from "@/components/ui/chart"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { api, type ActivityEventModel } from "@/lib/api"
import { useAsync, useDocumentTitle } from "@/lib/hooks"
import { cn } from "@/lib/utils"

const ALL_REPOS = "__all__"

// Subtle inset ring shared by every pill on the page.
const PILL_RING = "ring-1 ring-inset ring-foreground/10"

// Per-severity color treatment (semantic, not the near-black primary).
const SEVERITY_PILL: Record<string, string> = {
  blocker:
    "border-transparent bg-destructive/10 text-destructive ring-destructive/25",
  warning:
    "border-transparent bg-amber-500/15 text-amber-700 ring-amber-500/30 dark:text-amber-400",
  suggestion:
    "border-transparent bg-sky-500/15 text-sky-700 ring-sky-500/30 dark:text-sky-400",
}

const chartConfig = {
  reviews: { label: "Reviews", color: "var(--chart-1)" },
  comments: { label: "Issues", color: "var(--chart-2)" },
} satisfies ChartConfig

type SortKey =
  | "repo"
  | "pr_number"
  | "created_at"
  | "comments_posted"
  | "severity"
type SortDir = "asc" | "desc"

// Rank a row by severity: blockers dominate, then warnings, then suggestions.
function severityWeight(e: ActivityEventModel) {
  return e.blockers * 1_000_000 + e.warnings * 1_000 + e.suggestions
}

function sortValue(e: ActivityEventModel, key: SortKey): string | number {
  switch (key) {
    case "repo":
      return `${e.owner}/${e.repo}`.toLowerCase()
    case "pr_number":
      return e.pr_number
    case "created_at":
      return e.created_at
    case "comments_posted":
      return e.comments_posted
    case "severity":
      return severityWeight(e)
  }
}

function formatChartDate(d: string) {
  if (d.includes("W")) return `Week ${parseInt(d.split("W")[1])}`
  if (d.length === 7) {
    const [y, m] = d.split("-")
    const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    return `${months[parseInt(m) - 1]} ${y}`
  }
  const parts = d.split("-")
  return `${parseInt(parts[1])}/${parseInt(parts[2])}`
}

function relativeTime(epochSeconds: number) {
  const seconds = Math.floor(Date.now() / 1000 - epochSeconds)
  if (seconds < 60) return "just now"
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days}d ago`
  return new Date(epochSeconds * 1000).toLocaleDateString()
}

function formatTimestamp(epochSeconds: number) {
  return new Date(epochSeconds * 1000).toLocaleString()
}

function splitCategories(categories: string): string[] {
  return categories
    .split(",")
    .map((c) => c.trim())
    .filter(Boolean)
}

function SeverityBadges({ event }: { event: ActivityEventModel }) {
  const parts: { label: string; kind: keyof typeof SEVERITY_PILL }[] = []
  if (event.blockers > 0) parts.push({ label: `${event.blockers} blocker${event.blockers > 1 ? "s" : ""}`, kind: "blocker" })
  if (event.warnings > 0) parts.push({ label: `${event.warnings} warning${event.warnings > 1 ? "s" : ""}`, kind: "warning" })
  if (event.suggestions > 0) parts.push({ label: `${event.suggestions} suggestion${event.suggestions > 1 ? "s" : ""}`, kind: "suggestion" })
  if (parts.length === 0) return <span className="text-muted-foreground">—</span>
  return (
    <div className="flex flex-wrap gap-1">
      {parts.map((p) => (
        <Badge key={p.label} className={cn(PILL_RING, SEVERITY_PILL[p.kind])}>
          {p.label}
        </Badge>
      ))}
    </div>
  )
}

export function ActivityPage() {
  useDocumentTitle("Activity")

  const [period, setPeriod] = useState<"day" | "week" | "month">("day")
  const [search, setSearch] = useState("")
  const [debouncedSearch, setDebouncedSearch] = useState("")
  const [repo, setRepo] = useState<string>(ALL_REPOS)
  // `selected` holds the row being shown; `panelOpen` drives the slide
  // animation. We keep `selected` set during the close transition so the
  // content doesn't vanish before the panel finishes sliding out.
  const [selected, setSelected] = useState<ActivityEventModel | null>(null)
  const [panelOpen, setPanelOpen] = useState(false)

  const openDetail = (e: ActivityEventModel) => {
    setSelected(e)
    setPanelOpen(true)
  }
  const closeDetail = () => setPanelOpen(false)

  // Debounce the search input so typing doesn't refetch per keystroke.
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search), 250)
    return () => clearTimeout(t)
  }, [search])

  // Close the detail panel on Escape, like a native dialog.
  useEffect(() => {
    if (!panelOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeDetail()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [panelOpen])

  const { data: timeseries, loading: chartLoading } = useAsync(
    () => api.getTimeseries(period),
    [period],
  )

  const { data: activity, loading } = useAsync(
    () =>
      api.listActivity({
        q: debouncedSearch || undefined,
        repo: repo === ALL_REPOS ? undefined : repo,
      }),
    [debouncedSearch, repo],
  )

  const events = useMemo(() => activity?.events ?? [], [activity?.events])
  // Keep the repo list stable across searches: prefer the unfiltered list.
  const repos = useMemo(() => activity?.repos ?? [], [activity?.repos])

  // Client-side sort over the (≤200-row) result set. Default: newest first.
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({
    key: "created_at",
    dir: "desc",
  })

  const sortedEvents = useMemo(() => {
    const dir = sort.dir === "asc" ? 1 : -1
    return [...events].sort((a, b) => {
      const av = sortValue(a, sort.key)
      const bv = sortValue(b, sort.key)
      if (av < bv) return -1 * dir
      if (av > bv) return 1 * dir
      return 0
    })
  }, [events, sort])

  const toggleSort = (key: SortKey) =>
    setSort((s) =>
      s.key === key
        ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
        : // Text sorts ascending first; numbers/dates descending first.
          { key, dir: key === "repo" ? "asc" : "desc" },
    )

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Activity</h1>
          <p className="text-sm text-muted-foreground">
            Every PR Mira has reviewed, across all repositories.
          </p>
        </div>
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
      </div>

      {/* Top graph: reviews + issues found over time */}
      <Card>
        <CardContent className="pt-6">
          {chartLoading ? (
            <Skeleton className="h-[160px] w-full" />
          ) : timeseries && timeseries.length > 0 ? (
            <ChartContainer config={chartConfig} className="h-[160px] w-full">
              <LineChart data={timeseries}>
                <CartesianGrid vertical={false} />
                <XAxis
                  dataKey="date"
                  tickLine={false}
                  axisLine={false}
                  tickMargin={8}
                  tickFormatter={formatChartDate}
                />
                <ChartTooltip content={<ChartTooltipContent />} />
                <Line type="monotone" dataKey="reviews" stroke="var(--color-reviews)" strokeWidth={2} dot={false} />
                <Line type="monotone" dataKey="comments" stroke="var(--color-comments)" strokeWidth={2} dot={false} />
              </LineChart>
            </ChartContainer>
          ) : (
            <div className="flex h-[160px] items-center justify-center text-sm text-muted-foreground">
              No review activity yet.
            </div>
          )}
        </CardContent>
      </Card>

      {/* Search + repo filter */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search by PR title, number, repo, or category…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-8"
          />
        </div>
        <Select value={repo} onValueChange={setRepo}>
          <SelectTrigger className="sm:w-64">
            <SelectValue placeholder="All repos" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL_REPOS}>All repos</SelectItem>
            {repos.map((r) => (
              <SelectItem key={r} value={r}>
                {r}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Table */}
      {loading ? (
        <div className="text-sm text-muted-foreground">Loading…</div>
      ) : events.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center gap-2 py-12 text-center">
            <ActivityIcon className="h-8 w-8 text-muted-foreground" />
            <p className="text-sm text-muted-foreground">
              {debouncedSearch || repo !== ALL_REPOS
                ? "No reviews match your filters."
                : "Mira hasn't reviewed any PRs yet."}
            </p>
          </CardContent>
        </Card>
      ) : (
        <Card className="overflow-hidden py-0">
          <Table>
            <TableHeader>
              <TableRow>
                <SortHead label="Repo" sortKey="repo" sort={sort} onSort={toggleSort} />
                <SortHead label="PR" sortKey="pr_number" sort={sort} onSort={toggleSort} />
                <SortHead label="When" sortKey="created_at" sort={sort} onSort={toggleSort} />
                <SortHead
                  label="Comments"
                  sortKey="comments_posted"
                  sort={sort}
                  onSort={toggleSort}
                  align="right"
                />
                <SortHead label="Severity" sortKey="severity" sort={sort} onSort={toggleSort} />
                <TableHead>Categories</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sortedEvents.map((e) => (
                <TableRow
                  key={`${e.owner}/${e.repo}#${e.id}`}
                  data-active={
                    panelOpen && selected?.id === e.id && selected?.repo === e.repo
                  }
                  className="cursor-pointer data-[active=true]:bg-muted/60"
                  onClick={() => openDetail(e)}
                >
                  <TableCell className="whitespace-nowrap font-mono text-xs text-muted-foreground">
                    {e.owner}/{e.repo}
                  </TableCell>
                  <TableCell className="max-w-xs">
                    <div className="flex items-center gap-2">
                      <GitHubIcon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                      <span className="font-medium">#{e.pr_number}</span>
                      <span className="truncate text-muted-foreground">
                        {e.pr_title}
                      </span>
                    </div>
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-muted-foreground">
                    {relativeTime(e.created_at)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {e.comments_posted}
                  </TableCell>
                  <TableCell>
                    <SeverityBadges event={e} />
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {splitCategories(e.categories).map((c) => (
                        <Badge key={c} variant="secondary" className={PILL_RING}>
                          {c}
                        </Badge>
                      ))}
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}

      {/* Detail panel — a custom right-anchored drawer. No dimming/blur
          overlay; it sits below the top nav (top-12) and covers down to the
          bottom edge, leaving the rest of the screen visible and usable. */}
      <div
        aria-hidden={!panelOpen}
        className={cn(
          "fixed right-0 top-12 bottom-0 z-30 flex w-full max-w-[640px] flex-col border-l bg-background shadow-2xl transition-transform duration-300 ease-in-out",
          panelOpen ? "translate-x-0" : "pointer-events-none translate-x-full",
        )}
      >
        {selected && (
          <>
            <div className="flex items-start justify-between gap-3 border-b p-6">
              <div className="min-w-0 space-y-1">
                <div className="flex items-center gap-2">
                  <GitHubIcon className="h-4 w-4 shrink-0 text-muted-foreground" />
                  <h2 className="min-w-0 flex-1 truncate text-sm font-medium">
                    #{selected.pr_number} {selected.pr_title}
                  </h2>
                  <a
                    href={selected.pr_url}
                    target="_blank"
                    rel="noreferrer"
                    aria-label="Open PR on GitHub"
                    title="Open PR on GitHub"
                    className="shrink-0 text-muted-foreground transition-colors hover:text-foreground"
                  >
                    <ExternalLink className="h-4 w-4" />
                  </a>
                </div>
                <p className="text-xs text-muted-foreground">
                  {selected.owner}/{selected.repo} ·{" "}
                  {formatTimestamp(selected.created_at)}
                </p>
              </div>
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={closeDetail}
                aria-label="Close"
              >
                <X />
              </Button>
            </div>

            <div className="flex-1 space-y-6 overflow-y-auto p-6">
              <div>
                <h3 className="mb-2 text-xs font-medium uppercase text-muted-foreground">
                  Findings
                </h3>
                <SeverityBadges event={selected} />
              </div>

              <dl className="grid grid-cols-2 gap-x-4 gap-y-3">
                <Stat label="Comments posted" value={selected.comments_posted} />
                <Stat label="Files reviewed" value={selected.files_reviewed} />
                <Stat label="Lines changed" value={selected.lines_changed.toLocaleString()} />
                <Stat label="Tokens used" value={selected.tokens_used.toLocaleString()} />
                <Stat label="Duration" value={`${(selected.duration_ms / 1000).toFixed(1)}s`} />
              </dl>

              {splitCategories(selected.categories).length > 0 && (
                <div>
                  <h3 className="mb-2 text-xs font-medium uppercase text-muted-foreground">
                    Categories
                  </h3>
                  <div className="flex flex-wrap gap-1">
                    {splitCategories(selected.categories).map((c) => (
                      <Badge key={c} variant="secondary" className={PILL_RING}>
                        {c}
                      </Badge>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function SortHead({
  label,
  sortKey,
  sort,
  onSort,
  align = "left",
}: {
  label: string
  sortKey: SortKey
  sort: { key: SortKey; dir: SortDir }
  onSort: (key: SortKey) => void
  align?: "left" | "right"
}) {
  const active = sort.key === sortKey
  const Icon = active ? (sort.dir === "asc" ? ChevronUp : ChevronDown) : ChevronsUpDown
  return (
    <TableHead className={align === "right" ? "text-right" : undefined}>
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        aria-label={`Sort by ${label}`}
        className={cn(
          "inline-flex items-center gap-1 transition-colors hover:text-foreground",
          align === "right" && "flex-row-reverse",
          active ? "text-foreground" : "text-muted-foreground",
        )}
      >
        {label}
        <Icon
          className={cn(
            "h-3.5 w-3.5",
            active ? "text-foreground" : "text-muted-foreground/50",
          )}
        />
      </button>
    </TableHead>
  )
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm font-medium tabular-nums">{value}</dd>
    </div>
  )
}
