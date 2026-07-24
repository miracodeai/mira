import { ChevronLeft } from "lucide-react"
import { useNavigate, useParams } from "react-router"

import { PasswordForm } from "@/components/password-form"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { api } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { useAsync } from "@/lib/hooks"

export function ResetUserPasswordPage() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const { id } = useParams()
  const { data: users } = useAsync(() => api.listUsers(), [])

  if (!user?.is_admin) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        Admin access required.
      </div>
    )
  }

  const target = users?.find((u) => String(u.id) === id)

  return (
    <div className="mx-auto max-w-2xl space-y-6 p-6">
      <button
        onClick={() => navigate("/users")}
        className="flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ChevronLeft className="h-4 w-4" /> Users
      </button>

      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          Reset password
        </h1>
        <p className="text-sm text-muted-foreground">
          Set a new password{target ? ` for ${target.username}` : ""}.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Password</CardTitle>
        </CardHeader>
        <CardContent>
          <PasswordForm
            submitLabel="Reset password"
            onSubmit={async (_current, next) => {
              await api.resetUserPassword(Number(id), next)
              navigate("/users")
            }}
            onCancel={() => navigate("/users")}
          />
        </CardContent>
      </Card>
    </div>
  )
}
