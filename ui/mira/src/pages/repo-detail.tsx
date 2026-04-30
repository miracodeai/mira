import {
  ArrowLeft,
  BookOpen,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react"
import { useEffect, useState } from "react"
import { Link, useParams } from "react-router"

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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { DependenciesTable } from "@/components/dashboard/dependencies-table"
import { api, type ReviewContextModel } from "@/lib/api"
import { useAsync } from "@/lib/hooks"

function formatRelativeTime(iso: string | null): string {
  if (!iso) return "Never indexed"
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return "Never indexed"
  const diff = Date.now() - then
  if (diff < 0) return "Indexed just now"
  const mins = Math.floor(diff / 60_000)
  if (mins < 1) return "Indexed just now"
  if (mins < 60) return `Indexed ${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `Indexed ${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 7) return `Indexed ${days}d ago`
  return `Indexed on ${new Date(iso).toLocaleDateString()}`
}

export function RepoDetailPage() {
  const { owner, repo } = useParams<{ owner: string; repo: string }>()

  const { data, loading, error } = useAsync(
    () => api.getRepo(owner!, repo!),
    [owner, repo],
  )
  const { data: packages } = useAsync(
    () => api.getPackages(owner!, repo!),
    [owner, repo],
  )
  const { data: vulns } = useAsync(
    () =>
      api
        .getRepoVulnerabilities(owner!, repo!)
        .catch(() => [] as never),
    [owner, repo],
  )
  const [contextEntries, setContextEntries] = useState<ReviewContextModel[]>([])
  const [contextLoaded, setContextLoaded] = useState(false)
  const [editingCtx, setEditingCtx] = useState<{
    id?: number
    title: string
    content: string
  } | null>(null)

  if (!contextLoaded && owner && repo) {
    api
      .listContext(owner, repo)
      .then(setContextEntries)
      .finally(() => setContextLoaded(true))
  }

  const saveContext = async () => {
    if (!editingCtx || !owner || !repo) return
    if (editingCtx.id) {
      const updated = await api.updateContext(
        owner,
        repo,
        editingCtx.id,
        editingCtx.title,
        editingCtx.content,
      )
      setContextEntries((prev) =>
        prev.map((e) => (e.id === updated.id ? updated : e)),
      )
    } else {
      const created = await api.createContext(
        owner,
        repo,
        editingCtx.title,
        editingCtx.content,
      )
      setContextEntries((prev) => [...prev, created])
    }
    setEditingCtx(null)
  }

  const deleteCtx = async (id: number) => {
    if (!owner || !repo) return
    await api.deleteContext(owner, repo, id)
    setContextEntries((prev) => prev.filter((e) => e.id !== id))
  }

  const [indexing, setIndexing] = useState(false)
  const [indexStatus, setIndexStatus] = useState("")

  // Poll indexing status continuously so the UI reflects jobs started from
  // anywhere (this page, the setup modal, a webhook-driven backfill, etc.).
  useEffect(() => {
    if (!owner || !repo) return
    let cancelled = false
    const tick = async () => {
      try {
        const jobs = await api.getIndexingStatus()
        const job = jobs.find((j) => j.repo === `${owner}/${repo}`)
        if (cancelled) return
        if (job && job.status === "indexing") {
          setIndexing(true)
          setIndexStatus(
            job.files_done > 0
              ? `Indexing... ${job.files_done} files processed`
              : "Indexing...",
          )
        } else if (job && job.status === "completed") {
          setIndexStatus(`Done — ${job.files_done} files indexed`)
          if (indexing) {
            setIndexing(false)
            setTimeout(() => window.location.reload(), 1000)
          }
        } else if (job && job.status === "failed") {
          setIndexStatus(`Failed: ${job.error}`)
          setIndexing(false)
        } else if (job && job.status === "cancelled") {
          setIndexStatus(`Cancelled — ${job.files_done} files indexed before stopping`)
          setIndexing(false)
        }
      } catch {
        // ignore
      }
    }
    tick()
    const interval = setInterval(tick, 2000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [owner, repo, indexing])

  const triggerIndex = async (full: boolean) => {
    if (!owner || !repo) return
    setIndexing(true)
    setIndexStatus(full ? "Starting full re-index..." : "Starting index update...")
    try {
      await api.triggerIndex(owner, repo, full)
    } catch {
      setIndexStatus("Failed to start indexing")
      setIndexing(false)
    }
  }

  const [cancelling, setCancelling] = useState(false)

  const cancelIndex = async () => {
    if (!owner || !repo) return
    setCancelling(true)
    setIndexStatus("Cancelling...")
    try {
      await api.cancelIndex(owner, repo)
    } catch {
      // If cancel fails, reset state so user can try again
      setCancelling(false)
    }
  }

  useEffect(() => {
    if (!indexing) setCancelling(false)
  }, [indexing])

  if (loading) {
    return <div className="p-6 text-sm text-muted-foreground">Loading...</div>
  }
  if (error) {
    return <div className="p-6 text-sm text-destructive">{error}</div>
  }
  if (!data) return null

  return (
    <div className="space-y-6 p-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <Link
            to="/repos"
            className="mb-2 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft className="h-3 w-3" /> Back
          </Link>
          <h1 className="text-2xl font-semibold tracking-tight">
            {data.owner}/{data.repo}
          </h1>
          <p className="mt-1 text-xs text-muted-foreground">
            {formatRelativeTime(data.last_indexed)}
          </p>
        </div>
        <div className="flex gap-2">
          {indexing ? (
            <Button
              size="sm"
              variant="outline"
              onClick={cancelIndex}
              disabled={cancelling}
            >
              {cancelling ? (
                <Loader2 className="mr-1 h-3 w-3 animate-spin" />
              ) : (
                <X className="mr-1 h-3 w-3" />
              )}
              {cancelling ? "Cancelling..." : "Cancel Indexing"}
            </Button>
          ) : (
            <>
              <Button
                size="sm"
                variant="outline"
                onClick={() => triggerIndex(false)}
              >
                <RefreshCw className="mr-1 h-3 w-3" />
                Update Index
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => triggerIndex(true)}
              >
                <RefreshCw className="mr-1 h-3 w-3" />
                Full Re-index
              </Button>
            </>
          )}
        </div>
      </div>

      {/* Indexing status */}
      {indexStatus && (
        <Card className={indexing ? "border-primary/30 bg-primary/5" : ""}>
          <CardContent className="flex items-center gap-3 p-4">
            {indexing && (
              <Loader2 className="h-4 w-4 animate-spin text-primary" />
            )}
            <p className="text-sm font-medium">{indexStatus}</p>
          </CardContent>
        </Card>
      )}

      {/* Stats */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Files</CardDescription>
            <CardTitle className="text-4xl tabular-nums">
              {data.file_count}
            </CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Symbols</CardDescription>
            <CardTitle className="text-4xl tabular-nums">
              {data.symbols_count}
            </CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Imports</CardDescription>
            <CardTitle className="text-4xl tabular-nums">
              {data.imports_count}
            </CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>External Refs</CardDescription>
            <CardTitle className="text-4xl tabular-nums">
              {data.external_refs_count}
            </CardTitle>
          </CardHeader>
        </Card>
      </div>

      {/* Tabs */}
      <Tabs defaultValue="files">
        <TabsList>
          <TabsTrigger value="files">Files</TabsTrigger>
          <TabsTrigger value="context">Review Context</TabsTrigger>
          <TabsTrigger value="dependencies">Dependencies</TabsTrigger>
          <TabsTrigger value="blast">Blast Radius</TabsTrigger>
        </TabsList>

        {/* Files */}
        <TabsContent value="files">
          <Card>
            <CardHeader>
              <div className="flex items-center gap-2">
                <CardTitle>Files</CardTitle>
                <Badge variant="secondary" className="tabular-nums">
                  {data.files.length}
                </Badge>
              </div>
              <CardDescription>
                Indexed files with generated summaries and symbols
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="space-y-6">
                {data.files.map((f) => (
                  <div key={f.path} className="flex items-start">
                    <Avatar className="mt-0.5 h-9 w-9">
                      <AvatarFallback className="text-xs">
                        {(f.language || "?").slice(0, 2).toUpperCase()}
                      </AvatarFallback>
                    </Avatar>
                    <div className="ml-4 min-w-0 flex-1 space-y-1">
                      <p className="text-sm font-medium leading-none">
                        {f.path}
                      </p>
                      <p className="text-sm text-muted-foreground">
                        {f.summary}
                      </p>
                    </div>
                    <div className="ml-4 whitespace-nowrap text-sm text-muted-foreground tabular-nums">
                      {f.loc ?? 0} lines
                    </div>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* Review Context */}
        <TabsContent value="context">
          <Card>
            <CardHeader>
              <div className="flex items-start justify-between gap-4">
                <div>
                  <div className="flex items-center gap-2">
                    <CardTitle>Review Context</CardTitle>
                    <Badge variant="secondary" className="tabular-nums">
                      {contextEntries.length}
                    </Badge>
                  </div>
                  <CardDescription>
                    Docs, guidelines, and API contracts injected into PR
                    reviews for this repo
                  </CardDescription>
                </div>
                <Button
                  size="sm"
                  onClick={() => setEditingCtx({ title: "", content: "" })}
                >
                  <Plus className="mr-1 h-3 w-3" /> Add
                </Button>
              </div>
            </CardHeader>
            <CardContent>
              {editingCtx && (
                <div className="mb-6 space-y-3 rounded-lg border p-4">
                  <Input
                    placeholder="Title (e.g. Architecture Overview)"
                    value={editingCtx.title}
                    onChange={(e) =>
                      setEditingCtx({ ...editingCtx, title: e.target.value })
                    }
                  />
                  <Textarea
                    className="min-h-[150px] font-mono text-sm"
                    placeholder="Markdown content..."
                    value={editingCtx.content}
                    onChange={(e) =>
                      setEditingCtx({ ...editingCtx, content: e.target.value })
                    }
                  />
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      onClick={saveContext}
                      disabled={!editingCtx.title.trim()}
                    >
                      Save
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setEditingCtx(null)}
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              )}
              {contextEntries.length > 0 ? (
                <div className="space-y-6">
                  {contextEntries.map((entry) => (
                    <div key={entry.id} className="flex items-start">
                      <Avatar className="mt-0.5 h-9 w-9">
                        <AvatarFallback className="text-xs">
                          <BookOpen className="h-4 w-4" />
                        </AvatarFallback>
                      </Avatar>
                      <div className="ml-4 min-w-0 flex-1 space-y-1">
                        <p className="text-sm font-medium leading-none">
                          {entry.title}
                        </p>
                        <pre className="whitespace-pre-wrap text-sm text-muted-foreground">
                          {entry.content.slice(0, 200)}
                          {entry.content.length > 200 && "..."}
                        </pre>
                      </div>
                      <div className="ml-4 flex gap-1">
                        <Button
                          size="icon"
                          variant="ghost"
                          className="h-8 w-8"
                          onClick={() =>
                            setEditingCtx({
                              id: entry.id,
                              title: entry.title,
                              content: entry.content,
                            })
                          }
                        >
                          <Pencil className="h-3 w-3" />
                        </Button>
                        <Button
                          size="icon"
                          variant="ghost"
                          className="h-8 w-8"
                          onClick={() => deleteCtx(entry.id)}
                        >
                          <Trash2 className="h-3 w-3" />
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>
              ) : !editingCtx ? (
                <p className="text-sm text-muted-foreground">
                  No context yet. Add architecture docs or coding guidelines
                  to improve review quality on this repo.
                </p>
              ) : null}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Dependencies — packages declared in manifests (npm, pip, docker, etc.) */}
        <TabsContent value="dependencies">
          <Card>
            <CardHeader>
              <CardTitle>Dependencies</CardTitle>
              <CardDescription>
                Packages declared in package.json, requirements.txt,
                pyproject.toml, go.mod, and Dockerfile.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <DependenciesTable
                packages={packages ?? []}
                vulnerabilities={vulns ?? []}
              />
            </CardContent>
          </Card>
        </TabsContent>

        {/* Blast Radius — what depends on this repo */}
        <TabsContent value="blast">
          <Card>
            <CardHeader>
              <CardTitle>Blast Radius</CardTitle>
              <CardDescription>
                What depends on this repo. Drag nodes to rearrange; click
                cross-repo nodes to navigate.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              <BlastRadiusList owner={owner!} repo={repo!} />
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  )
}

function BlastRadiusList({ owner, repo }: { owner: string; repo: string }) {
  const { data, loading } = useAsync(
    () => api.getBlastRadius(owner, repo),
    [owner, repo],
  )

  if (loading) {
    return <p className="text-sm text-muted-foreground">Loading...</p>
  }

  const hasInternal = data && data.internal.length > 0
  const hasCrossRepo = data && data.cross_repo.length > 0

  if (!hasInternal && !hasCrossRepo) {
    return (
      <p className="text-sm text-muted-foreground">
        No dependencies detected yet. Mira detects dependencies via imports,
        Docker images, API endpoints, Terraform modules, and package references.
      </p>
    )
  }

  return (
    <div className="space-y-6">
      {/* Cross-repo detail list */}
      {hasCrossRepo && (
        <div className="space-y-3">
          <h3 className="text-sm font-medium">Cross-Repo References</h3>
          {data!.cross_repo.map((entry) => {
            const [rOwner, rRepo] = entry.repo.split("/")
            return (
              <div key={entry.repo} className="rounded-lg border p-3">
                <Link
                  to={`/repos/${rOwner}/${rRepo}`}
                  className="text-sm font-medium hover:underline"
                >
                  {entry.repo}
                </Link>
                <p className="text-xs text-muted-foreground">
                  {entry.files.length} reference{entry.files.length !== 1 ? "s" : ""}
                </p>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
