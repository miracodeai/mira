import { BookOpen, Globe, Pencil, Plus, Power, Trash2 } from "lucide-react"
import { useEffect, useState } from "react"

import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { api, type RepoListItem, type RuleModel } from "@/lib/api"
import { useAsync } from "@/lib/hooks"

// ── Types ──

interface EditingRule {
  id?: number
  title: string
  content: string
}

// ── Page ──

export function RulesPage() {
  // Global rules
  const [globalRules, setGlobalRules] = useState<RuleModel[]>([])
  const [editingGlobal, setEditingGlobal] = useState<EditingRule | null>(null)

  useEffect(() => {
    api.listGlobalRules().then(setGlobalRules).catch(() => {})
  }, [])

  const saveGlobal = async () => {
    if (!editingGlobal || !editingGlobal.title.trim()) return
    try {
      if (editingGlobal.id) {
        const updated = await api.updateGlobalRule(
          editingGlobal.id,
          editingGlobal.title,
          editingGlobal.content,
        )
        setGlobalRules((prev) =>
          prev.map((r) => (r.id === updated.id ? updated : r)),
        )
      } else {
        const created = await api.createGlobalRule(
          editingGlobal.title,
          editingGlobal.content,
        )
        setGlobalRules((prev) => [created, ...prev])
      }
      setEditingGlobal(null)
    } catch {
      // TODO: toast
    }
  }

  const deleteGlobal = async (id: number) => {
    await api.deleteGlobalRule(id)
    setGlobalRules((prev) => prev.filter((r) => r.id !== id))
  }

  const toggleGlobal = async (id: number) => {
    const updated = await api.toggleGlobalRule(id)
    setGlobalRules((prev) =>
      prev.map((r) => (r.id === updated.id ? updated : r)),
    )
  }

  // Per-repo rules
  const { data: repos } = useAsync(api.listRepos, [])
  const [selectedRepo, setSelectedRepo] = useState<string>("")
  const [repoRules, setRepoRules] = useState<RuleModel[]>([])
  const [editingRepo, setEditingRepo] = useState<EditingRule | null>(null)

  useEffect(() => {
    if (!selectedRepo) {
      setRepoRules([])
      return
    }
    const [owner, repo] = selectedRepo.split("/")
    api.listRepoRules(owner, repo).then(setRepoRules).catch(() => {})
  }, [selectedRepo])

  const saveRepo = async () => {
    if (!editingRepo || !editingRepo.title.trim() || !selectedRepo) return
    const [owner, repo] = selectedRepo.split("/")
    try {
      if (editingRepo.id) {
        const updated = await api.updateRepoRule(
          owner,
          repo,
          editingRepo.id,
          editingRepo.title,
          editingRepo.content,
        )
        setRepoRules((prev) =>
          prev.map((r) => (r.id === updated.id ? updated : r)),
        )
      } else {
        const created = await api.createRepoRule(
          owner,
          repo,
          editingRepo.title,
          editingRepo.content,
        )
        setRepoRules((prev) => [created, ...prev])
      }
      setEditingRepo(null)
    } catch {
      // TODO: toast
    }
  }

  const deleteRepo = async (id: number) => {
    if (!selectedRepo) return
    const [owner, repo] = selectedRepo.split("/")
    await api.deleteRepoRule(owner, repo, id)
    setRepoRules((prev) => prev.filter((r) => r.id !== id))
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Custom Rules</h1>
        <p className="text-sm text-muted-foreground">
          Define review rules that Mira follows when reviewing code. Global
          rules apply to all repos. Per-repo rules apply to a single repo.
        </p>
      </div>

      {/* Global Rules */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Global Rules</CardTitle>
              <CardDescription>
                Apply to all repositories in your organization
              </CardDescription>
            </div>
            <Button
              size="sm"
              onClick={() => setEditingGlobal({ title: "", content: "" })}
            >
              <Plus className="mr-1 h-3 w-3" /> Add
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <>
            {editingGlobal && (
              <RuleForm
                editing={editingGlobal}
                setEditing={setEditingGlobal}
                onSave={saveGlobal}
              />
            )}
            {globalRules.length > 0 ? (
                <div className="space-y-4">
                  {globalRules.map((rule) => (
                    <RuleItem
                      key={rule.id}
                      rule={rule}
                      icon={<Globe className="h-4 w-4" />}
                      onEdit={() =>
                        setEditingGlobal({
                          id: rule.id,
                          title: rule.title,
                          content: rule.content,
                        })
                      }
                      onDelete={() => deleteGlobal(rule.id)}
                      onToggle={() => toggleGlobal(rule.id)}
                    />
                  ))}
                </div>
            ) : !editingGlobal ? (
              <p className="text-sm text-muted-foreground">
                No global rules yet. Add one to enforce standards across all repos.
              </p>
            ) : null}
          </>
        </CardContent>
      </Card>

      {/* Per-Repo Rules */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Per-Repo Rules</CardTitle>
              <CardDescription>
                Rules scoped to a specific repository
              </CardDescription>
            </div>
            <Button
              size="sm"
              disabled={!selectedRepo}
              onClick={() => setEditingRepo({ title: "", content: "" })}
            >
              <Plus className="mr-1 h-3 w-3" /> Add
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <div className="mb-4">
            <Select value={selectedRepo} onValueChange={setSelectedRepo}>
              <SelectTrigger className="w-72">
                <SelectValue placeholder="Select a repository..." />
              </SelectTrigger>
              <SelectContent>
                {(repos ?? []).map((r: RepoListItem) => (
                  <SelectItem key={`${r.owner}/${r.repo}`} value={`${r.owner}/${r.repo}`}>
                    {r.owner}/{r.repo}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {selectedRepo && editingRepo && (
            <RuleForm
              editing={editingRepo}
              setEditing={setEditingRepo}
              onSave={saveRepo}
            />
          )}
          {selectedRepo && repoRules.length > 0 ? (
            <div className="space-y-4">
              {repoRules.map((rule) => (
                <RuleItem
                  key={rule.id}
                  rule={rule}
                  icon={<BookOpen className="h-4 w-4" />}
                  onEdit={() =>
                    setEditingRepo({
                      id: rule.id,
                      title: rule.title,
                      content: rule.content,
                    })
                  }
                  onDelete={() => deleteRepo(rule.id)}
                />
              ))}
            </div>
          ) : selectedRepo && !editingRepo ? (
            <p className="text-sm text-muted-foreground">
              No rules for this repo yet.
            </p>
          ) : !selectedRepo ? (
            <p className="text-sm text-muted-foreground">
              Select a repository to manage its rules.
            </p>
          ) : null}
        </CardContent>
      </Card>
    </div>
  )
}

// ── Shared components ──

function RuleForm({
  editing,
  setEditing,
  onSave,
}: {
  editing: EditingRule
  setEditing: (e: EditingRule | null) => void
  onSave: () => void
}) {
  return (
    <div className="mb-6 space-y-3 rounded-lg border p-4">
      <Input
        placeholder="Rule title (e.g. No raw SQL queries)"
        value={editing.title}
        onChange={(e) => setEditing({ ...editing, title: e.target.value })}
      />
      <Textarea
        className="min-h-[120px] font-mono text-sm"
        placeholder="Rule description — tell Mira what to look for or avoid..."
        value={editing.content}
        onChange={(e) => setEditing({ ...editing, content: e.target.value })}
      />
      <div className="flex gap-2">
        <Button size="sm" onClick={onSave} disabled={!editing.title.trim()}>
          Save
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setEditing(null)}>
          Cancel
        </Button>
      </div>
    </div>
  )
}

function RuleItem({
  rule,
  icon,
  onEdit,
  onDelete,
  onToggle,
}: {
  rule: RuleModel
  icon: React.ReactNode
  onEdit: () => void
  onDelete: () => void
  onToggle?: () => void
}) {
  return (
    <div
      className={`flex items-start ${!rule.enabled ? "opacity-50" : ""}`}
    >
      <Avatar className="mt-0.5 h-9 w-9">
        <AvatarFallback className="text-xs">{icon}</AvatarFallback>
      </Avatar>
      <div className="ml-4 min-w-0 flex-1 space-y-1">
        <p className="text-sm font-medium leading-none">{rule.title}</p>
        <pre className="whitespace-pre-wrap text-sm text-muted-foreground">
          {rule.content.slice(0, 200)}
          {rule.content.length > 200 && "..."}
        </pre>
      </div>
      <div className="ml-4 flex gap-1">
        {onToggle && (
          <Button
            size="icon"
            variant="ghost"
            className="h-8 w-8"
            onClick={onToggle}
            title={rule.enabled ? "Disable" : "Enable"}
          >
            <Power className={`h-3 w-3 ${rule.enabled ? "text-green-500" : "text-muted-foreground"}`} />
          </Button>
        )}
        <Button
          size="icon"
          variant="ghost"
          className="h-8 w-8"
          onClick={onEdit}
        >
          <Pencil className="h-3 w-3" />
        </Button>
        <Button
          size="icon"
          variant="ghost"
          className="h-8 w-8"
          onClick={onDelete}
        >
          <Trash2 className="h-3 w-3" />
        </Button>
      </div>
    </div>
  )
}
