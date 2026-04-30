import { AlertTriangle, Loader2 } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { api } from "@/lib/api"

export function UninstallModal({
  installationId,
  owner,
  onDone,
}: {
  installationId: number
  owner: string
  onDone: () => void
}) {
  const [submitting, setSubmitting] = useState(false)

  const handleKeep = async () => {
    setSubmitting(true)
    await api.keepUninstallData(installationId)
    setSubmitting(false)
    onDone()
  }

  const handleDelete = async () => {
    setSubmitting(true)
    await api.deleteUninstallData(installationId)
    setSubmitting(false)
    onDone()
  }

  return (
    <Dialog open>
      <DialogContent className="sm:max-w-md [&>button]:hidden">
        <DialogHeader>
          <div className="flex items-center gap-3">
            <div className="rounded-lg bg-destructive/10 p-2">
              <AlertTriangle className="h-5 w-5 text-destructive" />
            </div>
            <div>
              <DialogTitle>Mira was uninstalled from {owner}</DialogTitle>
              <DialogDescription>
                What would you like to do with the existing data?
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="space-y-3 py-2">
          <div className="rounded-lg border p-3">
            <p className="text-sm font-medium">Keep data</p>
            <p className="text-xs text-muted-foreground">
              Preserves indexes, reviews, and relationships for {owner}. If you
              re-install the app later, it will pick up where it left off.
            </p>
          </div>
          <div className="rounded-lg border p-3">
            <p className="text-sm font-medium">Delete everything</p>
            <p className="text-xs text-muted-foreground">
              Permanently removes all repos, indexes, reviews, and settings for{" "}
              {owner}. This cannot be undone.
            </p>
          </div>
        </div>

        <DialogFooter className="gap-2">
          <Button
            variant="destructive"
            onClick={handleDelete}
            disabled={submitting}
          >
            {submitting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            Delete everything
          </Button>
          <Button onClick={handleKeep} disabled={submitting}>
            Keep data
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
