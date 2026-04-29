import { BookOpen, Brain, Database, GitFork, LayoutDashboard, LogOut, Moon, Package, Settings, Sun, Users } from "lucide-react"
import { NavLink, Outlet, useLocation } from "react-router"

import { useTheme } from "@/components/theme-provider"
import { useAuth } from "@/lib/auth"

import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb"
import { Separator } from "@/components/ui/separator"
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
  { to: "/relationships", icon: GitFork, label: "Relationships" },
  { to: "/rules", icon: BookOpen, label: "Rules" },
  { to: "/learned-rules", icon: Brain, label: "Learned" },
  { to: "/users", icon: Users, label: "Users", adminOnly: true },
  { to: "/settings", icon: Settings, label: "Settings", adminOnly: true },
]

const PAGE_LABELS: Record<string, string> = {
  repos: "Repositories",
  packages: "Packages",
  relationships: "Relationships",
  rules: "Rules",
  "learned-rules": "Learned",
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
                <BreadcrumbLink href={`/${parts.slice(0, i + 1).join("/")}`}>
                  {label(part)}
                </BreadcrumbLink>
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

  return (
    <SidebarProvider>
      <Sidebar collapsible="icon">
        <SidebarHeader>
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton size="lg" asChild>
                <a href="/">
                  <div className="flex aspect-square size-8 items-center justify-center">
                    <img src="/logo.png" alt="Mira" className="size-7" />
                  </div>
                  <span className="text-sm font-semibold">Mira</span>
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
                  <SidebarMenuItem key={item.to}>
                    <SidebarMenuButton asChild>
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
          <Separator orientation="vertical" className="mr-2 h-4" />
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
    const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8100"
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
