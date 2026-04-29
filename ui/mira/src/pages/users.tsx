import { Plus, Trash2 } from "lucide-react"
import { useState } from "react"

import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { api } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { useAsync } from "@/lib/hooks"

export function UsersPage() {
  const { user: currentUser } = useAuth()
  const [refreshKey, setRefreshKey] = useState(0)
  const { data: users } = useAsync(() => api.listUsers(), [refreshKey])
  const [showCreate, setShowCreate] = useState(false)
  const [newUser, setNewUser] = useState({ username: "", password: "", is_admin: false })
  const [error, setError] = useState<string | null>(null)

  if (!currentUser?.is_admin) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        Admin access required.
      </div>
    )
  }

  const handleCreate = async () => {
    setError(null)
    try {
      await api.createUser(newUser.username, newUser.password, newUser.is_admin)
      setNewUser({ username: "", password: "", is_admin: false })
      setShowCreate(false)
      setRefreshKey((k) => k + 1)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to create user")
    }
  }

  const handleDelete = async (id: number) => {
    await api.deleteUser(id)
    setRefreshKey((k) => k + 1)
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Users</h1>
        <p className="text-sm text-muted-foreground">
          Manage users and access
        </p>
      </div>

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Users</CardTitle>
              <CardDescription>
                {users?.length ?? 0} users with dashboard access
              </CardDescription>
            </div>
            <Button size="sm" onClick={() => setShowCreate(!showCreate)}>
              <Plus className="mr-1 h-3 w-3" /> Add User
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {showCreate && (
            <div className="mb-6 space-y-3 rounded-lg border p-4">
              <div className="grid grid-cols-2 gap-3">
                <Input
                  placeholder="Username"
                  value={newUser.username}
                  onChange={(e) =>
                    setNewUser({ ...newUser, username: e.target.value })
                  }
                />
                <Input
                  type="password"
                  placeholder="Password"
                  value={newUser.password}
                  onChange={(e) =>
                    setNewUser({ ...newUser, password: e.target.value })
                  }
                />
              </div>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={newUser.is_admin}
                  onChange={(e) =>
                    setNewUser({ ...newUser, is_admin: e.target.checked })
                  }
                  className="rounded"
                />
                Admin privileges
              </label>
              {error && (
                <p className="text-sm text-destructive">{error}</p>
              )}
              <div className="flex gap-2">
                <Button
                  size="sm"
                  onClick={handleCreate}
                  disabled={!newUser.username || !newUser.password}
                >
                  Create
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setShowCreate(false)}
                >
                  Cancel
                </Button>
              </div>
            </div>
          )}

          {users && users.length > 0 ? (
            <div className="space-y-4">
              {users.map((u) => (
                <div key={u.id} className="flex items-center">
                  <Avatar className="h-9 w-9">
                    <AvatarFallback>
                      {u.username.slice(0, 2).toUpperCase()}
                    </AvatarFallback>
                  </Avatar>
                  <div className="ml-4 space-y-1">
                    <p className="text-sm font-medium leading-none">
                      {u.username}
                    </p>
                    <p className="text-sm text-muted-foreground">
                      {u.is_admin ? "Admin" : "User"}
                    </p>
                  </div>
                  <div className="ml-auto flex items-center gap-2">
                    {u.is_admin && (
                      <Badge variant="secondary">Admin</Badge>
                    )}
                    {u.id !== currentUser.id && (
                      <Button
                        size="icon"
                        variant="ghost"
                        className="h-8 w-8 text-muted-foreground hover:text-destructive"
                        onClick={() => handleDelete(u.id)}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">No users found.</p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
