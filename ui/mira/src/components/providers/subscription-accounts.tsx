import { Loader2 } from "lucide-react"
import { useCallback, useEffect, useState } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { api } from "@/lib/api"
import { errorText } from "@/lib/api/http"
import type {
  LlmAccount,
  LlmAccountProvider,
  LlmAccounts,
  UsageWindow,
} from "@/lib/api/settings"

import { ProviderIcon } from "./shared"

// Subscription sign-in (ChatGPT device code, Claude PKCE), account switching, and usage.

// `search` holds extra terms so "chatgpt" finds the OpenAI row.
const ROWS: { key: LlmAccountProvider; name: string; search: string }[] = [
  { key: "chatgpt", name: "OpenAI (Codex login)", search: "chatgpt" },
  { key: "anthropic", name: "Anthropic (Claude)", search: "" },
]

type Login = { loginId: string; url: string; code?: string; error?: string }

type Usage = Partial<
  Record<LlmAccountProvider, Record<string, UsageWindow[] | undefined>>
>

// Claude redirects to localhost:54545, which only reaches Mira when both run on this machine.
const REMOTE = !["localhost", "127.0.0.1", "[::1]"].includes(
  window.location.hostname
)

function UsageBars({ windows }: { windows?: UsageWindow[] }) {
  if (!windows?.length) return null
  return (
    <div className="mt-2 space-y-1.5">
      {windows.map((w) => (
        <div key={w.label} className="flex items-center gap-2 text-xs">
          <span className="w-14 text-muted-foreground">{w.label}</span>
          <div
            className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted"
            role="progressbar"
            aria-label={`${w.label} usage`}
            aria-valuenow={Math.round(w.percent)}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <div
              className={`h-full ${w.percent >= 90 ? "bg-destructive" : "bg-primary"}`}
              style={{ width: `${Math.min(w.percent, 100)}%` }}
            />
          </div>
          <span className="w-10 text-right tabular-nums">
            {Math.round(w.percent)}%
          </span>
          {w.resets_at && (
            <span className="text-muted-foreground">
              resets{" "}
              {new Date(w.resets_at).toLocaleString(undefined, {
                weekday: "short",
                hour: "numeric",
                minute: "2-digit",
              })}
            </span>
          )}
        </div>
      ))}
    </div>
  )
}

function RateLimited() {
  return <span className="ml-2 text-xs text-destructive">Rate limited</span>
}

function ManageAccounts({
  accounts,
  usage,
  busy,
  onActivate,
  onRemove,
}: {
  accounts: LlmAccount[]
  usage?: Record<string, UsageWindow[] | undefined>
  busy: boolean
  onActivate: (id: string) => void
  onRemove: (id: string) => void
}) {
  return (
    <ul className="mt-3 space-y-1 border-t pt-3">
      {accounts.map((a) => (
        <li key={a.id} className="flex flex-wrap items-center gap-2 text-sm">
          <span className="flex-1 truncate">
            {a.email || a.id}
            {a.active && (
              <span className="ml-2 text-xs text-green-500">In use</span>
            )}
            {a.rate_limited && <RateLimited />}
            <span className="ml-2 text-xs text-muted-foreground">
              {(usage?.[a.id] ?? [])
                .map((w) => `${w.label} ${Math.round(w.percent)}%`)
                .join(" · ")}
            </span>
          </span>
          {!a.active && (
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => onActivate(a.id)}
            >
              Use
            </Button>
          )}
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => onRemove(a.id)}
          >
            Remove
          </Button>
        </li>
      ))}
    </ul>
  )
}

function LoginPrompt({
  provider,
  login,
  onDone,
  onError,
}: {
  provider: LlmAccountProvider
  login: Login
  onDone: () => void
  onError: (error: string) => void
}) {
  const [pasted, setPasted] = useState("")
  if (login.error) {
    return <p className="text-xs text-destructive">{login.error}</p>
  }
  if (provider === "chatgpt") {
    return (
      <>
        <p>
          Open{" "}
          <a
            href={login.url}
            target="_blank"
            rel="noreferrer"
            className="underline"
          >
            {login.url}
          </a>{" "}
          and enter{" "}
          <code className="rounded bg-muted px-1.5 py-0.5 font-mono">
            {login.code}
          </code>
        </p>
        <p className="flex items-center text-xs text-muted-foreground">
          <Loader2 className="mr-2 h-3 w-3 animate-spin" />
          Waiting for approval…
        </p>
      </>
    )
  }
  return (
    <form
      className="space-y-2"
      onSubmit={async (e) => {
        e.preventDefault()
        try {
          await api.completeClaudeLogin(login.loginId, pasted)
          onDone()
        } catch (err) {
          onError(errorText(err))
        }
      }}
    >
      {REMOTE ? (
        <p className="text-xs text-muted-foreground">
          Approve access on the Claude tab (
          <a
            href={login.url}
            target="_blank"
            rel="noreferrer"
            className="underline"
          >
            reopen
          </a>
          ). It then lands on a localhost page that won't load. Copy that page's
          address and paste it here.
        </p>
      ) : (
        <>
          <p className="flex items-center text-xs text-muted-foreground">
            <Loader2 className="mr-2 h-3 w-3 animate-spin" />
            Finish signing in on the Claude tab (
            <a
              href={login.url}
              target="_blank"
              rel="noreferrer"
              className="mx-1 underline"
            >
              reopen
            </a>
            ).
          </p>
          <p className="text-xs text-muted-foreground">
            If that tab ends on an error page, copy its address (or the code
            shown) and paste it here.
          </p>
        </>
      )}
      <div className="flex gap-2">
        <Input
          aria-label="Claude redirect URL or code"
          placeholder="http://localhost:54545/callback?code=…"
          value={pasted}
          onChange={(e) => setPasted(e.target.value)}
        />
        <Button size="sm" type="submit" disabled={!pasted.trim()}>
          Submit
        </Button>
      </div>
    </form>
  )
}

export function SubscriptionAccounts({
  query,
  onChanged,
}: {
  query: string
  onChanged?: () => void
}) {
  const [accounts, setAccounts] = useState<LlmAccounts | null>(null)
  const [usage, setUsage] = useState<Usage>({})
  const [manage, setManage] = useState<LlmAccountProvider | null>(null)
  const [logins, setLogins] = useState<
    Partial<Record<LlmAccountProvider, Login>>
  >({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  const load = useCallback(() => {
    api.getLlmAccounts().then(
      (a) => {
        setAccounts(a)
        setError("")
      },
      (e) => setError(`Couldn't load accounts: ${errorText(e)}`)
    )
    api
      .getLlmUsage()
      .then((r) => setUsage(r.usage))
      .catch(() => setUsage({}))
  }, [])
  useEffect(load, [load])
  const refresh = useCallback(() => {
    load()
    onChanged?.()
  }, [load, onChanged])

  const setLogin = (key: LlmAccountProvider, login?: Login) =>
    setLogins((prev) => ({ ...prev, [key]: login }))

  // Poll pending sign-ins until the server reports done/error.
  useEffect(() => {
    const pending = Object.entries(logins).filter(([, l]) => l && !l.error) as [
      LlmAccountProvider,
      Login,
    ][]
    if (!pending.length) return
    const id = setInterval(async () => {
      for (const [key, login] of pending) {
        const s = await api.getLlmLogin(login.loginId)
        if (s.status === "done") {
          setLogin(key, undefined)
          refresh()
        } else if (s.status === "error" || s.status === "unknown") {
          setLogin(key, { ...login, error: s.error || "Sign-in failed" })
        }
      }
    }, 2000)
    return () => clearInterval(id)
  }, [logins, refresh])

  const startLogin = async (key: LlmAccountProvider) => {
    // Open the tab synchronously so the browser doesn't block it as a popup.
    const tab =
      key === "anthropic" ? window.open("about:blank", "_blank") : null
    setBusy(true)
    try {
      const res = await api.startLlmLogin(key)
      setLogin(key, { loginId: res.login_id, url: res.url, code: res.code })
      if (tab) tab.location.href = res.url
    } catch (e) {
      tab?.close()
      setLogin(key, { loginId: "", url: "", error: errorText(e) })
    } finally {
      setBusy(false)
    }
  }

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError("")
    try {
      await fn()
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
      refresh()
    }
  }

  const rows = ROWS.filter((r) =>
    `${r.name} ${r.search}`.toLowerCase().includes(query)
  )
  return (
    <>
      {error && <p className="text-xs text-destructive">{error}</p>}
      {rows.map((row) => {
        const list = accounts?.accounts[row.key] ?? []
        const active = list.find((a) => a.active) ?? list[0]
        const login = logins[row.key]
        // A second click would replace the pending flow and stop polling it.
        const loginPending = Boolean(login && !login.error)
        return (
          <div key={row.key} className="rounded-lg border bg-muted/30 p-3">
            <div className="flex items-center gap-3">
              <ProviderIcon id={row.key} label={row.name} />
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium">{row.name}</div>
                <div className="truncate text-xs text-muted-foreground">
                  {active
                    ? active.email || "Signed in"
                    : accounts
                      ? "Not logged in"
                      : "Status unavailable"}
                  {active?.rate_limited && <RateLimited />}
                </div>
              </div>
              {list.length ? (
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() =>
                      setManage(manage === row.key ? null : row.key)
                    }
                  >
                    Manage
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy || loginPending}
                    onClick={() => startLogin(row.key)}
                  >
                    Add account
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy}
                    onClick={() => act(() => api.logOutLlmProvider(row.key))}
                  >
                    Log out
                  </Button>
                </div>
              ) : (
                <Button
                  size="sm"
                  disabled={busy || loginPending}
                  onClick={() => startLogin(row.key)}
                >
                  Log in
                </Button>
              )}
            </div>
            {active && <UsageBars windows={usage[row.key]?.[active.id]} />}
            {manage === row.key && list.length > 0 && (
              <ManageAccounts
                accounts={list}
                usage={usage[row.key]}
                busy={busy}
                onActivate={(id) =>
                  act(() => api.activateLlmAccount(row.key, id))
                }
                onRemove={(id) => act(() => api.removeLlmAccount(row.key, id))}
              />
            )}
            {login && (
              <div className="mt-3 space-y-2 border-t pt-3 text-sm">
                <LoginPrompt
                  key={login.loginId}
                  provider={row.key}
                  login={login}
                  onDone={() => {
                    setLogin(row.key, undefined)
                    refresh()
                  }}
                  onError={(error) => setLogin(row.key, { ...login, error })}
                />
              </div>
            )}
          </div>
        )
      })}
    </>
  )
}
