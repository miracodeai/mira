import { AlertTriangle, Loader2, Search } from "lucide-react"
import { useEffect, useMemo, useState } from "react"
import { Link } from "react-router"

import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { api, type PackageSearchHit } from "@/lib/api"

const KIND_LABELS: Record<string, string> = {
  npm: "npm",
  pip: "pip",
  docker: "Docker",
  go: "Go",
  rust: "Cargo",
}

const KIND_COLORS: Record<string, string> = {
  npm: "text-red-400 border-red-500/40",
  pip: "text-yellow-400 border-yellow-500/40",
  docker: "text-blue-400 border-blue-500/40",
  go: "text-cyan-400 border-cyan-500/40",
  rust: "text-orange-400 border-orange-500/40",
}

export function PackagesPage() {
  const [name, setName] = useState("")
  const [version, setVersion] = useState("")
  const [kind, setKind] = useState<string | null>(null)
  const [devFilter, setDevFilter] = useState<"all" | "prod" | "dev">("all")

  const [hits, setHits] = useState<PackageSearchHit[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState("")

  // Debounced auto-search — fires 400ms after the user stops typing.
  useEffect(() => {
    if (!name.trim() && !version.trim() && !kind && devFilter === "all") {
      setHits([])
      setError("")
      return
    }
    const t = setTimeout(async () => {
      setLoading(true)
      setError("")
      try {
        const results = await api.searchPackages({
          name: name.trim() || undefined,
          version: version.trim() || undefined,
          kind: kind || undefined,
          is_dev:
            devFilter === "all"
              ? undefined
              : devFilter === "dev",
        })
        setHits(results)
      } catch (err) {
        setError(err instanceof Error ? err.message : "Search failed")
        setHits([])
      } finally {
        setLoading(false)
      }
    }, 400)
    return () => clearTimeout(t)
  }, [name, version, kind, devFilter])

  // Group hits by package (name + version) so the caller can see at a glance
  // "which repos use lodash@4.17.20".
  const grouped = useMemo(() => {
    const map = new Map<
      string,
      { name: string; kind: string; version: string; hits: PackageSearchHit[] }
    >()
    for (const h of hits) {
      const key = `${h.kind}:${h.name}:${h.version}`
      const entry = map.get(key)
      if (entry) {
        entry.hits.push(h)
      } else {
        map.set(key, {
          name: h.name,
          kind: h.kind,
          version: h.version,
          hits: [h],
        })
      }
    }
    return [...map.values()].sort((a, b) => b.hits.length - a.hits.length)
  }, [hits])

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Packages</h1>
        <p className="text-sm text-muted-foreground">
          Search every repo in your org for a package + version. Built for
          incident response — find which repos are running a vulnerable version
          in seconds.
        </p>
      </div>

      <>
          {/* Search form */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Search</CardTitle>
              <CardDescription>
                Partial names match. Leave fields blank to skip the filter.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="grid gap-3 md:grid-cols-[1fr_1fr_auto]">
                <div className="relative">
                  <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    placeholder="Package name (e.g. lodash)"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    className="pl-8"
                  />
                </div>
                <Input
                  placeholder="Version (e.g. 4.17.20)"
                  value={version}
                  onChange={(e) => setVersion(e.target.value)}
                  className="font-mono text-xs"
                />
                <div className="inline-flex rounded-md border">
                  {(
                    [
                      ["all", "All"],
                      ["prod", "Prod"],
                      ["dev", "Dev"],
                    ] as const
                  ).map(([value, label]) => {
                    const active = devFilter === value
                    return (
                      <button
                        key={value}
                        type="button"
                        onClick={() => setDevFilter(value)}
                        className={`px-3 py-1.5 text-xs font-medium transition-colors first:rounded-l-md last:rounded-r-md ${
                          active
                            ? "bg-foreground text-background"
                            : "text-muted-foreground hover:bg-muted"
                        }`}
                      >
                        {label}
                      </button>
                    )
                  })}
                </div>
              </div>
              <div className="mt-3 flex flex-wrap gap-1">
                <button
                  type="button"
                  onClick={() => setKind(null)}
                  className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                    kind === null
                      ? "bg-foreground text-background"
                      : "text-muted-foreground hover:bg-muted"
                  }`}
                >
                  Any ecosystem
                </button>
                {["npm", "pip", "docker", "go", "rust"].map((k) => {
                  const active = kind === k
                  return (
                    <button
                      key={k}
                      type="button"
                      onClick={() => setKind(active ? null : k)}
                      className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                        active
                          ? "bg-foreground text-background"
                          : "text-muted-foreground hover:bg-muted"
                      }`}
                    >
                      {KIND_LABELS[k] ?? k}
                    </button>
                  )
                })}
              </div>
            </CardContent>
          </Card>

          {/* Results */}
          <Card>
            <CardHeader>
              <div className="flex items-center gap-2">
                <CardTitle className="text-base">Results</CardTitle>
                {loading && (
                  <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />
                )}
                {!loading && hits.length > 0 && (
                  <Badge variant="secondary" className="tabular-nums">
                    {hits.length} {hits.length === 1 ? "match" : "matches"} in{" "}
                    {new Set(hits.map((h) => `${h.owner}/${h.repo}`)).size}{" "}
                    repos
                  </Badge>
                )}
              </div>
            </CardHeader>
            <CardContent>
              {error && (
                <div className="mb-4 flex items-start gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm">
                  <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
                  <span>{error}</span>
                </div>
              )}
              {!loading && hits.length === 0 && !error ? (
                <p className="py-4 text-sm text-muted-foreground">
                  {name || version || kind || devFilter !== "all"
                    ? "No packages matched your search."
                    : "Enter a package name or version to begin."}
                </p>
              ) : (
                <div className="space-y-6">
                  {grouped.map((g) => (
                    <div key={`${g.kind}-${g.name}-${g.version}`} className="space-y-2">
                      <div className="flex items-center gap-2">
                        <Badge
                          variant="outline"
                          className={`text-[10px] ${KIND_COLORS[g.kind] ?? "text-muted-foreground"}`}
                        >
                          {KIND_LABELS[g.kind] ?? g.kind}
                        </Badge>
                        <span className="font-mono text-sm font-medium">
                          {g.name}
                        </span>
                        <span className="font-mono text-xs text-muted-foreground">
                          {g.version || "—"}
                        </span>
                        <span className="text-xs text-muted-foreground">
                          · {g.hits.length} repo{g.hits.length !== 1 ? "s" : ""}
                        </span>
                      </div>
                      <div className="overflow-hidden rounded-lg border">
                        <table className="w-full text-sm">
                          <tbody>
                            {g.hits.map((h, i) => (
                              <tr
                                key={`${h.owner}-${h.repo}-${h.file_path}-${i}`}
                                className="border-t first:border-t-0"
                              >
                                <td className="px-4 py-2">
                                  <Link
                                    to={`/repos/${h.owner}/${h.repo}`}
                                    className="font-medium hover:underline"
                                  >
                                    {h.owner}/{h.repo}
                                  </Link>
                                </td>
                                <td className="px-4 py-2 font-mono text-xs text-muted-foreground">
                                  {h.file_path}
                                </td>
                                <td className="w-16 px-4 py-2 text-right">
                                  {h.is_dev && (
                                    <span className="text-[10px] tracking-wide text-muted-foreground">
                                      dev
                                    </span>
                                  )}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </>
    </div>
  )
}
