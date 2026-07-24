import { Loader2 } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"

function parseDetail(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e)
  try {
    const parsed = JSON.parse(raw.replace(/^API error \d+: /, ""))
    if (parsed?.error) return parsed.error
    if (parsed?.detail)
      return typeof parsed.detail === "string"
        ? parsed.detail
        : JSON.stringify(parsed.detail)
  } catch {
    /* ignore */
  }
  return raw
}

/**
 * Password new/confirm form (with an optional current-password field). Used by
 * the change-password and admin reset-password pages. `onSubmit` should perform
 * the request and navigate away on success; thrown errors are shown inline.
 */
export function PasswordForm({
  requireCurrent = false,
  submitLabel = "Save",
  onSubmit,
  onCancel,
}: {
  requireCurrent?: boolean
  submitLabel?: string
  onSubmit: (currentPassword: string, newPassword: string) => Promise<void>
  onCancel: () => void
}) {
  const [current, setCurrent] = useState("")
  const [next, setNext] = useState("")
  const [confirm, setConfirm] = useState("")
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const mismatch = confirm.length > 0 && next !== confirm
  const canSubmit =
    !saving &&
    next.length > 0 &&
    next === confirm &&
    (!requireCurrent || current.length > 0)

  const submit = async () => {
    setError(null)
    setSaving(true)
    try {
      // On success the caller navigates away (this component unmounts), so we
      // only reset `saving` on failure.
      await onSubmit(current, next)
    } catch (e) {
      setError(parseDetail(e))
      setSaving(false)
    }
  }

  return (
    <div className="space-y-4">
      {requireCurrent && (
        <div className="space-y-1.5">
          <label className="text-sm font-medium" htmlFor="pw-current">
            Current password
          </label>
          <Input
            id="pw-current"
            type="password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </div>
      )}
      <div className="space-y-1.5">
        <label className="text-sm font-medium" htmlFor="pw-new">
          New password
        </label>
        <Input
          id="pw-new"
          type="password"
          value={next}
          onChange={(e) => setNext(e.target.value)}
        />
      </div>
      <div className="space-y-1.5">
        <label className="text-sm font-medium" htmlFor="pw-confirm">
          Confirm new password
        </label>
        <Input
          id="pw-confirm"
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          aria-invalid={mismatch || undefined}
        />
        {mismatch && (
          <p className="text-xs text-destructive">Passwords don't match.</p>
        )}
      </div>
      {error && <p className="text-sm break-words text-destructive">{error}</p>}
      <div className="flex gap-2">
        <Button onClick={submit} disabled={!canSubmit}>
          {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          {submitLabel}
        </Button>
        <Button variant="ghost" onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
      </div>
    </div>
  )
}
