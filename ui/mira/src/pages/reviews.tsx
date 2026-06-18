import {
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Loader2,
} from "lucide-react"
import { useEffect, useState } from "react"

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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { api, type PaginatedReviews } from "@/lib/api"
import { useDocumentTitle } from "@/lib/hooks"

const PER_PAGE = 20

const STATUS_MAP: Record<
  string,
  { label: string; variant: "default" | "secondary" | "destructive" | "outline" }
> = {
  reviewing: { label: "Reviewing", variant: "default" },
  completed: { label: "Completed", variant: "secondary" },
  failed: { label: "Failed", variant: "destructive" },
}

function ago(ts: number): string {
  const sec = Math.floor((Date.now() / 1000 - ts))
  if (sec < 60) return `${sec}s ago`
  const min = Math.floor(sec / 60)
  if (min < 60) return `${min}m ago`
  const hr = Math.floor(min / 60)
  return `${hr}h ago`
}

export function RunningReviewsPage() {
  useDocumentTitle("Reviews")
  const [data, setData] = useState<PaginatedReviews | null>(null)
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(0)

  const pageItems = data?.items ?? []
  const total = data?.total ?? 0
  const hasActive = pageItems.some((r) => r.status === "reviewing")

  useEffect(() => {
    const load = () => {
      const offset = page * PER_PAGE
      api.getRunningReviews({ limit: PER_PAGE, offset }).then(setData).finally(() => setLoading(false))
    }
    load()
    const interval = setInterval(load, hasActive ? 3000 : 15000)
    return () => clearInterval(interval)
  }, [hasActive, page])

  const totalPages = Math.ceil(total / PER_PAGE)

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Reviews</h1>
        <p className="text-sm text-muted-foreground">
          Active and recent PR reviews
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>
            {loading ? (
              <Loader2 className="h-5 w-5 animate-spin" />
            ) : (
              total
            )}
          </CardTitle>
          <CardDescription>
            {total === 1 ? "review" : "reviews"} tracked
            {hasActive && " — polling live"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {total === 0 ? (
            <p className="text-sm text-muted-foreground">
              No reviews have been tracked yet.
            </p>
          ) : (
            <>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>PR</TableHead>
                    <TableHead>Title</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Started</TableHead>
                    <TableHead>Duration</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {pageItems.map((r) => {
                    const info = STATUS_MAP[r.status] ?? {
                      label: r.status,
                      variant: "outline" as const,
                    }
                    const dur =
                      r.status !== "reviewing" && r.finished_at > 0
                        ? `${Math.round(r.finished_at - r.started_at)}s`
                        : "—"

                    return (
                      <TableRow key={`${r.repo}#${r.pr_number}`}>
                        <TableCell className="font-medium">
                          <a
                            href={r.pr_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 underline-offset-2 hover:underline"
                          >
                            {r.repo}#{r.pr_number}
                            <ExternalLink className="h-3 w-3 text-muted-foreground" />
                          </a>
                        </TableCell>
                        <TableCell className="max-w-md truncate text-muted-foreground">
                          {r.pr_title || "—"}
                        </TableCell>
                        <TableCell>
                          <Badge variant={info.variant}>{info.label}</Badge>
                        </TableCell>
                        <TableCell className="text-muted-foreground tabular-nums">
                          {ago(r.started_at)}
                        </TableCell>
                        <TableCell className="tabular-nums">{dur}</TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>

              {totalPages > 1 && (
                <div className="mt-4 flex items-center justify-center gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={page === 0}
                    onClick={() => setPage(page - 1)}
                  >
                    <ChevronLeft className="h-4 w-4" />
                  </Button>
                  <span className="text-sm text-muted-foreground tabular-nums">
                    {page + 1} / {totalPages}
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={page >= totalPages - 1}
                    onClick={() => setPage(page + 1)}
                  >
                    <ChevronRight className="h-4 w-4" />
                  </Button>
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
