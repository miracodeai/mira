import { Loader2, Search } from "lucide-react"
import { useEffect, useState } from "react"
import { Link } from "react-router"

import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { api, type RepoListItem } from "@/lib/api"

export function ReposPage() {
  const [repos, setRepos] = useState<RepoListItem[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState("")

  // Initial load + poll while any repo is indexing
  useEffect(() => {
    const load = () => {
      api.listRepos().then(setRepos).finally(() => setLoading(false))
    }
    load()
    const interval = setInterval(load, 5000)
    return () => clearInterval(interval)
  }, [])

  const hasIndexing = repos.some((r) => r.status === "indexing")

  const filtered = repos.filter(
    (r) =>
      r.owner.toLowerCase().includes(search.toLowerCase()) ||
      r.repo.toLowerCase().includes(search.toLowerCase()),
  )

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Repositories</h1>
        <p className="text-sm text-muted-foreground">
          All repositories and their indexing status
        </p>
      </div>

      {/* Indexing banner */}
      {hasIndexing && (
        <Card className="relative overflow-hidden border-primary/30 bg-primary/5">
          <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-primary to-transparent [background-size:200%_100%] [animation:shimmer_2s_linear_infinite]" />
          <CardContent className="flex items-center gap-4 p-4">
            <div className="relative flex h-8 w-8 items-center justify-center">
              <div className="absolute inset-1 rounded-full border-2 border-primary/40 border-t-primary [animation:spin_1s_linear_infinite]" />
            </div>
            <p className="text-sm font-medium">Indexing in progress...</p>
          </CardContent>
        </Card>
      )}

      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          placeholder="Search repositories..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="pl-9"
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>
            {loading ? "Loading..." : `${filtered.length} repositories`}
          </CardTitle>
          <CardDescription>Click a repository to view details</CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <p className="text-sm text-muted-foreground">Loading...</p>
          ) : filtered.length > 0 ? (
            <div className="space-y-4">
              {filtered.map((r) => {
                const key = `${r.owner}/${r.repo}`
                const initials = r.repo
                  .split("-")
                  .map((w) => w[0])
                  .join("")
                  .toUpperCase()
                  .slice(0, 2)

                return (
                  <Link
                    key={key}
                    to={`/repos/${r.owner}/${r.repo}`}
                    className="flex items-center"
                  >
                    <Avatar className="h-9 w-9">
                      <AvatarFallback>{initials}</AvatarFallback>
                    </Avatar>
                    <div className="ml-4 space-y-1">
                      <p className="text-sm font-medium leading-none">
                        {r.repo}
                      </p>
                      <p className="text-sm text-muted-foreground">{r.owner}</p>
                    </div>
                    <div className="ml-auto flex items-center gap-2">
                      <StatusBadge status={r.status} error={r.error} />
                      {r.status === "ready" && (
                        <span className="text-sm font-medium">
                          {r.file_count} files
                        </span>
                      )}
                    </div>
                  </Link>
                )
              })}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              No repositories found.
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function StatusBadge({ status, error }: { status: string; error?: string }) {
  switch (status) {
    case "indexing":
      return (
        <Badge variant="secondary" className="gap-1">
          <Loader2 className="h-3 w-3 animate-spin" />
          Indexing
        </Badge>
      )
    case "ready":
      return null
    case "failed":
      return (
        <Badge variant="destructive" title={error}>
          Failed
        </Badge>
      )
    default:
      return (
        <Badge variant="outline" className="text-muted-foreground">
          Pending
        </Badge>
      )
  }
}
