import { Brain, Check, Clock, Pencil, Plus, X } from "lucide-react"
import { useMemo, useState } from "react"
import { Link } from "react-router"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
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

const SIGNAL_LABEL: Record<string, string> = {
  reject_pattern: "Rejected pattern",
  accept_pattern: "Accepted pattern",
  human_pattern: "Human reviewer style",
  manual: "Added by admin",
}

const SIGNAL_STYLE: Record<string, string> = {
  reject_pattern: "text-red-300 border-red-500/40 bg-red-500/10",
  accept_pattern: "text-emerald-300 border-emerald-500/40 bg-emerald-500/10",
  human_pattern: "text-violet-300 border-violet-500/40 bg-violet-500/10",
  manual: "text-sky-300 border-sky-500/40 bg-sky-500/10",
}

type RuleDraft = { rule_text: string; category: string; path_pattern: string }

function groupByRepo(
  rules: OrgLearnedRuleModel[],
): [string, OrgLearnedRuleModel[]][] {
  const map = new Map<string, OrgLearnedRuleModel[]>()
  for (const r of rules) {
    const key = `${r.owner}/${r.repo}`
    const list = map.get(key)
    if (list) list.push(r)
    else map.set(key, [r])
  }
  return [...map.entries()].sort((a, b) => b[1].length - a[1].length)
}

export function LearnedRulesPage() {
  useDocumentTitle("Learnings")
  const { user } = useAuth()
  const isAdmin = !!user?.is_admin

  const [refreshKey, setRefreshKey] = useState(0)
  const refresh = () => setRefreshKey((k) => k + 1)
  const [tab, setTab] = useState<"approved" | "pending">("approved")
  const [editing, setEditing] = useState<OrgLearnedRuleModel | null>(null)
  const [creating, setCreating] = useState(false)

  // Admins fetch everything (to populate the queue); others see approved only.
  const { data: rules, loading } = useAsync(
    () => api.listLearnedRules(isAdmin ? "" : "approved").catch(() => []),
    [refreshKey, isAdmin],
  )
  const { data: repos } = useAsync(
    () => (isAdmin ? api.listRepos().catch(() => []) : Promise.resolve([])),
    [isAdmin],
  )
  const { data: version } = useAsync(() => api.getVersion().catch(() => null), [])
  const botName = version?.bot_name ?? "miracodeai"

  const approved = useMemo(
    () => (rules ?? []).filter((r) => r.status === "approved"),
    [rules],
  )
  const pending = useMemo(
    () => (rules ?? []).filter((r) => r.status === "pending"),
    [rules],
  )

  const act = (fn: () => Promise<unknown>) => fn().then(refresh).catch(() => {})

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
        <RuleGroups
          groups={groupByRepo(approved)}
          emptyBotName={botName}
        />
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

          <TabsContent value="approved" className="mt-4">
            <RuleGroups
              groups={groupByRepo(approved)}
              admin
              tab="approved"
              onEdit={setEditing}
              onAct={act}
              emptyBotName={botName}
            />
          </TabsContent>

          <TabsContent value="pending" className="mt-4">
            {pending.length === 0 ? (
              <Card>
                <CardContent className="space-y-2 py-12 text-center">
                  <Clock className="mx-auto h-8 w-8 text-muted-foreground" />
                  <p className="text-sm font-medium">Nothing awaiting approval</p>
                  <p className="mx-auto max-w-md text-sm text-muted-foreground">
                    New patterns Mira synthesizes from feedback land here for an
                    admin to approve before they affect reviews.
                  </p>
                </CardContent>
              </Card>
            ) : (
              <RuleGroups
                groups={groupByRepo(pending)}
                admin
                tab="pending"
                onEdit={setEditing}
                onAct={act}
                emptyBotName={botName}
              />
            )}
          </TabsContent>
        </Tabs>
      )}

      {/* Create / edit dialog */}
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

function RuleGroups({
  groups,
  admin = false,
  tab,
  onEdit,
  onAct,
  emptyBotName,
}: {
  groups: [string, OrgLearnedRuleModel[]][]
  admin?: boolean
  tab?: "approved" | "pending"
  onEdit?: (r: OrgLearnedRuleModel) => void
  onAct?: (fn: () => Promise<unknown>) => void
  emptyBotName: string
}) {
  if (groups.length === 0) {
    return (
      <Card>
        <CardContent className="space-y-3 py-12 text-center">
          <Brain className="mx-auto h-8 w-8 text-muted-foreground" />
          <p className="text-sm font-medium">No learnings yet</p>
          <p className="mx-auto max-w-md text-sm text-muted-foreground">
            Mira learns from{" "}
            <code className="font-mono">@{emptyBotName} reject</code> dismissals
            and from human review comments on merged PRs.
          </p>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="space-y-4">
      {groups.map(([repoKey, repoRules]) => {
        const [owner, repo] = repoKey.split("/")
        return (
          <Card key={repoKey}>
            <CardHeader>
              <div className="flex items-center gap-2">
                <CardTitle className="text-base">
                  <Link
                    to={`/repos/${owner}/${repo}`}
                    className="font-mono hover:underline"
                  >
                    {repoKey}
                  </Link>
                </CardTitle>
                <Badge variant="secondary" className="tabular-nums">
                  {repoRules.length}
                </Badge>
              </div>
            </CardHeader>
            <CardContent>
              <div className="space-y-3">
                {repoRules.map((rule) => (
                  <RuleRow
                    key={rule.id}
                    rule={rule}
                    admin={admin}
                    tab={tab}
                    onEdit={onEdit}
                    onAct={onAct}
                  />
                ))}
              </div>
            </CardContent>
          </Card>
        )
      })}
    </div>
  )
}

function RuleRow({
  rule,
  admin,
  tab,
  onEdit,
  onAct,
}: {
  rule: OrgLearnedRuleModel
  admin?: boolean
  tab?: "approved" | "pending"
  onEdit?: (r: OrgLearnedRuleModel) => void
  onAct?: (fn: () => Promise<unknown>) => void
}) {
  const { owner, repo, id } = rule
  return (
    <div className="space-y-1.5 rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge
          variant="outline"
          className={`text-[10px] ${SIGNAL_STYLE[rule.source_signal] ?? ""}`}
        >
          {SIGNAL_LABEL[rule.source_signal] ?? rule.source_signal}
        </Badge>
        {rule.category && (
          <span className="text-xs font-medium text-muted-foreground">
            {rule.category}
          </span>
        )}
        {rule.path_pattern && (
          <span className="font-mono text-xs text-muted-foreground">
            {rule.path_pattern}
          </span>
        )}
        {admin && tab === "approved" && !rule.active && (
          <Badge variant="outline" className="text-[10px] text-muted-foreground">
            Disabled
          </Badge>
        )}
        <span className="ml-auto text-xs text-muted-foreground">
          {rule.sample_count} sample{rule.sample_count !== 1 ? "s" : ""}
        </span>
      </div>

      <p
        className={cn(
          "text-sm text-foreground/90",
          admin && tab === "approved" && !rule.active && "opacity-60",
        )}
      >
        {rule.rule_text}
      </p>

      {admin && onAct && (
        <div className="flex flex-wrap items-center gap-2 pt-1">
          {tab === "pending" ? (
            <>
              <Button
                size="sm"
                onClick={() => onAct(() => api.approveLearnedRule(owner, repo, id))}
              >
                <Check className="mr-1 h-3.5 w-3.5" /> Approve
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => onAct(() => api.rejectLearnedRule(owner, repo, id))}
              >
                <X className="mr-1 h-3.5 w-3.5" /> Reject
              </Button>
            </>
          ) : (
            <Button
              size="sm"
              variant="outline"
              onClick={() =>
                onAct(() =>
                  api.setLearnedRuleActive(owner, repo, id, !rule.active),
                )
              }
            >
              {rule.active ? "Disable" : "Enable"}
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={() => onEdit?.(rule)}>
            <Pencil className="mr-1 h-3.5 w-3.5" /> Edit
          </Button>
          <ConfirmButton
            size="sm"
            variant="ghost"
            destructive
            dialogTitle="Delete learning?"
            dialogDescription="This permanently removes the rule. This cannot be undone."
            confirmLabel="Delete"
            onConfirm={() => onAct(() => api.deleteLearnedRule(owner, repo, id))}
          >
            Delete
          </ConfirmButton>
        </div>
      )}
    </div>
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
