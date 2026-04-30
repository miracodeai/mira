import { createContext, useContext, useEffect, useState, type ReactNode } from "react"

const API_BASE = import.meta.env.VITE_API_URL || ""

export interface AuthUser {
  id: number
  username: string
  is_admin: boolean
  theme: "dark" | "light"
}

interface AuthContextType {
  user: AuthUser | null
  loading: boolean
  login: (username: string, password: string) => Promise<string | null>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthContextType | null>(null)

function applyTheme(theme: string) {
  // Sync with the ThemeProvider by setting localStorage + class
  localStorage.setItem("theme", theme)
  const root = document.documentElement
  root.classList.remove("light", "dark")
  root.classList.add(theme)
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch(`${API_BASE}/api/auth/me`, { credentials: "include" })
      .then((res) => {
        if (res.ok) return res.json()
        return null
      })
      .then((data) => {
        if (data && data.id) {
          setUser(data)
          applyTheme(data.theme)
        }
      })
      .finally(() => setLoading(false))
  }, [])

  const login = async (username: string, password: string): Promise<string | null> => {
    const res = await fetch(`${API_BASE}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ username, password }),
    })
    const data = await res.json()
    if (res.ok && data.user) {
      setUser(data.user)
      applyTheme(data.user.theme)
      return null
    }
    return data.error || "Login failed"
  }

  const logout = async () => {
    await fetch(`${API_BASE}/api/auth/logout`, {
      method: "POST",
      credentials: "include",
    })
    setUser(null)
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error("useAuth must be used within AuthProvider")
  return ctx
}
