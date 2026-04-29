import { Loader2 } from "lucide-react"
import { useEffect, useState } from "react"

import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { api, type RepoListItem } from "@/lib/api"

function formatCost(usd: number): string {
  if (usd === 0) return "$0"
  if (usd < 0.01) return "~<$0.01"
  return `~$${usd.toFixed(2)}`
}

export function SetupModal({
  open,
  onComplete,
}: {
  open: boolean
  onComplete: () => void
}) {
  const [repos, setRepos] = useState<RepoListItem[]>([])
  const [indexingModel, setIndexingModel] = useState("")
  const [estimate, setEstimate] = useState<{
    estimated_usd: number
    file_count: number
    input_tokens: number
    output_tokens: number
  } | null>(null)
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (!open) return

    const load = async () => {
      const [data, models, est] = await Promise.all([
        api.listRepos(),
        api.getModels(),
        api.getCostEstimate().catch(() => null),
      ])
      setRepos(data.filter((r) => r.status === "pending"))
      setIndexingModel(models.indexing_model)
      if (est) setEstimate(est)
      setLoading(false)
    }

    load()
    const interval = setInterval(load, 2000)
    return () => clearInterval(interval)
  }, [open])

  const handleStart = async () => {
    setSubmitting(true)
    await api.completeSetup(
      repos.map((r) => ({
        owner: r.owner,
        repo: r.repo,
        enabled: true,
      })),
      "full",
    )
    onComplete()
  }

  const handleSkip = () => {
    // Mark these repos as skipped (index_mode = none) so the popup won't show again
    Promise.all(
      repos.map((r) => api.completeSetup([{ owner: r.owner, repo: r.repo, enabled: false }], "full"))
    ).finally(() => onComplete())
  }

  const orgName = repos.length > 0 ? repos[0].owner : "your organization"
  const totalFiles = estimate?.file_count ?? 0
  const hasFileCounts = totalFiles > 0

  return (
    <Dialog open={open}>
      <DialogContent className="sm:max-w-md [&>button]:hidden">
        <DialogHeader>
          <div className="flex items-center gap-3">
            <img src="/logo.png" alt="Mira" className="h-8 w-8" />
            <div>
              <DialogTitle>Set up {orgName}</DialogTitle>
              <DialogDescription>
                {loading
                  ? "Loading repositories..."
                  : `${repos.length} ${repos.length === 1 ? "repository" : "repositories"} ready to index`}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        {loading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        ) : (
          <>
            {/* Repo list */}
            <div className="max-h-[30vh] space-y-1 overflow-y-auto">
              {repos.map((r) => {
                const initials = r.repo
                  .split("-")
                  .map((w) => w[0])
                  .join("")
                  .toUpperCase()
                  .slice(0, 2)
                return (
                  <div
                    key={`${r.owner}/${r.repo}`}
                    className="flex items-center gap-3 rounded-lg px-3 py-2"
                  >
                    <Avatar className="h-7 w-7">
                      <AvatarFallback className="text-[10px]">
                        {initials}
                      </AvatarFallback>
                    </Avatar>
                    <div className="flex-1">
                      <p className="text-sm font-medium leading-none">
                        {r.repo}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {r.owner}
                      </p>
                    </div>
                    {r.file_count_estimate > 0 && (
                      <span className="text-xs text-muted-foreground">
                        {r.file_count_estimate} files
                      </span>
                    )}
                  </div>
                )
              })}
            </div>

            {/* Cost + model info */}
            <div className="rounded-lg border bg-muted/30 p-3">
              <div className="flex items-center justify-between text-sm">
                <span className="text-muted-foreground">Estimated cost</span>
                {estimate && estimate.file_count > 0 ? (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Badge variant="secondary" className="cursor-help">
                        {formatCost(estimate.estimated_usd)}
                      </Badge>
                    </TooltipTrigger>
                    <TooltipContent className="max-w-xs">
                      <p className="mb-1 font-medium">Rough estimate</p>
                      <p className="text-xs">
                        Based on ~800 input + 400 output tokens per file. Actual
                        cost varies with file size, symbol density, and retries.
                      </p>
                      <div className="mt-2 space-y-0.5 text-xs">
                        <div>Input: {estimate.input_tokens.toLocaleString()} tokens</div>
                        <div>Output: {estimate.output_tokens.toLocaleString()} tokens</div>
                      </div>
                    </TooltipContent>
                  </Tooltip>
                ) : (
                  <Badge variant="secondary" className="gap-1.5">
                    <Loader2 className="h-3 w-3 animate-spin" />
                    Calculating
                  </Badge>
                )}
              </div>
              <div className="mt-2 flex items-center justify-between text-sm">
                <span className="text-muted-foreground">Model</span>
                <span className="font-mono text-xs">{indexingModel}</span>
              </div>
              {hasFileCounts && estimate && (
                <p className="mt-2 text-xs text-muted-foreground">
                  {totalFiles} files · ~
                  {Math.round(
                    (estimate.input_tokens + estimate.output_tokens) / 1000,
                  )}
                  K tokens
                </p>
              )}
            </div>

            <p className="text-xs text-muted-foreground">
              To change which repos Mira can access, update your GitHub App
              installation permissions.
            </p>
          </>
        )}

        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={handleSkip} disabled={submitting}>
            Skip for now
          </Button>
          <Button onClick={handleStart} disabled={submitting || repos.length === 0 || loading}>
            {submitting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            Start Indexing
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
