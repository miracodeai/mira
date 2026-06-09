import { deleteJson, fetchJson, postJson } from "./http"

// User management (admin) and password change/reset.
export const usersApi = {
  listUsers: () =>
    fetchJson<
      {
        id: number
        username: string
        is_admin: boolean
        last_login_at: number
      }[]
    >("/api/auth/users"),

  createUser: (username: string, password: string, is_admin: boolean) =>
    postJson<{ id: number; username: string; is_admin: boolean }>(
      "/api/auth/users",
      { username, password, is_admin }
    ),

  deleteUser: (id: number) => deleteJson(`/api/auth/users/${id}`),

  // Change the logged-in user's own password (verifies the current one).
  changePassword: (current_password: string, new_password: string) =>
    postJson<{ ok: boolean }>("/api/auth/change-password", {
      current_password,
      new_password,
    }),

  // Admin: set a new password for any user (no current password needed).
  resetUserPassword: (id: number, new_password: string) =>
    postJson<{ ok: boolean }>(`/api/auth/users/${id}/password`, {
      new_password,
    }),
}
