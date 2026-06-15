import {
  Loader2,
  Pencil,
  Plus,
  Trash2,
  Webhook as WebhookIcon,
} from "lucide-react"
import { useState } from "react"
import { useNavigate } from "react-router"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { ConfirmButton } from "@/components/ui/confirm-button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { api } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { useAsync } from "@/lib/hooks"

type Webhook = {
  id: string
  name: string
  url_masked: string
  events: string[]
  enabled: boolean
  format: string
}

type EventOption = { value: string; label: string; description: string }

const FORMAT_LABEL: Record<string, string> = {
  slack: "Slack",
  teams: "Teams",
  generic: "Webhook",
}

export function WebhooksPage() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [refreshKey, setRefreshKey] = useState(0)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const { data, loading } = useAsync(() => api.getWebhooks(), [refreshKey])
  const webhooks: Webhook[] = data?.webhooks ?? []
  const events: EventOption[] = data?.available_events ?? []

  if (!user?.is_admin) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        Admin access required.
      </div>
    )
  }

  const labelFor = (value: string) =>
    events.find((e) => e.value === value)?.label ?? value

  const remove = async (id: string) => {
    setDeleteError(null)
    try {
      await api.deleteWebhook(id)
      setRefreshKey((k) => k + 1)
    } catch (e) {
      setDeleteError(
        `Failed to delete webhook: ${e instanceof Error ? e.message : String(e)}`
      )
    }
  }

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Webhooks</h1>
          <p className="text-sm text-muted-foreground">
            Send a webhook to any HTTPS endpoint when Mira reviews a PR or
            finishes indexing. Slack and Teams URLs are auto-formatted.
          </p>
        </div>
        {webhooks.length > 0 && (
          <Button size="sm" onClick={() => navigate("/settings/webhooks/new")}>
            <Plus className="mr-1 h-4 w-4" /> Add webhook
          </Button>
        )}
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading…
        </div>
      ) : webhooks.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center gap-4 py-16 text-center">
            <div className="flex size-12 items-center justify-center rounded-full bg-muted">
              <WebhookIcon className="size-6 text-muted-foreground" />
            </div>
            <div className="space-y-1">
              <p className="text-sm font-medium">No webhooks yet</p>
              <p className="max-w-sm text-sm text-muted-foreground">
                Add a webhook to get notified at any endpoint when Mira reviews
                or indexes.
              </p>
            </div>
            <Button
              size="sm"
              onClick={() => navigate("/settings/webhooks/new")}
            >
              <Plus className="mr-1 h-4 w-4" /> Add webhook
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card className="overflow-hidden py-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Destination</TableHead>
                <TableHead>Events</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {webhooks.map((w) => (
                <TableRow
                  key={w.id}
                  className="cursor-pointer"
                  onClick={() => navigate(`/settings/webhooks/${w.id}`)}
                >
                  <TableCell className="font-medium">
                    {w.name || "Untitled"}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <Badge variant="secondary">
                        {FORMAT_LABEL[w.format] ?? "Webhook"}
                      </Badge>
                      <span className="font-mono text-xs text-muted-foreground">
                        {w.url_masked}
                      </span>
                    </div>
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {w.events.length ? (
                        w.events.map((e) => (
                          <Badge key={e} variant="outline">
                            {labelFor(e)}
                          </Badge>
                        ))
                      ) : (
                        <span className="text-xs text-muted-foreground">
                          None
                        </span>
                      )}
                    </div>
                  </TableCell>
                  <TableCell
                    className="text-right"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <div className="flex items-center justify-end gap-1">
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            onClick={() =>
                              navigate(`/settings/webhooks/${w.id}`)
                            }
                          >
                            <Pencil className="h-3.5 w-3.5" />
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent>Edit</TooltipContent>
                      </Tooltip>
                      <ConfirmButton
                        variant="ghost"
                        size="icon-sm"
                        tooltip="Delete"
                        dialogTitle="Delete webhook?"
                        dialogDescription={`"${w.name || "Untitled"}" will stop receiving events. This can't be undone.`}
                        confirmLabel="Delete"
                        destructive
                        onConfirm={() => remove(w.id)}
                      >
                        <Trash2 className="h-3.5 w-3.5 text-destructive" />
                      </ConfirmButton>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}

      {deleteError && (
        <p className="text-sm break-words text-destructive">{deleteError}</p>
      )}
    </div>
  )
}
