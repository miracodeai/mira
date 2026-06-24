import { ChevronLeft, Loader2, Trash2 } from "lucide-react"
import { useEffect, useState } from "react"
import { useNavigate, useSearchParams } from "react-router"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { ConfirmButton } from "@/components/ui/confirm-button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { useDocumentTitle } from "@/lib/hooks"
import { api } from "@/lib/api"
import { useAuth } from "@/lib/auth"

function parseDetail(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e)
  try {
    const parsed = JSON.parse(raw.replace(/^API error \d+: /, ""))
    if (parsed?.detail)
      return typeof parsed.detail === "string"
        ? parsed.detail
        : JSON.stringify(parsed.detail)
  } catch {
    /* ignore */
  }
  return raw
}

export function LearningFormPage() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [params] = useSearchParams()

  const editOwner = params.get("owner") ?? ""
  const editRepo = params.get("repo") ?? ""
  const editId = params.get("id")
  const isEdit = Boolean(editId)
  useDocumentTitle(isEdit ? "Edit learning" : "Add learning")

  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [repos, setRepos] = useState<string[]>([])
  const [repoKey, setRepoKey] = useState("")
  const [ruleText, setRuleText] = useState("")
  const [category, setCategory] = useState("other")
  const [pathPattern, setPathPattern] = useState("")

  useEffect(() => {
    if (!user?.is_admin) return
    const reposP = api.listRepos().catch(() => [])
    const ruleP =
      isEdit && editOwner && editRepo && editId
        ? api.getLearnedRule(editOwner, editRepo, Number(editId))
        : Promise.resolve(null)
    Promise.all([reposP, ruleP])
      .then(([list, rule]) => {
        const slugs = list.map((r) => `${r.owner}/${r.repo}`)
        setRepos(slugs)
        if (rule) {
          setRepoKey(`${rule.owner}/${rule.repo}`)
          setRuleText(rule.rule_text)
          setCategory(rule.category || "other")
          setPathPattern(rule.path_pattern || "")
        } else {
          setRepoKey(slugs[0] ?? "")
        }
        setLoading(false)
      })
      .catch((e) => {
        setError(parseDetail(e))
        setLoading(false)
      })
  }, [user, isEdit, editOwner, editRepo, editId])

  if (!user?.is_admin) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        Admin access required.
      </div>
    )
  }

  const save = async () => {
    if (!repoKey || !ruleText.trim()) {
      setError("Pick a repo and enter the rule text.")
      return
    }
    const [owner, repo] = repoKey.split("/")
    const body = {
      rule_text: ruleText.trim(),
      category: category.trim() || "other",
      path_pattern: pathPattern.trim(),
    }
    setSaving(true)
    setError(null)
    try {
      if (isEdit && editId) {
        await api.updateLearnedRule(owner, repo, Number(editId), body)
      } else {
        await api.createLearnedRule(owner, repo, body)
      }
      navigate("/learnings")
    } catch (e) {
      setError(parseDetail(e))
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    if (!editId) return
    setError(null)
    try {
      await api.deleteLearnedRule(editOwner, editRepo, Number(editId))
      navigate("/learnings")
    } catch (e) {
      setError(parseDetail(e))
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6 p-6">
      <button
        onClick={() => navigate("/learnings")}
        className="flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ChevronLeft className="h-4 w-4" /> Learnings
      </button>

      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          {isEdit ? "Edit learning" : "Add learning"}
        </h1>
        <p className="text-sm text-muted-foreground">
          {isEdit
            ? "Update this learned rule. Admin-edited rules stay approved."
            : "Author a rule directly. It's approved immediately and feeds future reviews."}
        </p>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading…
        </div>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Details</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <label className="text-sm font-medium">Repo</label>
                {isEdit ? (
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

              <div className="space-y-2">
                <label className="text-sm font-medium" htmlFor="lr-text">
                  Rule
                </label>
                <Textarea
                  id="lr-text"
                  rows={4}
                  placeholder="e.g. Don't flag missing docstrings on internal helpers."
                  value={ruleText}
                  onChange={(e) => setRuleText(e.target.value)}
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <label className="text-sm font-medium" htmlFor="lr-category">
                    Category
                  </label>
                  <Input
                    id="lr-category"
                    value={category}
                    onChange={(e) => setCategory(e.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <label className="text-sm font-medium" htmlFor="lr-path">
                    Path pattern (optional)
                  </label>
                  <Input
                    id="lr-path"
                    placeholder="e.g. tests/"
                    value={pathPattern}
                    onChange={(e) => setPathPattern(e.target.value)}
                  />
                </div>
              </div>
            </CardContent>
          </Card>

          {error && <p className="text-sm break-words text-destructive">{error}</p>}

          <div className="flex items-center justify-between">
            <div className="flex gap-2">
              <Button onClick={save} disabled={saving || !ruleText.trim() || !repoKey}>
                {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                {isEdit ? "Save changes" : "Add learning"}
              </Button>
              <Button variant="ghost" onClick={() => navigate("/learnings")}>
                Cancel
              </Button>
            </div>
            {isEdit && (
              <ConfirmButton
                variant="ghost"
                className="text-destructive"
                destructive
                dialogTitle="Delete learning?"
                dialogDescription="This permanently removes the rule. This cannot be undone."
                confirmLabel="Delete"
                onConfirm={remove}
              >
                <Trash2 className="mr-2 h-4 w-4" /> Delete
              </ConfirmButton>
            )}
          </div>
        </>
      )}
    </div>
  )
}
