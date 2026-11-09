import { useEffect, useState } from "react"
import { Navigate, useSearchParams } from "react-router"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { useAuth } from "@/lib/auth"
import { useDocumentTitle } from "@/lib/hooks"

const API_BASE = import.meta.env.VITE_API_URL || ""

const SSO_ERRORS: Record<string, string> = {
  sso_denied:
    "Your account isn't allowed to use Mira. Ask an admin for access.",
  sso_conflict:
    "A Mira account with your username already exists. Ask an admin to remove it, then sign in again.",
  sso_expired: "Sign-in took too long or was interrupted. Please try again.",
  sso_failed: "Single sign-on didn't work. Please try again, or ask an admin.",
}

interface SsoProvider {
  label: string
  login_url: string
}

export function LoginPage() {
  useDocumentTitle("Sign in")
  const { user, loading, login } = useAuth()
  const [searchParams] = useSearchParams()
  const ssoError = searchParams.get("error")
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(
    ssoError ? (SSO_ERRORS[ssoError] ?? SSO_ERRORS.sso_failed) : null
  )
  const [submitting, setSubmitting] = useState(false)
  const [sso, setSso] = useState<SsoProvider | null>(null)
  const [passwordLogin, setPasswordLogin] = useState(true)

  useEffect(() => {
    fetch(`${API_BASE}/api/auth/providers`, { credentials: "include" })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        setSso(data?.sso ?? null)
        setPasswordLogin(data?.password !== false)
      })
      .catch(() => setSso(null))
  }, [])

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p className="text-sm text-muted-foreground">Loading...</p>
      </div>
    )
  }

  if (user) {
    return <Navigate to="/" replace />
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    const err = await login(username, password)
    if (err) setError(err)
    setSubmitting(false)
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="text-center">
          <img src="/logo.png" alt="Mira" className="mx-auto mb-2 hidden h-10 w-10 dark:block" />
          <img src="/logo-light.png" alt="Mira" className="mx-auto mb-2 h-10 w-10 dark:hidden" />
          <CardTitle className="text-xl">Sign in to Mira</CardTitle>
          <CardDescription>
            {!sso
              ? "Enter your credentials to continue"
              : passwordLogin
                ? "Use your company account, or a Mira password"
                : "Use your company account to continue"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {sso && (
            <>
              <Button asChild className="w-full">
                <a href={`${API_BASE}${sso.login_url}`}>
                  Sign in with {sso.label}
                </a>
              </Button>
              {passwordLogin && (
                <div className="my-4 flex items-center gap-3 text-xs text-muted-foreground">
                  <span className="h-px flex-1 bg-border" />
                  or
                  <span className="h-px flex-1 bg-border" />
                </div>
              )}
              {!passwordLogin && error && (
                <p className="mt-4 text-sm text-destructive">{error}</p>
              )}
            </>
          )}
          {passwordLogin && (
            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="space-y-2">
                <Input
                  placeholder="Username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  autoFocus
                />
              </div>
              <div className="space-y-2">
                <Input
                  type="password"
                  placeholder="Password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </div>
              {error && (
                <p className="text-sm text-destructive">{error}</p>
              )}
              <Button
                type="submit"
                variant={sso ? "outline" : "default"}
                className="w-full"
                disabled={submitting || !username || !password}
              >
                {submitting ? "Signing in..." : "Sign in"}
              </Button>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
