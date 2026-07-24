import { ChevronLeft } from "lucide-react"
import { useNavigate } from "react-router"

import { PasswordForm } from "@/components/password-form"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { api } from "@/lib/api"
import { useAuth } from "@/lib/auth"

export function ChangePasswordPage() {
  const { user } = useAuth()
  const navigate = useNavigate()

  if (!user) {
    return (
      <div className="p-6 text-sm text-muted-foreground">Not signed in.</div>
    )
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6 p-6">
      <button
        onClick={() => navigate(-1)}
        className="flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ChevronLeft className="h-4 w-4" /> Back
      </button>

      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          Change password
        </h1>
        <p className="text-sm text-muted-foreground">
          Update the password for your account ({user.username}).
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Password</CardTitle>
        </CardHeader>
        <CardContent>
          <PasswordForm
            requireCurrent
            submitLabel="Update password"
            onSubmit={async (current, next) => {
              await api.changePassword(current, next)
              navigate(-1)
            }}
            onCancel={() => navigate(-1)}
          />
        </CardContent>
      </Card>
    </div>
  )
}
