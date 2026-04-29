import { ArrowRight, Check, Plus, X } from "lucide-react"
import { useState } from "react"
import { Link } from "react-router"

import { Avatar, AvatarFallback } from "@/components/ui/avatar"
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
import { Separator } from "@/components/ui/separator"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
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
          Edges and groups detected across indexed repositories
        </p>
      </div>

      {loading && (
        <p className="text-sm text-muted-foreground">Loading...</p>
      )}
      {error && <p className="text-sm text-destructive">{error}</p>}

      {displayData && (
        <Tabs defaultValue="graph">
          <TabsList>
            <TabsTrigger value="graph">Graph</TabsTrigger>
            <TabsTrigger value="groups">
              Groups ({displayData.groups.length})
            </TabsTrigger>
            <TabsTrigger value="edges">
              Edges ({displayData.edges.length})
            </TabsTrigger>
          </TabsList>

          {/* Graph */}
          <TabsContent value="graph">
            <Card>
              <CardHeader className="pb-3">
                <CardTitle>Dependency Graph</CardTitle>
                <CardDescription>
                  Drag nodes to rearrange. Click a repo to view details.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <RelationshipGraph
                  data={displayData}
                  repoFileCounts={repoFileCounts}
                />
              </CardContent>
            </Card>
          </TabsContent>

          {/* Groups */}
          <TabsContent value="groups">
            {displayData.groups.length > 0 ? (
              <div className="grid gap-4 lg:grid-cols-2">
                {displayData.groups.map((g) => (
                  <Card key={g.name}>
                    <CardHeader className="pb-2">
                      <CardDescription>
                        {g.repos.length} repositories
                      </CardDescription>
                      <CardTitle className="flex items-center justify-between">
                        <span>{g.name}</span>
                        <span className="text-sm font-normal text-muted-foreground">
                          {Math.round(g.confidence * 100)}% confidence
                        </span>
                      </CardTitle>
                    </CardHeader>
                    <CardContent>
                      <div className="space-y-4">
                        {g.repos.map((r) => {
                          const [owner, repo] = r.split("/")
                          const initials = repo
                            .split("-")
                            .map((w) => w[0])
                            .join("")
                            .toUpperCase()
                            .slice(0, 2)
                          return (
                            <Link
                              key={r}
                              to={`/repos/${owner}/${repo}`}
                              className="flex items-center"
                            >
                              <Avatar className="h-8 w-8">
                                <AvatarFallback className="text-xs">
                                  {initials}
                                </AvatarFallback>
                              </Avatar>
                              <div className="ml-3 space-y-1">
                                <p className="text-sm font-medium leading-none">
                                  {repo}
                                </p>
                                <p className="text-xs text-muted-foreground">
                                  {owner}
                                </p>
                              </div>
                            </Link>
                          )
                        })}
                      </div>
                      <Separator className="my-4" />
                      <div className="space-y-1">
                        {g.evidence.map((ev, i) => (
                          <p
                            key={i}
                            className="text-xs text-muted-foreground"
                          >
                            {ev}
                          </p>
                        ))}
                      </div>
                    </CardContent>
                  </Card>
                ))}
              </div>
            ) : (
              <Card>
                <CardContent className="py-8 text-center text-sm text-muted-foreground">
                  No repo groups detected yet.
                </CardContent>
              </Card>
            )}
          </TabsContent>

          {/* Edges */}
          <TabsContent value="edges">
            <Card>
              <CardHeader>
                <div className="flex items-center justify-between">
                  <div>
                    <CardTitle>Cross-Repo Edges</CardTitle>
                    <CardDescription>
                      Confirm, deny, or add relationships
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

                {displayData.edges.length > 0 ? (
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
                ) : (
                  <p className="text-sm text-muted-foreground">
                    No cross-repo edges detected.
                  </p>
                )}
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
      )}
    </div>
  )
}
