import { ChevronLeft, Loader2, Send, Trash2 } from "lucide-react"
import { useEffect, useState } from "react"
import { useNavigate, useParams } from "react-router"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { ConfirmButton } from "@/components/ui/confirm-button"
import { Input } from "@/components/ui/input"
import { api } from "@/lib/api"
import { useAuth } from "@/lib/auth"

type EventOption = { value: string; label: string; description: string }

// The API returns `{detail: "..."}` for errors; postJson/putJson wrap the body
// as `API error NNN: <body>`. Strip the prefix and recover the detail string.
function parseDetail(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e)
  try {
    const parsed = JSON.parse(raw.replace(/^API error \d+: /, ""))
    if (parsed?.detail)
      return typeof parsed.detail === "string"
        ? parsed.detail
        : JSON.stringify(parsed.detail)
  } catch {
    /* ignore */
  }
  return raw
}

export function WebhookFormPage() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const { id } = useParams()
  const isEdit = Boolean(id)

  const [events, setEvents] = useState<EventOption[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<{
    ok: boolean
    detail: string
  } | null>(null)

  const [name, setName] = useState("")
  const [url, setUrl] = useState("")
  const [selected, setSelected] = useState<string[]>([])

  useEffect(() => {
    // Non-admins never reach the loading UI (the component returns the
    // access-required notice first), so there's nothing to toggle here.
    if (!user?.is_admin) return
    // The event picker comes from the list endpoint; for edit we also fetch
    // the full webhook (incl. the real, unmasked URL) so the form is prefilled
    // with the actual values.
    Promise.all([
      api.getWebhooks(),
      isEdit && id ? api.getWebhook(id) : Promise.resolve(null),
    ])
      .then(([list, w]) => {
        setEvents(list.available_events)
        if (w) {
          setName(w.name)
          setUrl(w.url)
          setSelected(w.events)
        }
        setLoading(false)
      })
      .catch((e) => {
        setError(parseDetail(e))
        setLoading(false)
      })
  }, [user, id, isEdit])

  if (!user?.is_admin) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        Admin access required.
      </div>
    )
  }

  const toggle = (value: string) =>
    setSelected((p) =>
      p.includes(value) ? p.filter((x) => x !== value) : [...p, value]
    )

  const save = async () => {
    setError(null)
    setSaving(true)
    try {
      if (isEdit) {
        await api.updateWebhook(id!, {
          name: name.trim(),
          url: url.trim(),
          events: selected,
        })
      } else {
        await api.createWebhook({
          name: name.trim(),
          url: url.trim(),
          events: selected,
        })
      }
      navigate("/settings/webhooks")
    } catch (e) {
      setError(parseDetail(e))
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    if (!id) return
    setError(null)
    try {
      await api.deleteWebhook(id)
      navigate("/settings/webhooks")
    } catch (e) {
      setError(parseDetail(e))
    }
  }

  const test = async () => {
    if (!id) return
    setTesting(true)
    try {
      setTestResult(await api.testWebhook(id))
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6 p-6">
      <button
        onClick={() => navigate("/settings/webhooks")}
        className="flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ChevronLeft className="h-4 w-4" /> Webhooks
      </button>

      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          {isEdit ? "Edit webhook" : "Add webhook"}
        </h1>
        <p className="text-sm text-muted-foreground">
          Send events to any HTTPS endpoint when Mira reviews or indexes.
        </p>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading…
        </div>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Details</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <label className="text-sm font-medium" htmlFor="wh-name">
                  Name
                </label>
                <Input
                  id="wh-name"
                  placeholder="e.g. Engineering reviews"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>
              <div className="space-y-2">
                <label className="text-sm font-medium" htmlFor="wh-url">
                  Endpoint URL
                </label>
                <Input
                  id="wh-url"
                  placeholder="https://hooks.slack.com/services/…"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  Any HTTPS endpoint works. Slack and Teams URLs are
                  auto-formatted; everything else receives a generic JSON
                  payload.
                </p>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Events</CardTitle>
              <CardDescription>
                Choose which events trigger this webhook.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-2">
              {events.map((ev) => {
                const checked = selected.includes(ev.value)
                return (
                  <label
                    key={ev.value}
                    className={
                      "flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors " +
                      (checked
                        ? "border-primary/40 bg-accent/50"
                        : "hover:bg-accent/40")
                    }
                  >
                    <Checkbox
                      className="mt-0.5"
                      checked={checked}
                      onCheckedChange={() => toggle(ev.value)}
                    />
                    <div className="space-y-0.5">
                      <div className="text-sm font-medium">{ev.label}</div>
                      <div className="text-xs text-muted-foreground">
                        {ev.description}
                      </div>
                    </div>
                  </label>
                )
              })}
            </CardContent>
          </Card>

          {error && (
            <p className="text-sm break-words text-destructive">{error}</p>
          )}
          {testResult && (
            <p
              className={
                testResult.ok
                  ? "text-sm text-muted-foreground"
                  : "text-sm text-destructive"
              }
            >
              {testResult.ok ? "Test delivered" : "Test failed"} —{" "}
              {testResult.detail}
            </p>
          )}

          <div className="flex items-center justify-between">
            <div className="flex gap-2">
              <Button
                onClick={save}
                disabled={saving || selected.length === 0 || !url.trim()}
              >
                {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                {isEdit ? "Save changes" : "Create webhook"}
              </Button>
              <Button
                variant="ghost"
                onClick={() => navigate("/settings/webhooks")}
              >
                Cancel
              </Button>
            </div>
            {isEdit && (
              <div className="flex gap-2">
                <Button variant="ghost" onClick={test} disabled={testing}>
                  {testing ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <Send className="mr-2 h-4 w-4" />
                  )}
                  Send test
                </Button>
                <ConfirmButton
                  variant="ghost"
                  className="text-destructive"
                  dialogTitle="Delete webhook?"
                  dialogDescription={`"${name || "Untitled"}" will stop receiving events. This can't be undone.`}
                  confirmLabel="Delete"
                  destructive
                  onConfirm={remove}
                >
                  <Trash2 className="mr-2 h-4 w-4" /> Delete
                </ConfirmButton>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
