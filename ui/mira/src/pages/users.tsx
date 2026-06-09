import { Plus, Trash2, Users as UsersIcon } from "lucide-react"
import { useState } from "react"
import { useNavigate } from "react-router"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { ConfirmButton } from "@/components/ui/confirm-button"
import { UserAvatar } from "@/components/ui/user-avatar"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { api } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { useAsync } from "@/lib/hooks"

export function UsersPage() {
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
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {users.map((u) => (
                <TableRow key={u.id}>
                  <TableCell>
                    <div className="flex items-center gap-3">
                      <UserAvatar seed={u.username} className="h-8 w-8" />
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
                      <Badge variant="secondary">Admin</Badge>
                    ) : (
                      <Badge variant="outline">User</Badge>
                    )}
                  </TableCell>
                  <TableCell className="text-right">
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
