import { ChevronLeft, Loader2 } from "lucide-react"
import { useState } from "react"
import { useNavigate } from "react-router"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { api } from "@/lib/api"
import { useAuth } from "@/lib/auth"

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

export function UserFormPage() {
  const { user: currentUser } = useAuth()
  const navigate = useNavigate()

  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [isAdmin, setIsAdmin] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (!currentUser?.is_admin) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        Admin access required.
      </div>
    )
  }

  const save = async () => {
    setError(null)
    setSaving(true)
    try {
      await api.createUser(username.trim(), password, isAdmin)
      navigate("/users")
    } catch (e) {
      setError(parseDetail(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6 p-6">
      <button
        onClick={() => navigate("/users")}
        className="flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ChevronLeft className="h-4 w-4" /> Users
      </button>

      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Add user</h1>
        <p className="text-sm text-muted-foreground">
          Create a new account that can sign in to the Mira dashboard.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Details</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <label className="text-sm font-medium" htmlFor="u-name">
              Username
            </label>
            <Input
              id="u-name"
              placeholder="e.g. alex"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <label className="text-sm font-medium" htmlFor="u-pass">
              Password
            </label>
            <Input
              id="u-pass"
              type="password"
              placeholder="Set an initial password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <label className="flex w-fit items-center gap-2 text-sm">
            <Checkbox
              checked={isAdmin}
              onCheckedChange={(v) => setIsAdmin(Boolean(v))}
            />
            Admin privileges
          </label>
        </CardContent>
      </Card>

      {error && <p className="text-sm break-words text-destructive">{error}</p>}

      <div className="flex gap-2">
        <Button
          onClick={save}
          disabled={saving || !username.trim() || !password}
        >
          {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          Create user
        </Button>
        <Button variant="ghost" onClick={() => navigate("/users")}>
          Cancel
        </Button>
      </div>
    </div>
  )
}
