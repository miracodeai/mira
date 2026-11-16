import { Loader2 } from "lucide-react"
import { type FormEvent, useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { api } from "@/lib/api"
import { errorText } from "@/lib/api/http"
import type { ConnectedProvider, ProviderPreset } from "@/lib/api/settings"

import { ProviderIcon } from "./shared"

// API-key and local providers: presets, connected rows, and the custom form.

function DisconnectButton({
  id,
  onChanged,
}: {
  id: string
  onChanged: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const disconnect = async () => {
    setBusy(true)
    setError("")
    try {
      await api.disconnectApiProvider(id)
      onChanged()
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="text-right">
      <Button size="sm" variant="outline" disabled={busy} onClick={disconnect}>
        Disconnect
      </Button>
      {error && <p className="mt-1 text-xs text-destructive">{error}</p>}
    </div>
  )
}

function ConnectForm({
  preset,
  onDone,
}: {
  preset?: ProviderPreset
  onDone: () => void
}) {
  const [label, setLabel] = useState("")
  const [baseUrl, setBaseUrl] = useState(preset?.base_url ?? "")
  const [key, setKey] = useState("")
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const keyMode = preset?.key ?? "optional"
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError("")
    try {
      await api.connectApiProvider({
        preset_id: preset?.id,
        label,
        base_url: baseUrl,
        api_key: key,
      })
      onDone()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }
  return (
    <form onSubmit={submit} className="mt-3 space-y-2 border-t pt-3">
      {!preset && (
        <Input
          aria-label="Provider name"
          placeholder="Name, e.g. My vLLM"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
      )}
      <Input
        aria-label="Base URL"
        placeholder="https://example.com/v1 (OpenAI-compatible)"
        value={baseUrl}
        onChange={(e) => setBaseUrl(e.target.value)}
      />
      {keyMode !== "none" && (
        <Input
          type="password"
          aria-label="API key"
          placeholder={
            keyMode === "required" ? "API key" : "API key (optional)"
          }
          value={key}
          onChange={(e) => setKey(e.target.value)}
        />
      )}
      {error && <p className="text-xs text-destructive">{error}</p>}
      <div className="flex items-center justify-between gap-2">
        {preset?.dashboard ? (
          <a
            href={preset.dashboard}
            target="_blank"
            rel="noreferrer"
            className="text-xs underline"
          >
            Get an API key
          </a>
        ) : (
          <span />
        )}
        <Button
          size="sm"
          type="submit"
          disabled={
            busy ||
            !baseUrl.trim() ||
            (!preset && !label.trim()) ||
            (keyMode === "required" && !key.trim())
          }
        >
          {busy && <Loader2 className="mr-2 h-3 w-3 animate-spin" />}
          Connect
        </Button>
      </div>
    </form>
  )
}

export function PresetRow({
  preset,
  connected,
  onChanged,
}: {
  preset: ProviderPreset
  connected: boolean
  onChanged: () => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-lg border bg-muted/30 p-3">
      <div className="flex items-start gap-3">
        <ProviderIcon id={preset.id} label={preset.label} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5 text-sm font-medium">
            {preset.label}
            {preset.tier === "free" && (
              <Badge variant="secondary" className="text-green-500">
                Free
              </Badge>
            )}
            <Badge variant="outline">
              {preset.key === "required"
                ? "API key"
                : preset.key === "optional"
                  ? "Key optional"
                  : "No key"}
            </Badge>
            {connected && (
              <Badge variant="secondary" className="text-green-500">
                Connected
              </Badge>
            )}
          </div>
          {preset.note && (
            <p className="mt-0.5 text-xs text-muted-foreground">
              {preset.note}
            </p>
          )}
        </div>
        {connected ? (
          <DisconnectButton id={preset.id} onChanged={onChanged} />
        ) : (
          <Button
            size="sm"
            variant={open ? "outline" : "default"}
            onClick={() => setOpen(!open)}
          >
            {open ? "Cancel" : "Connect"}
          </Button>
        )}
      </div>
      {open && !connected && (
        <ConnectForm
          preset={preset}
          onDone={() => {
            setOpen(false)
            onChanged()
          }}
        />
      )}
    </div>
  )
}

export function ConnectedProviderRow({
  provider,
  onChanged,
}: {
  provider: ConnectedProvider
  onChanged: () => void
}) {
  return (
    <div className="flex items-center gap-3 rounded-lg border bg-muted/30 p-3">
      <ProviderIcon id={provider.id} label={provider.label} />
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium">{provider.label}</div>
        <div className="truncate text-xs text-muted-foreground">
          {provider.base_url}
          {provider.has_key ? " · API key" : ""}
        </div>
      </div>
      <DisconnectButton id={provider.id} onChanged={onChanged} />
    </div>
  )
}

export function CustomProvider({ onDone }: { onDone: () => void }) {
  const [open, setOpen] = useState(false)
  if (!open) {
    return (
      <div className="text-right">
        <button
          type="button"
          className="text-xs underline"
          onClick={() => setOpen(true)}
        >
          Provider not listed? Add a custom one
        </button>
      </div>
    )
  }
  return (
    <div className="rounded-lg border bg-muted/30 p-3">
      <div className="flex items-center justify-between text-sm font-medium">
        Custom OpenAI-compatible provider
        <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
      <ConnectForm
        onDone={() => {
          setOpen(false)
          onDone()
        }}
      />
    </div>
  )
}
