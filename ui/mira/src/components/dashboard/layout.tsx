import { BookOpen, Brain, Database, GitFork, LayoutDashboard, LogOut, Moon, Package, Settings, ShieldAlert, Sun, Users } from "lucide-react"
import { useEffect, useState } from "react"
import { NavLink, Outlet, useLocation } from "react-router"

import { useTheme } from "@/components/theme-provider"
import { useAuth } from "@/lib/auth"

const API_BASE = import.meta.env.VITE_API_URL || ""

import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
  SidebarTrigger,
} from "@/components/ui/sidebar"

const navItems = [
  { to: "/", icon: LayoutDashboard, label: "Dashboard" },
  { to: "/repos", icon: Database, label: "Repositories" },
  { to: "/packages", icon: Package, label: "Packages" },
  { to: "/vulnerabilities", icon: ShieldAlert, label: "Vulnerabilities" },
  { to: "/relationships", icon: GitFork, label: "Relationships" },
  { to: "/rules", icon: BookOpen, label: "Rules" },
  { to: "/learnings", icon: Brain, label: "Learnings" },
  { to: "/users", icon: Users, label: "Users", adminOnly: true },
  { to: "/settings", icon: Settings, label: "Settings", adminOnly: true },
]

const PAGE_LABELS: Record<string, string> = {
  repos: "Repositories",
  packages: "Packages",
  vulnerabilities: "Vulnerabilities",
  relationships: "Relationships",
  rules: "Rules",
  learnings: "Learnings",
  settings: "Settings",
  users: "Users",
}

function AppBreadcrumb() {
  const location = useLocation()
  const parts = location.pathname.split("/").filter(Boolean)

  if (parts.length === 0) {
    return (
      <Breadcrumb>
        <BreadcrumbList>
          <BreadcrumbItem>
            <BreadcrumbPage>Dashboard</BreadcrumbPage>
          </BreadcrumbItem>
        </BreadcrumbList>
      </Breadcrumb>
    )
  }

  const label = (part: string) =>
    PAGE_LABELS[part] || decodeURIComponent(part)

  // /repos/{owner}/{repo} doesn't have a real /repos/{owner} route, so the
  // owner segment links back to the repos list with that owner pre-filtered.
  const hrefFor = (i: number) => {
    if (parts[0] === "repos" && i === 1 && parts.length >= 3) {
      return `/repos?owner=${encodeURIComponent(parts[1])}`
    }
    return `/${parts.slice(0, i + 1).join("/")}`
  }

  return (
    <Breadcrumb>
      <BreadcrumbList>
        {parts.map((part, i) => (
          <span key={i} className="contents">
            {i > 0 && <BreadcrumbSeparator />}
            <BreadcrumbItem>
              {i === parts.length - 1 ? (
                <BreadcrumbPage>{label(part)}</BreadcrumbPage>
              ) : (
                <BreadcrumbLink href={hrefFor(i)}>{label(part)}</BreadcrumbLink>
              )}
            </BreadcrumbItem>
          </span>
        ))}
      </BreadcrumbList>
    </Breadcrumb>
  )
}

export function DashboardLayout() {
  const { user } = useAuth()

  const visibleNav = navItems.filter(
    (item) => !("adminOnly" in item && item.adminOnly) || user?.is_admin,
  )

  // Fetch the running Mira version once on mount and render it next to the
  // logo. Falls back silently if the call fails (e.g. older backend without
  // the endpoint) — the chrome stays clean instead of showing "unknown".
  const [version, setVersion] = useState<string | null>(null)
  useEffect(() => {
    fetch(`${API_BASE}/api/version`, { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data?.version) setVersion(data.version)
      })
      .catch(() => {})
  }, [])

  return (
    <SidebarProvider>
      <Sidebar collapsible="icon">
        <SidebarHeader>
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton size="lg" asChild>
                <a href="/">
                  <div className="flex aspect-square size-8 items-center justify-center">
                    <img src="/logo.png" alt="Mira" className="hidden size-7 dark:block" />
                    <img src="/logo-light.png" alt="Mira" className="size-7 dark:hidden" />
                  </div>
                  <div className="flex flex-col leading-tight">
                    <span className="text-sm font-semibold">Mira</span>
                    {version && (
                      <span className="text-[10px] text-muted-foreground tabular-nums">
                        v{version}
                      </span>
                    )}
                  </div>
                </a>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarHeader>

        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupLabel>Navigation</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {visibleNav.map((item) => (
                  // Active state is driven entirely by NavLink: it sets
                  // aria-current="page" on the active link (with the same
                  // prefix matching `end` controls), so styling off
                  // aria-current keeps a single source of truth instead of
                  // recomputing the match here.
                  <SidebarMenuItem key={item.to}>
                    <SidebarMenuButton
                      asChild
                      className="aria-[current=page]:bg-sidebar-accent aria-[current=page]:font-semibold aria-[current=page]:text-sidebar-accent-foreground"
                    >
                      <NavLink to={item.to} end={item.to === "/"}>
                        <item.icon />
                        <span>{item.label}</span>
                      </NavLink>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>

        <SidebarFooter>
          <SidebarMenu>
            <SidebarMenuItem>
              <ThemeToggle />
            </SidebarMenuItem>
            <UserMenu />
          </SidebarMenu>
        </SidebarFooter>

        <SidebarRail />
      </Sidebar>

      <SidebarInset>
        <header className="flex h-12 shrink-0 items-center gap-2 border-b px-4">
          <SidebarTrigger className="-ml-1" />
          <AppBreadcrumb />
        </header>
        <main className="flex-1 overflow-auto">
          <Outlet />
        </main>
      </SidebarInset>
    </SidebarProvider>
  )
}

function UserMenu() {
  const { user, logout } = useAuth()
  if (!user) return null

  return (
    <SidebarMenuItem>
      <SidebarMenuButton size="sm" onClick={logout}>
        <LogOut className="h-4 w-4" />
        <span className="text-xs">{user.username}</span>
      </SidebarMenuButton>
    </SidebarMenuItem>
  )
}

function ThemeToggle() {
  const { theme, setTheme } = useTheme()

  const isDark =
    theme === "dark" ||
    (theme === "system" &&
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches)

  const next = () => {
    const newTheme = isDark ? "light" : "dark"
    setTheme(newTheme)
    // Save to user profile in DB
    const API_BASE = import.meta.env.VITE_API_URL || ""
    fetch(`${API_BASE}/api/auth/theme`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ theme: newTheme }),
    }).catch(() => {})
  }

  return (
    <SidebarMenuButton size="sm" onClick={next}>
      {isDark ? (
        <Moon className="h-4 w-4" />
      ) : (
        <Sun className="h-4 w-4" />
      )}
      <span className="text-xs">{isDark ? "Dark" : "Light"}</span>
    </SidebarMenuButton>
  )
}
