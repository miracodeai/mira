import { KeyRound, Plus, Trash2, Users as UsersIcon } from "lucide-react"
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
import { UserAvatar } from "@/components/ui/user-avatar"
import { api } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { useAsync, useDocumentTitle } from "@/lib/hooks"

// Epoch seconds → "Just now" / "5m ago" / "3h ago" / "2d ago" / a date.
// 0 (never logged in) → "Never".
function lastSeen(ts: number): string {
  if (!ts) return "Never"
  const diff = Date.now() / 1000 - ts
  if (diff < 60) return "Just now"
  const mins = Math.floor(diff / 60)
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  if (days < 30) return `${days}d ago`
  return new Date(ts * 1000).toLocaleDateString()
}

export function UsersPage() {
  useDocumentTitle("Users")
  const { user: currentUser } = useAuth()
  const navigate = useNavigate()
  const [refreshKey, setRefreshKey] = useState(0)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const { data: users, loading } = useAsync(() => api.listUsers(), [refreshKey])

  if (!currentUser?.is_admin) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        Admin access required.
      </div>
    )
  }

  const remove = async (id: number) => {
    setDeleteError(null)
    try {
      await api.deleteUser(id)
      setRefreshKey((k) => k + 1)
    } catch (e) {
      setDeleteError(
        `Failed to delete user: ${e instanceof Error ? e.message : String(e)}`
      )
    }
  }

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Users</h1>
          <p className="text-sm text-muted-foreground">
            Manage who can access the Mira dashboard.
          </p>
        </div>
        {users && users.length > 0 && (
          <Button size="sm" onClick={() => navigate("/users/new")}>
            <Plus className="mr-1 h-4 w-4" /> Add user
          </Button>
        )}
      </div>

      {loading ? (
        <div className="text-sm text-muted-foreground">Loading…</div>
      ) : !users || users.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center gap-4 py-16 text-center">
            <div className="flex size-12 items-center justify-center rounded-full bg-muted">
              <UsersIcon className="size-6 text-muted-foreground" />
            </div>
            <div className="space-y-1">
              <p className="text-sm font-medium">No users yet</p>
              <p className="max-w-sm text-sm text-muted-foreground">
                Add a user to give someone access to the dashboard.
              </p>
            </div>
            <Button size="sm" onClick={() => navigate("/users/new")}>
              <Plus className="mr-1 h-4 w-4" /> Add user
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card className="overflow-hidden py-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>User</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Last seen</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {users.map((u) => (
                <TableRow key={u.id}>
                  <TableCell>
                    <div className="flex items-center gap-3">
                      <UserAvatar seed={u.username} className="h-7 w-7" />
                      <span className="font-medium">{u.username}</span>
                      {u.id === currentUser.id && (
                        <span className="text-xs text-muted-foreground">
                          (you)
                        </span>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    {u.is_admin ? (
                      <Badge variant="secondary" className="ring-1 ring-border">
                        Admin
                      </Badge>
                    ) : (
                      <Badge
                        variant="secondary"
                        className="bg-transparent ring-1 ring-border"
                      >
                        User
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {lastSeen(u.last_login_at)}
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex items-center justify-end gap-1">
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            onClick={() => navigate(`/users/${u.id}/password`)}
                          >
                            <KeyRound className="h-3.5 w-3.5" />
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent>Reset password</TooltipContent>
                      </Tooltip>
                      {u.id !== currentUser.id && (
                        <ConfirmButton
                          variant="ghost"
                          size="icon-sm"
                          tooltip="Delete"
                          dialogTitle="Delete user?"
                          dialogDescription={`"${u.username}" will lose dashboard access. This can't be undone.`}
                          confirmLabel="Delete"
                          destructive
                          onConfirm={() => remove(u.id)}
                        >
                          <Trash2 className="h-3.5 w-3.5 text-destructive" />
                        </ConfirmButton>
                      )}
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
