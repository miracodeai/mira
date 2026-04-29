import { useEffect, useState } from "react"
import { BrowserRouter, Navigate, Route, Routes } from "react-router"

import { DashboardLayout } from "@/components/dashboard/layout"
import { SetupModal } from "@/components/dashboard/setup-modal"
import { UninstallModal } from "@/components/dashboard/uninstall-modal"
import { api } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { DashboardPage } from "@/pages/dashboard"
import { LearnedRulesPage } from "@/pages/learned-rules"
import { LoginPage } from "@/pages/login"
import { PackagesPage } from "@/pages/packages"
import { RepoDetailPage } from "@/pages/repo-detail"
import { RelationshipsPage } from "@/pages/relationships"
import { ReposPage } from "@/pages/repos"
import { SettingsPage } from "@/pages/settings"
import { SetupPage } from "@/pages/setup"
import { RulesPage } from "@/pages/rules"
import { UsersPage } from "@/pages/users"

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8100"

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth()

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p className="text-sm text-muted-foreground">Loading...</p>
      </div>
    )
  }

  if (!user) {
    return <Navigate to="/login" replace />
  }

  return <>{children}</>
}

function SetupGuard({ children }: { children: React.ReactNode }) {
  const { user } = useAuth()
  const [checking, setChecking] = useState(true)
  const [needsSetup, setNeedsSetup] = useState(false)

  useEffect(() => {
    if (!user?.is_admin) {
      setChecking(false)
      return
    }
    api
      .getSetupStatus()
      .then((s) => setNeedsSetup(!s.setup_complete))
      .catch(() => {})
      .finally(() => setChecking(false))
  }, [user])

  if (checking) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p className="text-sm text-muted-foreground">Loading...</p>
      </div>
    )
  }

  if (needsSetup) {
    return <Navigate to="/setup" replace />
  }

  return <>{children}</>
}

function AppShell() {
  const { user } = useAuth()
  const [showInstallPopup, setShowInstallPopup] = useState(false)
  const [pendingUninstall, setPendingUninstall] = useState<{
    installation_id: number
    owner: string
  } | null>(null)

  // Check initial state + subscribe to SSE
  useEffect(() => {
    if (!user?.is_admin) return

    let eventSource: EventSource | null = null
    let cancelled = false

    const checkInitial = async () => {
      try {
        // Sync DB with GitHub installations (catches missed webhooks)
        await api.syncRepos().catch(() => {})

        if (cancelled) return

        // Show install popup if there are pending repos
        const repos = await api.listRepos()
        if (!cancelled && repos.some((r) => r.status === "pending")) {
          setShowInstallPopup(true)
        }

        // Show uninstall popup if any pending uninstalls
        const uninstalls = await api.listPendingUninstalls()
        if (!cancelled && uninstalls.length > 0) {
          setPendingUninstall(uninstalls[0])
        }
      } catch {
        // ignore
      }
    }

    checkInitial()

    // Subscribe to SSE for real-time events
    eventSource = new EventSource(`${API_BASE}/api/events`, {
      withCredentials: true,
    })

    eventSource.addEventListener("install_created", () => {
      setShowInstallPopup(true)
    })

    eventSource.addEventListener("repos_added", () => {
      setShowInstallPopup(true)
    })

    eventSource.addEventListener("uninstall_pending", (e) => {
      try {
        const data = JSON.parse((e as MessageEvent).data)
        setPendingUninstall({
          installation_id: data.installation_id,
          owner: data.owner,
        })
      } catch {
        // ignore
      }
    })

    return () => {
      cancelled = true
      eventSource?.close()
    }
  }, [user])

  return (
    <>
      {/* Uninstall popup takes priority */}
      {pendingUninstall && (
        <UninstallModal
          installationId={pendingUninstall.installation_id}
          owner={pendingUninstall.owner}
          onDone={() => setPendingUninstall(null)}
        />
      )}
      <SetupModal
        open={showInstallPopup && !pendingUninstall}
        onComplete={() => setShowInstallPopup(false)}
      />
      <DashboardLayout />
    </>
  )
}

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/setup"
          element={
            <ProtectedRoute>
              <SetupPage />
            </ProtectedRoute>
          }
        />
        <Route
          element={
            <ProtectedRoute>
              <SetupGuard>
                <AppShell />
              </SetupGuard>
            </ProtectedRoute>
          }
        >
          <Route index element={<DashboardPage />} />
          <Route path="repos" element={<ReposPage />} />
          <Route path="repos/:owner/:repo" element={<RepoDetailPage />} />
          <Route path="packages" element={<PackagesPage />} />
          <Route path="relationships" element={<RelationshipsPage />} />
          <Route path="rules" element={<RulesPage />} />
          <Route path="learned-rules" element={<LearnedRulesPage />} />
          <Route path="users" element={<UsersPage />} />
          <Route path="settings" element={<SettingsPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

export default App
