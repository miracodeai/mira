import { AlertTriangle, Loader2, Search } from "lucide-react"
import { useEffect, useState } from "react"
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
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

  // Debounced auto-search — fires 400ms after the user stops typing. With
  // no filters set it loads the full list so the page isn't blank on first
  // visit.
  useEffect(() => {
    const hasFilter = name.trim() || version.trim() || kind || devFilter !== "all"
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
    }, hasFilter ? 400 : 0)
    return () => clearTimeout(t)
  }, [name, version, kind, devFilter])

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
            <CardContent className="px-0 pb-0">
              {error && (
                <div className="mx-6 mb-4 flex items-start gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm">
                  <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
                  <span>{error}</span>
                </div>
              )}
              {!loading && hits.length === 0 && !error ? (
                <p className="px-6 py-4 text-sm text-muted-foreground">
                  {name || version || kind || devFilter !== "all"
                    ? "No packages matched your search."
                    : "No packages indexed yet. Index a repo to populate this page."}
                </p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-[90px] pl-6">Ecosystem</TableHead>
                      <TableHead>Package</TableHead>
                      <TableHead className="w-[140px]">Version</TableHead>
                      <TableHead>Repo</TableHead>
                      <TableHead className="hidden lg:table-cell">Manifest</TableHead>
                      <TableHead className="w-[60px] pr-6 text-right">Type</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {hits.map((h, i) => (
                      <TableRow
                        key={`${h.owner}-${h.repo}-${h.file_path}-${h.name}-${i}`}
                      >
                        <TableCell className="pl-6">
                          <Badge
                            variant="outline"
                            className={`text-[10px] ${KIND_COLORS[h.kind] ?? "text-muted-foreground"}`}
                          >
                            {KIND_LABELS[h.kind] ?? h.kind}
                          </Badge>
                        </TableCell>
                        <TableCell className="font-mono text-sm">
                          {h.name}
                        </TableCell>
                        <TableCell className="font-mono text-xs text-muted-foreground">
                          {h.version || "—"}
                        </TableCell>
                        <TableCell>
                          <Link
                            to={`/repos/${h.owner}/${h.repo}`}
                            className="text-sm hover:underline"
                          >
                            {h.owner}/{h.repo}
                          </Link>
                        </TableCell>
                        <TableCell className="hidden font-mono text-xs text-muted-foreground lg:table-cell">
                          {h.file_path}
                        </TableCell>
                        <TableCell className="pr-6 text-right">
                          {h.is_dev ? (
                            <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
                              dev
                            </span>
                          ) : (
                            <span className="text-[10px] uppercase tracking-wide text-muted-foreground/60">
                              prod
                            </span>
                          )}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </>
    </div>
  )
}
