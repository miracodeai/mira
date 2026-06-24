import {
  Brain,
  Check,
  Clock,
  Pencil,
  Plus,
  Power,
  Search,
  Trash2,
  X,
} from "lucide-react"
import { useMemo, useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { ConfirmButton } from "@/components/ui/confirm-button"
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

type RuleDraft = { rule_text: string; category: string; path_pattern: string }

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

  const filters = (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
      <div className="relative flex-1">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          placeholder="Filter learnings…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="pl-8"
        />
      </div>
      <Select value={repoFilter} onValueChange={setRepoFilter}>
        <SelectTrigger className="sm:w-64">
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

      {loading ? (
        <div className="text-sm text-muted-foreground">Loading…</div>
      ) : !isAdmin ? (
        <div className="space-y-3">
          {filters}
          <LearningsTable rows={applyFilter(approved)} />
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

          <TabsContent value="approved" className="mt-4 space-y-3">
            {pending.length > 0 && (
              <button
                onClick={() => setTab("pending")}
                className="flex w-full items-center gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-left text-sm text-amber-700 transition-colors hover:bg-amber-500/15 dark:text-amber-400"
              >
                <Clock className="h-4 w-4 shrink-0" />
                <span>
                  <span className="font-medium">{pending.length}</span> learning
                  {pending.length !== 1 ? "s" : ""} awaiting approval
                </span>
                <span className="ml-auto font-medium">Review queue →</span>
              </button>
            )}
            {filters}
            <LearningsTable
              rows={applyFilter(approved)}
              admin
              tab="approved"
              onEdit={setEditing}
              onAct={act}
            />
          </TabsContent>

          <TabsContent value="pending" className="mt-4 space-y-3">
            {filters}
            <LearningsTable
              rows={applyFilter(pending)}
              admin
              tab="pending"
              onEdit={setEditing}
              onAct={act}
            />
          </TabsContent>
        </Tabs>
      )}

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
            refresh()
          }}
        />
      )}
    </div>
  )
}

function LearningsTable({
  rows,
  admin = false,
  tab,
  onEdit,
  onAct,
}: {
  rows: OrgLearnedRuleModel[]
  admin?: boolean
  tab?: "approved" | "pending"
  onEdit?: (r: OrgLearnedRuleModel) => void
  onAct?: (fn: () => Promise<unknown>) => void
}) {
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
            <TableHead className="w-48">Repo</TableHead>
            <TableHead>Learning</TableHead>
            {admin && <TableHead className="w-px text-right">Actions</TableHead>}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => (
            <TableRow key={`${r.owner}/${r.repo}#${r.id}`}>
              <TableCell className="whitespace-nowrap align-top font-mono text-xs text-muted-foreground">
                {r.owner}/{r.repo}
              </TableCell>
              <TableCell className="align-top">
                <div
                  className={cn(
                    "text-sm",
                    admin && tab === "approved" && !r.active && "opacity-50",
                  )}
                >
                  {r.rule_text}
                </div>
                <div className="mt-0.5 text-xs text-muted-foreground">
                  {r.category}
                  {r.path_pattern ? ` · ${r.path_pattern}` : ""}
                  {admin && tab === "approved" && !r.active ? " · disabled" : ""}
                </div>
              </TableCell>
              {admin && onAct && (
                <TableCell className="align-top text-right whitespace-nowrap">
                  {tab === "pending" ? (
                    <div className="flex justify-end gap-1">
                      <Button
                        size="sm"
                        onClick={() =>
                          onAct(() => api.approveLearnedRule(r.owner, r.repo, r.id))
                        }
                      >
                        <Check className="mr-1 h-3.5 w-3.5" /> Approve
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          onAct(() => api.rejectLearnedRule(r.owner, r.repo, r.id))
                        }
                      >
                        <X className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  ) : (
                    <div className="flex justify-end gap-0.5">
                      <Button
                        size="icon-sm"
                        variant="ghost"
                        title={r.active ? "Disable" : "Enable"}
                        onClick={() =>
                          onAct(() =>
                            api.setLearnedRuleActive(r.owner, r.repo, r.id, !r.active),
                          )
                        }
                      >
                        <Power className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        size="icon-sm"
                        variant="ghost"
                        title="Edit"
                        onClick={() => onEdit?.(r)}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </Button>
                      <ConfirmButton
                        size="icon-sm"
                        variant="ghost"
                        destructive
                        tooltip="Delete"
                        dialogTitle="Delete learning?"
                        dialogDescription="This permanently removes the rule. This cannot be undone."
                        confirmLabel="Delete"
                        onConfirm={() =>
                          onAct(() => api.deleteLearnedRule(r.owner, r.repo, r.id))
                        }
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </ConfirmButton>
                    </div>
                  )}
                </TableCell>
              )}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Card>
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
              rows={3}
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
