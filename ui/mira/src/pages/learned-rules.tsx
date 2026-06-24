import {
  Brain,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronsUpDown,
  ChevronUp,
  Clock,
  Pencil,
  Plus,
  Power,
  Search,
  Trash2,
  X,
} from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { ConfirmButton } from "@/components/ui/confirm-button"
import { GitHubIcon } from "@/components/ui/github-icon"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { api, type OrgLearnedRuleModel } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { useAsync, useDocumentTitle } from "@/lib/hooks"
import { cn } from "@/lib/utils"

const ALL_REPOS = "__all__"
const PAGE_SIZE_OPTIONS = [10, 20, 50, 100]
// Blue highlight on a filter control when it's narrowing results (matches Activity).
const ACTIVE_FILTER = "border-blue-500 ring-1 ring-blue-500/30"

const SIGNAL_LABEL: Record<string, string> = {
  reject_pattern: "Rejected pattern",
  accept_pattern: "Accepted pattern",
  human_pattern: "Human reviewer style",
  manual: "Added by admin",
}

type RuleDraft = { rule_text: string; category: string; path_pattern: string }
type SortKey = "repo" | "learning" | "status"
type SortDir = "asc" | "desc"

function ruleKey(r: OrgLearnedRuleModel) {
  return `${r.owner}/${r.repo}#${r.id}`
}

function formatDate(epochSeconds: number) {
  if (!epochSeconds) return "—"
  return new Date(epochSeconds * 1000).toLocaleString()
}

export function LearnedRulesPage() {
  useDocumentTitle("Learnings")
  const { user } = useAuth()
  const isAdmin = !!user?.is_admin

  const [refreshKey, setRefreshKey] = useState(0)
  const refresh = () => setRefreshKey((k) => k + 1)
  const [tab, setTab] = useState<"approved" | "pending">("approved")
  const [query, setQuery] = useState("")
  const [repoFilter, setRepoFilter] = useState(ALL_REPOS)
  const [editing, setEditing] = useState<OrgLearnedRuleModel | null>(null)
  const [creating, setCreating] = useState(false)
  const [selected, setSelected] = useState<OrgLearnedRuleModel | null>(null)
  const [panelOpen, setPanelOpen] = useState(false)

  const openDetail = (r: OrgLearnedRuleModel) => {
    setSelected(r)
    setPanelOpen(true)
  }
  const closeDetail = () => setPanelOpen(false)

  useEffect(() => {
    if (!panelOpen) return
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && closeDetail()
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [panelOpen])

  const { data: rules, loading } = useAsync(
    () => api.listLearnedRules(isAdmin ? "" : "approved").catch(() => []),
    [refreshKey, isAdmin],
  )
  const { data: repos } = useAsync(
    () => (isAdmin ? api.listRepos().catch(() => []) : Promise.resolve([])),
    [isAdmin],
  )

  const approved = useMemo(
    () => (rules ?? []).filter((r) => r.status === "approved"),
    [rules],
  )
  const pending = useMemo(
    () => (rules ?? []).filter((r) => r.status === "pending"),
    [rules],
  )

  const repoOptions = useMemo(() => {
    const set = new Set<string>()
    for (const r of rules ?? []) set.add(`${r.owner}/${r.repo}`)
    return [...set].sort()
  }, [rules])

  const applyFilter = (list: OrgLearnedRuleModel[]) => {
    const q = query.trim().toLowerCase()
    return list.filter((r) => {
      const slug = `${r.owner}/${r.repo}`
      if (repoFilter !== ALL_REPOS && slug !== repoFilter) return false
      if (!q) return true
      return `${r.rule_text} ${r.category} ${r.path_pattern} ${slug}`
        .toLowerCase()
        .includes(q)
    })
  }

  const act = (fn: () => Promise<unknown>) => fn().then(refresh).catch(() => {})

  // Panel actions operate on the selected rule.
  const approveSel = () => {
    if (!selected) return
    act(() => api.approveLearnedRule(selected.owner, selected.repo, selected.id))
    closeDetail()
  }
  const rejectSel = () => {
    if (!selected) return
    act(() => api.rejectLearnedRule(selected.owner, selected.repo, selected.id))
    closeDetail()
  }
  const toggleSel = (active: boolean) => {
    if (!selected) return
    act(() => api.setLearnedRuleActive(selected.owner, selected.repo, selected.id, active))
    setSelected({ ...selected, active })
  }
  const deleteSel = () => {
    if (!selected) return
    act(() => api.deleteLearnedRule(selected.owner, selected.repo, selected.id))
    closeDetail()
  }

  const filters = (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
      <div className="relative flex-1">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          placeholder="Filter learnings…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className={cn("pl-8", query && ACTIVE_FILTER)}
        />
      </div>
      <Select value={repoFilter} onValueChange={setRepoFilter}>
        <SelectTrigger className={cn("sm:w-64", repoFilter !== ALL_REPOS && ACTIVE_FILTER)}>
          <SelectValue placeholder="All repos" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL_REPOS}>All repos</SelectItem>
          {repoOptions.map((r) => (
            <SelectItem key={r} value={r}>
              {r}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )

  const firstLoad = loading && !rules
  const selectedKey = selected ? ruleKey(selected) : null

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Learnings</h1>
          <p className="text-sm text-muted-foreground">
            What Mira has learned from your team's PR feedback. Approved learnings
            inject into every review automatically.
          </p>
        </div>
        {isAdmin && (
          <Button size="sm" onClick={() => setCreating(true)}>
            <Plus className="mr-1 h-4 w-4" /> Add learning
          </Button>
        )}
      </div>

      {firstLoad ? (
        <div className="text-sm text-muted-foreground">Loading…</div>
      ) : !isAdmin ? (
        <div className="space-y-3">
          {filters}
          <LearningsTable
            rows={applyFilter(approved)}
            onSelect={openDetail}
            selectedKey={panelOpen ? selectedKey : null}
            resetKey={`${query}|${repoFilter}`}
          />
        </div>
      ) : (
        <Tabs value={tab} onValueChange={(v) => setTab(v as "approved" | "pending")}>
          <TabsList>
            <TabsTrigger value="approved">
              Approved
              <Badge variant="secondary" className="ml-2 tabular-nums">
                {approved.length}
              </Badge>
            </TabsTrigger>
            <TabsTrigger value="pending">
              Pending
              <Badge
                variant={pending.length ? "default" : "secondary"}
                className="ml-2 tabular-nums"
              >
                {pending.length}
              </Badge>
            </TabsTrigger>
          </TabsList>

          <div className="mt-4">{filters}</div>

          <TabsContent value="approved" className="mt-3 space-y-2">
            {pending.length > 0 && (
              <button
                onClick={() => setTab("pending")}
                className="inline-flex items-center gap-1.5 rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-xs text-amber-700 transition-colors hover:bg-amber-500/15 dark:text-amber-400"
              >
                <Clock className="h-3 w-3 shrink-0" />
                <span>
                  <span className="font-medium">{pending.length}</span> awaiting
                  approval
                </span>
                <ChevronRight className="h-3.5 w-3.5 shrink-0" />
              </button>
            )}
            <LearningsTable
              rows={applyFilter(approved)}
              onSelect={openDetail}
              selectedKey={panelOpen ? selectedKey : null}
              resetKey={`approved|${query}|${repoFilter}`}
            />
          </TabsContent>

          <TabsContent value="pending" className="mt-3">
            <LearningsTable
              rows={applyFilter(pending)}
              onSelect={openDetail}
              selectedKey={panelOpen ? selectedKey : null}
              resetKey={`pending|${query}|${repoFilter}`}
            />
          </TabsContent>
        </Tabs>
      )}

      {/* Detail side panel */}
      <div
        aria-hidden={!panelOpen}
        className={cn(
          "fixed right-0 top-12 bottom-0 z-30 flex w-full max-w-[560px] flex-col border-l bg-background shadow-2xl transition-transform duration-300 ease-in-out",
          panelOpen ? "translate-x-0" : "pointer-events-none translate-x-full",
        )}
      >
        {selected && (
          <>
            <div className="flex items-start justify-between gap-3 border-b p-6">
              <div className="min-w-0 space-y-1.5">
                <div className="flex items-center gap-1.5 font-mono text-xs text-muted-foreground">
                  <GitHubIcon className="h-3.5 w-3.5 shrink-0" />
                  {selected.owner}/{selected.repo}
                </div>
                <StatusBadge rule={selected} />
              </div>
              <Button variant="ghost" size="icon-sm" onClick={closeDetail} aria-label="Close">
                <X />
              </Button>
            </div>

            <div className="flex-1 space-y-6 overflow-y-auto p-6">
              <div>
                <h3 className="mb-2 text-xs font-medium uppercase text-muted-foreground">
                  Learning
                </h3>
                <p className="whitespace-pre-wrap text-sm">{selected.rule_text}</p>
              </div>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-3">
                <Meta label="Category" value={selected.category || "—"} />
                <Meta label="Path pattern" value={selected.path_pattern || "Any"} />
                <Meta
                  label="Source"
                  value={SIGNAL_LABEL[selected.source_signal] ?? selected.source_signal}
                />
                <Meta label="Samples" value={String(selected.sample_count)} />
                <Meta label="Updated" value={formatDate(selected.updated_at)} />
              </dl>
            </div>

            {isAdmin && (
              <div className="space-y-2 border-t p-6">
                {selected.status === "pending" ? (
                  <div className="flex gap-2">
                    <Button className="flex-1" onClick={approveSel}>
                      <Check className="mr-1 h-4 w-4" /> Approve
                    </Button>
                    <Button variant="outline" className="flex-1" onClick={rejectSel}>
                      <X className="mr-1 h-4 w-4" /> Reject
                    </Button>
                  </div>
                ) : selected.status === "approved" ? (
                  selected.active ? (
                    <ConfirmButton
                      variant="outline"
                      className="w-full"
                      dialogTitle="Disable learning?"
                      dialogDescription="It will stop influencing reviews until you re-enable it."
                      confirmLabel="Disable"
                      onConfirm={() => toggleSel(false)}
                    >
                      <Power className="mr-1 h-4 w-4" /> Disable
                    </ConfirmButton>
                  ) : (
                    <Button variant="outline" className="w-full" onClick={() => toggleSel(true)}>
                      <Power className="mr-1 h-4 w-4" /> Enable
                    </Button>
                  )
                ) : null}
                <div className="flex gap-2">
                  <Button
                    variant="ghost"
                    className="flex-1"
                    onClick={() => setEditing(selected)}
                  >
                    <Pencil className="mr-1 h-4 w-4" /> Edit
                  </Button>
                  <ConfirmButton
                    variant="ghost"
                    className="flex-1"
                    destructive
                    dialogTitle="Delete learning?"
                    dialogDescription="This permanently removes the rule. This cannot be undone."
                    confirmLabel="Delete"
                    onConfirm={deleteSel}
                  >
                    <Trash2 className="mr-1 h-4 w-4" /> Delete
                  </ConfirmButton>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {(creating || editing) && (
        <RuleDialog
          mode={editing ? "edit" : "create"}
          rule={editing}
          repos={(repos ?? []).map((r) => `${r.owner}/${r.repo}`)}
          onClose={() => {
            setCreating(false)
            setEditing(null)
          }}
          onSaved={() => {
            setCreating(false)
            setEditing(null)
            closeDetail()
            refresh()
          }}
        />
      )}
    </div>
  )
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm font-medium">{value}</dd>
    </div>
  )
}

function sortValue(r: OrgLearnedRuleModel, key: SortKey): string {
  switch (key) {
    case "repo":
      return `${r.owner}/${r.repo}`.toLowerCase()
    case "learning":
      return r.rule_text.toLowerCase()
    case "status":
      return r.status === "approved" ? (r.active ? "enabled" : "disabled") : r.status
  }
}

function LearningsTable({
  rows,
  onSelect,
  selectedKey,
  resetKey,
}: {
  rows: OrgLearnedRuleModel[]
  onSelect: (r: OrgLearnedRuleModel) => void
  selectedKey: string | null
  resetKey: string
}) {
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({
    key: "repo",
    dir: "asc",
  })
  const [page, setPage] = useState(0)
  const [pageSize, setPageSize] = useState(20)

  useEffect(() => {
    setPage(0)
  }, [resetKey, sort.key, sort.dir, pageSize])

  const toggleSort = (key: SortKey) =>
    setSort((s) =>
      s.key === key
        ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
        : { key, dir: "asc" },
    )

  const sorted = useMemo(() => {
    const dir = sort.dir === "asc" ? 1 : -1
    return [...rows].sort((a, b) => {
      const av = sortValue(a, sort.key)
      const bv = sortValue(b, sort.key)
      if (av < bv) return -1 * dir
      if (av > bv) return 1 * dir
      return a.id - b.id // stable tiebreaker
    })
  }, [rows, sort])

  const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize))
  const safePage = Math.min(page, totalPages - 1)
  const paged = sorted.slice(safePage * pageSize, safePage * pageSize + pageSize)
  const rangeStart = sorted.length === 0 ? 0 : safePage * pageSize + 1
  const rangeEnd = Math.min(sorted.length, safePage * pageSize + pageSize)

  if (rows.length === 0) {
    return (
      <Card>
        <CardContent className="flex flex-col items-center gap-2 py-12 text-center">
          <Brain className="h-8 w-8 text-muted-foreground" />
          <p className="text-sm text-muted-foreground">No learnings here.</p>
        </CardContent>
      </Card>
    )
  }

  return (
    <Card className="overflow-hidden py-0">
      <Table>
        <TableHeader>
          <TableRow>
            <SortHead label="Repo" sortKey="repo" sort={sort} onSort={toggleSort} className="w-56" />
            <SortHead label="Learning" sortKey="learning" sort={sort} onSort={toggleSort} />
            <SortHead label="Status" sortKey="status" sort={sort} onSort={toggleSort} className="w-28" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {paged.map((r) => {
            const disabled = r.status === "approved" && !r.active
            return (
              <TableRow
                key={ruleKey(r)}
                data-active={selectedKey === ruleKey(r)}
                className="cursor-pointer data-[active=true]:bg-muted/60"
                onClick={() => onSelect(r)}
              >
                <TableCell className="whitespace-nowrap align-top font-mono text-xs text-muted-foreground">
                  <span className="flex items-center gap-1.5">
                    <GitHubIcon className="h-3.5 w-3.5 shrink-0" />
                    {r.owner}/{r.repo}
                  </span>
                </TableCell>
                <TableCell className="align-top">
                  <div className={cn("line-clamp-2 text-sm", disabled && "opacity-50")}>
                    {r.rule_text}
                  </div>
                  {(r.category || r.path_pattern) && (
                    <div className="mt-0.5 truncate text-xs text-muted-foreground">
                      {r.category}
                      {r.path_pattern ? ` · ${r.path_pattern}` : ""}
                    </div>
                  )}
                </TableCell>
                <TableCell className="align-top">
                  <StatusBadge rule={r} />
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>

      <div className="flex items-center justify-between gap-2 border-t px-4 py-2 text-xs text-muted-foreground">
        <div className="flex items-center gap-3">
          <span>
            {rangeStart}–{rangeEnd} of {sorted.length}
          </span>
          <div className="flex items-center gap-1.5">
            <span>Rows:</span>
            <Select value={String(pageSize)} onValueChange={(v) => setPageSize(Number(v))}>
              <SelectTrigger className="h-7 w-[4.25rem] text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PAGE_SIZE_OPTIONS.map((n) => (
                  <SelectItem key={n} value={String(n)}>
                    {n}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="tabular-nums">
            Page {safePage + 1} / {totalPages}
          </span>
          <Button
            variant="outline"
            size="icon-sm"
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={safePage === 0}
            aria-label="Previous page"
          >
            <ChevronLeft />
          </Button>
          <Button
            variant="outline"
            size="icon-sm"
            onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
            disabled={safePage >= totalPages - 1}
            aria-label="Next page"
          >
            <ChevronRight />
          </Button>
        </div>
      </div>
    </Card>
  )
}

function StatusBadge({ rule }: { rule: OrgLearnedRuleModel }) {
  if (rule.status === "pending") {
    return (
      <Badge
        variant="outline"
        className="border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400"
      >
        Pending
      </Badge>
    )
  }
  if (rule.status === "rejected") {
    return (
      <Badge variant="outline" className="text-muted-foreground">
        Rejected
      </Badge>
    )
  }
  return rule.active ? (
    <Badge
      variant="outline"
      className="border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
    >
      Enabled
    </Badge>
  ) : (
    <Badge variant="outline" className="text-muted-foreground">
      Disabled
    </Badge>
  )
}

function SortHead({
  label,
  sortKey,
  sort,
  onSort,
  className,
}: {
  label: string
  sortKey: SortKey
  sort: { key: SortKey; dir: SortDir }
  onSort: (key: SortKey) => void
  className?: string
}) {
  const active = sort.key === sortKey
  const Icon = active ? (sort.dir === "asc" ? ChevronUp : ChevronDown) : ChevronsUpDown
  return (
    <TableHead className={className}>
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        aria-label={`Sort by ${label}`}
        className={cn(
          "inline-flex items-center gap-1 transition-colors hover:text-foreground",
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

function RuleDialog({
  mode,
  rule,
  repos,
  onClose,
  onSaved,
}: {
  mode: "create" | "edit"
  rule: OrgLearnedRuleModel | null
  repos: string[]
  onClose: () => void
  onSaved: () => void
}) {
  const [repoKey, setRepoKey] = useState(
    rule ? `${rule.owner}/${rule.repo}` : (repos[0] ?? ""),
  )
  const [draft, setDraft] = useState<RuleDraft>({
    rule_text: rule?.rule_text ?? "",
    category: rule?.category ?? "other",
    path_pattern: rule?.path_pattern ?? "",
  })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async () => {
    if (!repoKey || !draft.rule_text.trim()) {
      setError("Pick a repo and enter the rule text.")
      return
    }
    const [owner, repo] = repoKey.split("/")
    setSaving(true)
    setError(null)
    try {
      if (mode === "edit" && rule) {
        await api.updateLearnedRule(owner, repo, rule.id, draft)
      } else {
        await api.createLearnedRule(owner, repo, draft)
      }
      onSaved()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {mode === "edit" ? "Edit learning" : "Add learning"}
          </DialogTitle>
          <DialogDescription>
            {mode === "edit"
              ? "Update this learned rule. Admin-edited rules stay approved."
              : "Author a rule directly. It's approved immediately and feeds future reviews."}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1">
            <span className="text-xs font-medium text-muted-foreground">Repo</span>
            {mode === "edit" ? (
              <div className="font-mono text-sm">{repoKey}</div>
            ) : (
              <Select value={repoKey} onValueChange={setRepoKey}>
                <SelectTrigger>
                  <SelectValue placeholder="Select a repo" />
                </SelectTrigger>
                <SelectContent>
                  {repos.map((r) => (
                    <SelectItem key={r} value={r}>
                      {r}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>

          <div className="space-y-1">
            <span className="text-xs font-medium text-muted-foreground">Rule</span>
            <Textarea
              rows={4}
              placeholder="e.g. Don't flag missing docstrings on internal helpers."
              value={draft.rule_text}
              onChange={(e) => setDraft({ ...draft, rule_text: e.target.value })}
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <span className="text-xs font-medium text-muted-foreground">
                Category
              </span>
              <Input
                value={draft.category}
                onChange={(e) => setDraft({ ...draft, category: e.target.value })}
              />
            </div>
            <div className="space-y-1">
              <span className="text-xs font-medium text-muted-foreground">
                Path pattern (optional)
              </span>
              <Input
                placeholder="e.g. tests/"
                value={draft.path_pattern}
                onChange={(e) =>
                  setDraft({ ...draft, path_pattern: e.target.value })
                }
              />
            </div>
          </div>

          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={save} disabled={saving}>
            {saving ? "Saving…" : mode === "edit" ? "Save" : "Add"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
