import { ArrowRight, Check, Plus, X } from "lucide-react"
import { useState } from "react"
import { Link } from "react-router"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { RelationshipGraph } from "@/components/dashboard/relationship-graph"
import { api } from "@/lib/api"
import { useAsync } from "@/lib/hooks"

export function RelationshipsPage() {
  const { data, loading, error } = useAsync(api.getRelationships, [])
  const { data: repos } = useAsync(api.listRepos, [])
  const [refreshKey, setRefreshKey] = useState(0)
  const [showAddEdge, setShowAddEdge] = useState(false)
  const [newEdge, setNewEdge] = useState({ source: "", target: "", reason: "" })

  const { data: freshData } = useAsync(
    () => api.getRelationships(),
    [refreshKey],
  )
  const displayData = freshData ?? data

  const repoFileCounts: Record<string, number> = {}
  repos?.forEach((r) => {
    repoFileCounts[`${r.owner}/${r.repo}`] = r.file_count
  })

  const confirmEdge = async (source: string, target: string) => {
    await api.setOverride(source, target, "confirmed")
    setRefreshKey((k) => k + 1)
  }

  const denyEdge = async (source: string, target: string) => {
    await api.setOverride(source, target, "denied")
    setRefreshKey((k) => k + 1)
  }

  const addCustomEdge = async () => {
    if (!newEdge.source || !newEdge.target) return
    await api.addCustomEdge(newEdge.source, newEdge.target, newEdge.reason)
    setNewEdge({ source: "", target: "", reason: "" })
    setShowAddEdge(false)
    setRefreshKey((k) => k + 1)
  }

  const allRepoNames = repos?.map((r) => `${r.owner}/${r.repo}`) ?? []

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          Cross-Repo Relationships
        </h1>
        <p className="text-sm text-muted-foreground">
          How your repositories reference each other — useful for tracing the
          impact of a change before merging.
        </p>
      </div>

      {loading && (
        <p className="text-sm text-muted-foreground">Loading...</p>
      )}
      {error && <p className="text-sm text-destructive">{error}</p>}

      {displayData && (
        <>
          {/* Graph — primary view */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle>Dependency Graph</CardTitle>
              <CardDescription>
                Each circle is a repo; lines show how they reference each
                other. Drag to rearrange, click a repo to open it.
                {displayData.groups.length > 0 && (
                  <>
                    {" "}
                    Colour-coded clusters group repos that share heavy mutual
                    references.
                  </>
                )}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <RelationshipGraph
                data={displayData}
                repoFileCounts={repoFileCounts}
              />
            </CardContent>
          </Card>

          {/* Connections table — replaces the old Edges tab */}
          <Card>
            <CardHeader>
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle>Connections</CardTitle>
                  <CardDescription>
                    {displayData.edges.length === 0
                      ? "No cross-repo references detected yet."
                      : `${displayData.edges.length} reference${
                          displayData.edges.length === 1 ? "" : "s"
                        } across ${
                          new Set(
                            displayData.edges.flatMap((e) => [
                              e.source_repo,
                              e.target_repo,
                            ]),
                          ).size
                        } repos. Approve, dismiss, or add your own.`}
                  </CardDescription>
                </div>
                <Button
                  size="sm"
                  onClick={() => setShowAddEdge(!showAddEdge)}
                >
                  <Plus className="mr-1 h-3 w-3" /> Add
                </Button>
              </div>
            </CardHeader>
            <CardContent>
              {showAddEdge && (
                <div className="mb-6 space-y-3 rounded-lg border p-4">
                  <div className="grid grid-cols-2 gap-3">
                    <Select
                      value={newEdge.source}
                      onValueChange={(v) =>
                        setNewEdge({ ...newEdge, source: v })
                      }
                    >
                      <SelectTrigger>
                        <SelectValue placeholder="Source repo..." />
                      </SelectTrigger>
                      <SelectContent>
                        {allRepoNames.map((r) => (
                          <SelectItem key={r} value={r}>
                            {r}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <Select
                      value={newEdge.target}
                      onValueChange={(v) =>
                        setNewEdge({ ...newEdge, target: v })
                      }
                    >
                      <SelectTrigger>
                        <SelectValue placeholder="Target repo..." />
                      </SelectTrigger>
                      <SelectContent>
                        {allRepoNames
                          .filter((r) => r !== newEdge.source)
                          .map((r) => (
                            <SelectItem key={r} value={r}>
                              {r}
                            </SelectItem>
                          ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <Input
                    placeholder="Reason (e.g. shares database, calls internal API)"
                    value={newEdge.reason}
                    onChange={(e) =>
                      setNewEdge({ ...newEdge, reason: e.target.value })
                    }
                  />
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      onClick={addCustomEdge}
                      disabled={!newEdge.source || !newEdge.target}
                    >
                      Add
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setShowAddEdge(false)}
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              )}

              {displayData.edges.length > 0 && (
                <div className="space-y-4">
                  {displayData.edges.map((e, i) => {
                    const [sOwner, sRepo] = e.source_repo.split("/")
                    const [tOwner, tRepo] = e.target_repo.split("/")
                    return (
                      <div key={i} className="flex items-center">
                        <div className="min-w-0 flex-1">
                          <Link
                            to={`/repos/${sOwner}/${sRepo}`}
                            className="text-sm font-medium leading-none hover:underline"
                          >
                            {e.source_repo}
                          </Link>
                        </div>
                        <ArrowRight className="mx-3 h-4 w-4 shrink-0 text-muted-foreground" />
                        <div className="min-w-0 flex-1">
                          <Link
                            to={`/repos/${tOwner}/${tRepo}`}
                            className="text-sm font-medium leading-none hover:underline"
                          >
                            {e.target_repo}
                          </Link>
                        </div>
                        <Badge variant="outline" className="ml-3 shrink-0">
                          {e.kind}
                        </Badge>
                        <div className="ml-3 flex shrink-0 gap-1">
                          <Button
                            size="icon"
                            variant="ghost"
                            className="h-8 w-8 text-muted-foreground hover:text-foreground"
                            onClick={() =>
                              confirmEdge(e.source_repo, e.target_repo)
                            }
                          >
                            <Check className="h-4 w-4" />
                          </Button>
                          <Button
                            size="icon"
                            variant="ghost"
                            className="h-8 w-8 text-muted-foreground hover:text-destructive"
                            onClick={() =>
                              denyEdge(e.source_repo, e.target_repo)
                            }
                          >
                            <X className="h-4 w-4" />
                          </Button>
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
