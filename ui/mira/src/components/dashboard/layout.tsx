import {
  Activity,
  BookOpen,
  Brain,
  ChevronRight,
  ChevronsUpDown,
  Database,
  GitFork,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Moon,
  Package,
  Settings,
  ShieldAlert,
  Sun,
  Users,
} from "lucide-react"
import { useEffect, useState } from "react"
import { NavLink, Outlet, useLocation, useNavigate } from "react-router"

import { useTheme } from "@/components/theme-provider"
import { api } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { useAsync } from "@/lib/hooks"

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
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
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
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
  SidebarProvider,
  SidebarRail,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import { UserAvatar } from "@/components/ui/user-avatar"

const navItems = [
  { to: "/", icon: LayoutDashboard, label: "Dashboard" },
  { to: "/activity", icon: Activity, label: "Activity" },
  { to: "/repos", icon: Database, label: "Repositories" },
  { to: "/packages", icon: Package, label: "Packages" },
  { to: "/vulnerabilities", icon: ShieldAlert, label: "Vulnerabilities" },
  { to: "/relationships", icon: GitFork, label: "Relationships" },
  { to: "/rules", icon: BookOpen, label: "Rules" },
  { to: "/learnings", icon: Brain, label: "Learnings" },
  { to: "/users", icon: Users, label: "Users", adminOnly: true },
]

// Settings is rendered as a collapsible group (admin-only) with these
// children rather than a flat nav item.
const settingsSubItems = [
  { to: "/settings/models", label: "Models" },
  { to: "/settings/review", label: "Review" },
  { to: "/settings/webhooks", label: "Webhooks" },
]

const PAGE_LABELS: Record<string, string> = {
  activity: "Activity",
  repos: "Repositories",
  packages: "Packages",
  vulnerabilities: "Vulnerabilities",
  relationships: "Relationships",
  rules: "Rules",
  learnings: "Learnings",
  settings: "Settings",
  users: "Users",
  new: "New",
  edit: "Edit",
  account: "Account",
  password: "Password",
  models: "Models",
  review: "Review",
  webhooks: "Webhooks",
}

function AppBreadcrumb() {
  const location = useLocation()
  const parts = location.pathname.split("/").filter(Boolean)

  // The /settings/webhooks/{id} segment is an opaque id — resolve it to the
  // webhook's name so the breadcrumb reads "Webhooks / #eng-reviews", not a
  // raw uuid. Only fetches on that route ({id} is null elsewhere).
  const webhookId =
    parts[0] === "settings" &&
    parts[1] === "webhooks" &&
    parts.length === 3 &&
    parts[2] !== "new"
      ? parts[2]
      : null
  const { data: webhookData } = useAsync(
    () => (webhookId ? api.getWebhook(webhookId) : Promise.resolve(null)),
    [webhookId]
  )
  const webhookName = webhookData?.name ?? null

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

  const label = (part: string, i: number) => {
    if (parts[0] === "settings" && parts[1] === "webhooks" && i === 2) {
      if (part === "new") return "New"
      return webhookName || "Webhook"
    }
    return PAGE_LABELS[part] || decodeURIComponent(part)
  }

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
                <BreadcrumbPage>{label(part, i)}</BreadcrumbPage>
              ) : (
                <BreadcrumbLink href={hrefFor(i)}>
                  {label(part, i)}
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
  const location = useLocation()
  const onSettings = location.pathname.startsWith("/settings")

  const visibleNav = navItems.filter(
    (item) => !("adminOnly" in item && item.adminOnly) || user?.is_admin
  )

  // Active styling keys off aria-current, which NavLink sets on the active
  // link — single source of truth, no parallel route-matching here.
  const navActive =
    "aria-[current=page]:bg-sidebar-accent aria-[current=page]:font-semibold aria-[current=page]:text-sidebar-accent-foreground"

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
                    <img
                      src="/logo.png"
                      alt="Mira"
                      className="hidden size-7 dark:block"
                    />
                    <img
                      src="/logo-light.png"
                      alt="Mira"
                      className="size-7 dark:hidden"
                    />
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
                      tooltip={item.label}
                      className={navActive}
                    >
                      <NavLink to={item.to} end={item.to === "/"}>
                        <item.icon />
                        <span>{item.label}</span>
                      </NavLink>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}

                {user?.is_admin && (
                  <Collapsible
                    asChild
                    defaultOpen={onSettings}
                    className="group/collapsible"
                  >
                    <SidebarMenuItem>
                      <CollapsibleTrigger asChild>
                        <SidebarMenuButton>
                          <Settings />
                          <span>Settings</span>
                          <ChevronRight className="ml-auto transition-transform group-data-[state=open]/collapsible:rotate-90" />
                        </SidebarMenuButton>
                      </CollapsibleTrigger>
                      <CollapsibleContent>
                        <SidebarMenuSub>
                          {settingsSubItems.map((sub) => (
                            <SidebarMenuSubItem key={sub.to}>
                              <SidebarMenuSubButton
                                asChild
                                className={navActive}
                              >
                                <NavLink to={sub.to}>
                                  <span>{sub.label}</span>
                                </NavLink>
                              </SidebarMenuSubButton>
                            </SidebarMenuSubItem>
                          ))}
                        </SidebarMenuSub>
                      </CollapsibleContent>
                    </SidebarMenuItem>
                  </Collapsible>
                )}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>

        <SidebarFooter>
          <SidebarMenu>
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
  const { theme, setTheme } = useTheme()
  const navigate = useNavigate()
  if (!user) return null

  const isDark =
    theme === "dark" ||
    (theme === "system" &&
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches)

  const toggleTheme = () => {
    const newTheme = isDark ? "light" : "dark"
    setTheme(newTheme)
    // Persist to the user profile in the DB.
    fetch(`${API_BASE}/api/auth/theme`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ theme: newTheme }),
    }).catch(() => {})
  }

  return (
    <SidebarMenuItem>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <SidebarMenuButton size="lg">
            <UserAvatar seed={user.username} className="size-7" />
            <span className="text-xs font-medium">{user.username}</span>
            <ChevronsUpDown className="ml-auto size-4" />
          </SidebarMenuButton>
        </DropdownMenuTrigger>
        <DropdownMenuContent side="top" align="start" className="min-w-40">
          <DropdownMenuItem
            onClick={toggleTheme}
            className="gap-2 py-1 text-xs [&_svg]:size-3.5"
          >
            {isDark ? <Sun /> : <Moon />}
            {isDark ? "Light mode" : "Dark mode"}
          </DropdownMenuItem>
          <DropdownMenuItem
            onClick={() => navigate("/account/password")}
            className="gap-2 py-1 text-xs [&_svg]:size-3.5"
          >
            <KeyRound /> Change password
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            variant="destructive"
            onClick={logout}
            className="gap-2 py-1 text-xs [&_svg]:size-3.5"
          >
            <LogOut /> Log out
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </SidebarMenuItem>
  )
}
