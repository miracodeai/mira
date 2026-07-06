import {
  Brain,
  Check,
  ChevronDown,
  Pencil,
  ShieldQuestion,
  Sparkles,
  Trash2,
  Undo2,
  X,
} from "lucide-react"
import { useEffect, useMemo, useState } from "react"
import { Link } from "react-router"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import { ConfirmButton } from "@/components/ui/confirm-button"
import { Textarea } from "@/components/ui/textarea"
import { api, type OrgLearnedRuleModel } from "@/lib/api"
import { useAsync, useDocumentTitle } from "@/lib/hooks"

const SIGNAL_LABEL: Record<string, string> = {
  reject_pattern: "Rejected pattern",
  accept_pattern: "Accepted pattern",
  human_pattern: "Human reviewer style",
}

const SIGNAL_STYLE: Record<string, string> = {
  reject_pattern: "text-red-300 border-red-500/40 bg-red-500/10",
  accept_pattern: "text-emerald-300 border-emerald-500/40 bg-emerald-500/10",
  human_pattern: "text-violet-300 border-violet-500/40 bg-violet-500/10",
}

const ruleKey = (r: OrgLearnedRuleModel) => `${r.owner}/${r.repo}#${r.id}`

export function LearnedRulesPage() {
  useDocumentTitle("Learnings")
  const [rules, setRules] = useState<OrgLearnedRuleModel[]>([])
  const [loading, setLoading] = useState(true)
  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [editingText, setEditingText] = useState("")
  const { data: version } = useAsync(
    () => api.getVersion().catch(() => null),
    [],
  )
  const botName = version?.bot_name ?? "miracodeai"

  useEffect(() => {
    api
      .listLearnedRules()
      .then(setRules)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const pending = useMemo(() => rules.filter((r) => r.status === "pending"), [rules])
  const approved = useMemo(() => rules.filter((r) => r.status === "approved"), [rules])
  const declined = useMemo(() => rules.filter((r) => r.status === "declined"), [rules])

  // Group by (owner/repo) so the page reads "what Mira learned about each repo"
  const grouped = useMemo(() => {
    const map = new Map<string, OrgLearnedRuleModel[]>()
    for (const r of approved) {
      const key = `${r.owner}/${r.repo}`
      const list = map.get(key)
      if (list) list.push(r)
      else map.set(key, [r])
    }
    return [...map.entries()].sort((a, b) => b[1].length - a[1].length)
  }, [approved])

  const totalRules = approved.length
  const reposWithRules = grouped.length

  const setStatus = async (
    rule: OrgLearnedRuleModel,
    status: "approved" | "declined",
  ) => {
    const updated = await api.setLearnedRuleStatus(rule.owner, rule.repo, rule.id, status)
    setRules((prev) =>
      prev.map((r) => (ruleKey(r) === ruleKey(rule) ? { ...r, ...updated } : r)),
    )
  }

  const startEdit = (rule: OrgLearnedRuleModel) => {
    setEditingKey(ruleKey(rule))
    setEditingText(rule.rule_text)
  }

  const saveEdit = async (rule: OrgLearnedRuleModel) => {
    if (!editingText.trim()) return
    const updated = await api.updateLearnedRule(
      rule.owner,
      rule.repo,
      rule.id,
      editingText.trim(),
    )
    setRules((prev) =>
      prev.map((r) => (ruleKey(r) === ruleKey(rule) ? { ...r, ...updated } : r)),
    )
    setEditingKey(null)
  }

  const remove = async (rule: OrgLearnedRuleModel) => {
    await api.deleteLearnedRule(rule.owner, rule.repo, rule.id)
    setRules((prev) => prev.filter((r) => ruleKey(r) !== ruleKey(rule)))
  }

  const ruleMeta = (rule: OrgLearnedRuleModel, showRepo = false) => (
    <div className="flex flex-wrap items-center gap-2">
      <Badge
        variant="outline"
        className={`text-[10px] ${SIGNAL_STYLE[rule.source_signal] ?? ""}`}
      >
        {SIGNAL_LABEL[rule.source_signal] ?? rule.source_signal}
      </Badge>
      {showRepo && (
        <Link
          to={`/repos/${rule.owner}/${rule.repo}`}
          className="font-mono text-xs text-muted-foreground hover:underline"
        >
          {rule.owner}/{rule.repo}
        </Link>
      )}
      {rule.category && (
        <span className="text-xs font-medium text-muted-foreground">
          {rule.category}
        </span>
      )}
      {rule.path_pattern && !rule.path_pattern.startsWith("__llm_pattern") && (
        <span className="font-mono text-xs text-muted-foreground">
          {rule.path_pattern}
        </span>
      )}
      <span className="ml-auto text-xs text-muted-foreground">
        {rule.sample_count} sample{rule.sample_count !== 1 ? "s" : ""}
      </span>
    </div>
  )

  const ruleEditor = (rule: OrgLearnedRuleModel) => (
    <div className="space-y-2">
      <Textarea
        className="min-h-[80px] text-sm"
        value={editingText}
        onChange={(e) => setEditingText(e.target.value)}
      />
      <div className="flex gap-2">
        <Button size="sm" onClick={() => saveEdit(rule)} disabled={!editingText.trim()}>
          Save
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setEditingKey(null)}>
          Cancel
        </Button>
      </div>
    </div>
  )

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Learnings</h1>
        <p className="text-sm text-muted-foreground">
          What Mira has learned from your team's PR feedback. Approved learnings
          inject into every review automatically.
        </p>
      </div>

      {pending.length > 0 && (
        <Card className="border-amber-500/40">
          <CardHeader>
            <div className="flex items-center gap-2">
              <ShieldQuestion className="h-4 w-4 text-amber-400" />
              <CardTitle className="text-base">Pending approval</CardTitle>
              <Badge
                variant="outline"
                className="border-amber-500/40 bg-amber-500/10 tabular-nums text-amber-300"
              >
                {pending.length}
              </Badge>
            </div>
            <CardDescription>
              New learnings land here and only apply to reviews once approved.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              {pending.map((rule) => (
                <div key={ruleKey(rule)} className="space-y-1.5 rounded-lg border p-3">
                  {ruleMeta(rule, true)}
                  {editingKey === ruleKey(rule) ? (
                    ruleEditor(rule)
                  ) : (
                    <p className="text-sm text-foreground/90">{rule.rule_text}</p>
                  )}
                  <div className="flex gap-2 pt-1">
                    <Button size="sm" onClick={() => setStatus(rule, "approved")}>
                      <Check className="mr-1 h-3 w-3" /> Approve
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setStatus(rule, "declined")}
                    >
                      <X className="mr-1 h-3 w-3" /> Decline
                    </Button>
                    <Button
                      size="icon"
                      variant="ghost"
                      className="h-8 w-8"
                      onClick={() => startEdit(rule)}
                    >
                      <Pencil className="h-3 w-3" />
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {!loading && rules.length === 0 ? (
        <Card>
          <CardContent className="space-y-3 py-12 text-center">
            <Brain className="mx-auto h-8 w-8 text-muted-foreground" />
            <p className="text-sm font-medium">No learnings yet</p>
            <p className="mx-auto max-w-md text-sm text-muted-foreground">
              Mira learns from <code className="font-mono">@{botName} reject</code>{" "}
              dismissals and from human review comments on merged PRs. Reach
              ~3 reject signals or merge a PR with substantive review comments to
              see synthesized patterns appear here.
            </p>
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
            <div className="flex items-center gap-1.5">
              <Sparkles className="h-3.5 w-3.5 text-muted-foreground" />
              <span className="font-semibold tabular-nums">{totalRules}</span>
              <span className="text-muted-foreground">
                approved rule{totalRules !== 1 ? "s" : ""} across {reposWithRules}{" "}
                repo{reposWithRules !== 1 ? "s" : ""}
              </span>
            </div>
          </div>

          <div className="space-y-4">
            {grouped.map(([repoKey, repoRules]) => {
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
                    <CardDescription>
                      Synthesized from {repoRules.length}{" "}
                      feedback signal{repoRules.length !== 1 ? "s" : ""} on this
                      repo
                    </CardDescription>
                  </CardHeader>
                  <CardContent>
                    <div className="space-y-3">
                      {repoRules.map((rule) => (
                        <div
                          key={ruleKey(rule)}
                          className="space-y-1.5 rounded-lg border p-3"
                        >
                          {ruleMeta(rule)}
                          {editingKey === ruleKey(rule) ? (
                            ruleEditor(rule)
                          ) : (
                            <div className="flex items-start gap-2">
                              <p className="flex-1 text-sm text-foreground/90">
                                {rule.rule_text}
                              </p>
                              <div className="flex gap-1">
                                <Button
                                  size="icon"
                                  variant="ghost"
                                  className="h-8 w-8"
                                  onClick={() => startEdit(rule)}
                                >
                                  <Pencil className="h-3 w-3" />
                                </Button>
                                <ConfirmButton
                                  size="icon"
                                  variant="ghost"
                                  className="h-8 w-8"
                                  destructive
                                  dialogTitle="Delete learning?"
                                  dialogDescription="This forgets the learning entirely. It may be re-synthesized as pending from future feedback. To suppress it permanently, decline it instead."
                                  confirmLabel="Delete"
                                  onConfirm={() => remove(rule)}
                                >
                                  <Trash2 className="h-3 w-3" />
                                </ConfirmButton>
                              </div>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </CardContent>
                </Card>
              )
            })}
          </div>
        </>
      )}

      {declined.length > 0 && (
        <Collapsible>
          <CollapsibleTrigger className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
            <ChevronDown className="h-3.5 w-3.5" />
            Declined ({declined.length})
          </CollapsibleTrigger>
          <CollapsibleContent>
            <div className="mt-3 space-y-3">
              {declined.map((rule) => (
                <div
                  key={ruleKey(rule)}
                  className="space-y-1.5 rounded-lg border p-3 opacity-50"
                >
                  {ruleMeta(rule, true)}
                  <div className="flex items-start gap-2">
                    <p className="flex-1 text-sm text-foreground/90">
                      {rule.rule_text}
                    </p>
                    <div className="flex gap-1">
                      <Button
                        size="icon"
                        variant="ghost"
                        className="h-8 w-8"
                        title="Restore"
                        onClick={() => setStatus(rule, "approved")}
                      >
                        <Undo2 className="h-3 w-3" />
                      </Button>
                      <ConfirmButton
                        size="icon"
                        variant="ghost"
                        className="h-8 w-8"
                        destructive
                        dialogTitle="Delete learning?"
                        dialogDescription="This forgets the learning entirely. It may be re-synthesized as pending from future feedback."
                        confirmLabel="Delete"
                        onConfirm={() => remove(rule)}
                      >
                        <Trash2 className="h-3 w-3" />
                      </ConfirmButton>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </CollapsibleContent>
        </Collapsible>
      )}
    </div>
  )
}
