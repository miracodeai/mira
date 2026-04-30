import dagre from "dagre"
import {
  ReactFlow,
  Background,
  Controls,
  type Node,
  type Edge,
  Handle,
  Position,
  useNodesState,
  useEdgesState,
  MarkerType,
  type NodeProps,
  ConnectionLineType,
} from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import { Database, Package, Puzzle } from "lucide-react"
import { useEffect, useMemo, useState } from "react"
import { useNavigate } from "react-router"

import { Input } from "@/components/ui/input"
import type { RelationshipsResponse } from "@/lib/api"

// ── Custom node ──

type RepoNodeData = {
  label: string
  short: string
  group?: string
  isUtility?: boolean
  colorIdx?: number
}

const GROUP_BORDER_COLORS = [
  "border-blue-500",
  "border-emerald-500",
  "border-violet-500",
  "border-amber-500",
  "border-rose-500",
]

const GROUP_BG_COLORS = [
  "bg-blue-500/8",
  "bg-emerald-500/8",
  "bg-violet-500/8",
  "bg-amber-500/8",
  "bg-rose-500/8",
]

const GROUP_TEXT_COLORS = [
  "text-blue-400",
  "text-emerald-400",
  "text-violet-400",
  "text-amber-400",
  "text-rose-400",
]

const GROUP_EDGE_COLORS = [
  "#3b82f6",
  "#10b981",
  "#8b5cf6",
  "#f59e0b",
  "#f43f5e",
]

const NODE_WIDTH = 260
const NODE_HEIGHT = 90

function RepoNode({ data }: NodeProps<Node<RepoNodeData>>) {
  const navigate = useNavigate()
  const label = data.label as string
  const [owner, repo] = label.split("/")
  const ci = data.colorIdx ?? -1

  const borderClass = data.isUtility
    ? "border-zinc-600"
    : ci >= 0
      ? GROUP_BORDER_COLORS[ci % GROUP_BORDER_COLORS.length]
      : "border-zinc-700"

  const bgClass = data.isUtility
    ? "bg-zinc-800/60"
    : ci >= 0
      ? GROUP_BG_COLORS[ci % GROUP_BG_COLORS.length]
      : "bg-zinc-900/80"

  const accentText =
    ci >= 0
      ? GROUP_TEXT_COLORS[ci % GROUP_TEXT_COLORS.length]
      : "text-zinc-500"

  return (
    <div
      onClick={() => navigate(`/repos/${owner}/${repo}`)}
      className={`cursor-pointer rounded-xl border-2 px-5 py-3.5 shadow-lg backdrop-blur transition-all hover:scale-105 hover:shadow-xl ${borderClass} ${bgClass}`}
      style={{ minWidth: NODE_WIDTH }}
    >
      {/* Handles on all four sides */}
      <Handle type="target" position={Position.Top} className="!h-2 !w-2 !border-0 !bg-zinc-500" />
      <Handle type="source" position={Position.Bottom} className="!h-2 !w-2 !border-0 !bg-zinc-500" />
      <Handle type="target" position={Position.Left} id="left-t" className="!h-2 !w-2 !border-0 !bg-zinc-500" />
      <Handle type="source" position={Position.Left} id="left-s" className="!h-2 !w-2 !border-0 !bg-zinc-500" />
      <Handle type="target" position={Position.Right} id="right-t" className="!h-2 !w-2 !border-0 !bg-zinc-500" />
      <Handle type="source" position={Position.Right} id="right-s" className="!h-2 !w-2 !border-0 !bg-zinc-500" />

      <div className="flex items-center gap-2.5">
        {data.isUtility ? (
          <Package className="h-4 w-4 shrink-0 text-zinc-500" />
        ) : (
          <Database className={`h-4 w-4 shrink-0 ${accentText}`} />
        )}
        <div className="min-w-0">
          <div className="text-[10px] text-zinc-500">{owner}</div>
          <div className="whitespace-nowrap text-sm font-bold text-zinc-100">
            {data.short}
          </div>
        </div>
      </div>
      {data.group && (
        <div className="mt-2 flex items-center gap-1">
          <Puzzle className={`h-3 w-3 shrink-0 ${accentText}`} />
          <span className={`text-[10px] font-medium ${accentText}`}>
            {data.group}
          </span>
        </div>
      )}
    </div>
  )
}

const nodeTypes = { repo: RepoNode }

// ── Dagre layout ──

function buildGraph(
  data: RelationshipsResponse,
): { nodes: Node<RepoNodeData>[]; edges: Edge[] } {
  // Collect all repos
  const allRepos = new Set<string>()
  for (const e of data.edges) {
    allRepos.add(e.source_repo)
    allRepos.add(e.target_repo)
  }
  for (const g of data.groups) {
    for (const r of g.repos) allRepos.add(r)
  }

  // Group membership
  const repoToGroup = new Map<string, string>()
  const repoToGroupIdx = new Map<string, number>()
  data.groups.forEach((g, gi) => {
    for (const r of g.repos) {
      repoToGroup.set(r, g.name)
      repoToGroupIdx.set(r, gi)
    }
  })

  // Detect utility repos
  const utilityRepos = new Set<string>()
  for (const repo of allRepos) {
    if (!repoToGroup.has(repo)) {
      const inbound = data.edges.filter((e) => e.target_repo === repo).length
      if (inbound >= 2) utilityRepos.add(repo)
    }
  }

  // ── Merge mutual edges ──
  const seen = new Set<string>()
  type MergedEdge = {
    source: string
    target: string
    mutual: boolean
    totalRefs: number
  }
  const mergedEdges: MergedEdge[] = []

  for (const e of data.edges) {
    const pairKey = [e.source_repo, e.target_repo].sort().join("||")
    if (seen.has(pairKey)) continue
    seen.add(pairKey)

    const reverse = data.edges.find(
      (o) =>
        o.source_repo === e.target_repo && o.target_repo === e.source_repo,
    )

    mergedEdges.push({
      source: e.source_repo,
      target: e.target_repo,
      mutual: !!reverse,
      totalRefs: e.ref_count + (reverse?.ref_count ?? 0),
    })
  }

  // ── Dagre layout ──
  const g = new dagre.graphlib.Graph()
  g.setGraph({
    rankdir: "LR", // left-to-right flow
    nodesep: 80,
    ranksep: 160,
    edgesep: 50,
    marginx: 40,
    marginy: 40,
  })
  g.setDefaultEdgeLabel(() => ({}))

  for (const repo of allRepos) {
    g.setNode(repo, { width: NODE_WIDTH, height: NODE_HEIGHT })
  }

  for (const me of mergedEdges) {
    g.setEdge(me.source, me.target)
  }

  dagre.layout(g)

  // ── Build React Flow nodes ──
  const nodes: Node<RepoNodeData>[] = []

  for (const repo of allRepos) {
    const pos = g.node(repo)
    const gi = repoToGroupIdx.get(repo)

    nodes.push({
      id: repo,
      type: "repo",
      position: {
        x: pos.x - NODE_WIDTH / 2,
        y: pos.y - NODE_HEIGHT / 2,
      },
      data: {
        label: repo,
        short: repo.split("/").pop() || repo,
        group: repoToGroup.get(repo),
        isUtility: utilityRepos.has(repo),
        colorIdx: gi,
      },
    })
  }

  // ── Build React Flow edges with position-aware handles ──
  const nodePositions = new Map(nodes.map((n) => [n.id, n.position]))

  const edges: Edge[] = mergedEdges.map((me) => {
    const sourcePos = nodePositions.get(me.source)!
    const targetPos = nodePositions.get(me.target)!

    const dx = targetPos.x - sourcePos.x
    const dy = targetPos.y - sourcePos.y

    // Pick handles based on dominant direction
    let sourceHandle: string | undefined
    let targetHandle: string | undefined

    if (Math.abs(dx) >= Math.abs(dy)) {
      // Horizontal: right→left or left→right
      if (dx >= 0) {
        sourceHandle = "right-s"
        targetHandle = "left-t"
      } else {
        sourceHandle = "left-s"
        targetHandle = "right-t"
      }
    }
    // else: vertical, use default top/bottom

    // Edge color
    const sgi = repoToGroupIdx.get(me.source)
    const tgi = repoToGroupIdx.get(me.target)
    const sameGroup = sgi !== undefined && sgi === tgi

    const color = sameGroup
      ? GROUP_EDGE_COLORS[sgi % GROUP_EDGE_COLORS.length]
      : me.mutual
        ? "#60a5fa"
        : "#52525b"

    const pairKey = [me.source, me.target].sort().join("||")

    return {
      id: `e-${pairKey}`,
      source: me.source,
      target: me.target,
      sourceHandle,
      targetHandle,
      animated: me.mutual,
      label: undefined,
      style: {
        stroke: color,
        strokeWidth: me.mutual ? 2.5 : 1.5,
        opacity: sameGroup || me.mutual ? 1 : 0.45,
      },
      markerStart: me.mutual
        ? { type: MarkerType.ArrowClosed, width: 12, height: 12, color }
        : undefined,
      markerEnd: {
        type: MarkerType.ArrowClosed,
        width: 12,
        height: 12,
        color,
      },
      type: "smoothstep",
    }
  })

  return { nodes, edges }
}

// ── Filter data before layout ──

function filterData(
  data: RelationshipsResponse,
  groupFilter: string | null,
  search: string,
): RelationshipsResponse {
  if (!groupFilter && !search) return data

  // Determine which repos to keep
  let keepRepos: Set<string> | null = null

  if (groupFilter) {
    const group = data.groups.find((g) => g.name === groupFilter)
    if (group) {
      // Show group members + their direct connections
      const members = new Set(group.repos)
      const connected = new Set(group.repos)
      for (const e of data.edges) {
        if (members.has(e.source_repo)) connected.add(e.target_repo)
        if (members.has(e.target_repo)) connected.add(e.source_repo)
      }
      keepRepos = connected
    }
  }

  if (search) {
    const q = search.toLowerCase()
    const matching = new Set<string>()
    for (const e of data.edges) {
      if (e.source_repo.toLowerCase().includes(q)) {
        matching.add(e.source_repo)
        matching.add(e.target_repo)
      }
      if (e.target_repo.toLowerCase().includes(q)) {
        matching.add(e.target_repo)
        matching.add(e.source_repo)
      }
    }
    for (const g of data.groups) {
      for (const r of g.repos) {
        if (r.toLowerCase().includes(q)) {
          g.repos.forEach((m) => matching.add(m))
        }
      }
    }
    if (matching.size > 0) {
      keepRepos = keepRepos
        ? new Set([...keepRepos].filter((r) => matching.has(r)))
        : matching
    }
  }

  if (!keepRepos) return data

  return {
    edges: data.edges.filter(
      (e) => keepRepos!.has(e.source_repo) && keepRepos!.has(e.target_repo),
    ),
    groups: data.groups
      .map((g) => ({
        ...g,
        repos: g.repos.filter((r) => keepRepos!.has(r)),
      }))
      .filter((g) => g.repos.length > 0),
  }
}

// ── Main component ──

export function RelationshipGraph({
  data,
}: {
  data: RelationshipsResponse
  repoFileCounts?: Record<string, number>
}) {
  const [groupFilter, setGroupFilter] = useState<string | null>(null)
  const [search, setSearch] = useState("")

  const filtered = useMemo(
    () => filterData(data, groupFilter, search),
    [data, groupFilter, search],
  )

  const { nodes: initialNodes, edges: initialEdges } = useMemo(
    () => buildGraph(filtered),
    [filtered],
  )

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes)
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges)

  // Re-sync when filter changes
  useEffect(() => {
    setNodes(initialNodes)
    setEdges(initialEdges)
  }, [initialNodes, initialEdges, setNodes, setEdges])

  const groupNames = data.groups.map((g) => g.name)

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <Input
          placeholder="Search repos..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="h-8 w-48 text-xs"
        />
        <button
          onClick={() => setGroupFilter(null)}
          className={`inline-flex h-8 items-center rounded-md border px-3 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
            !groupFilter
              ? "border-primary bg-primary/10 text-primary"
              : "border-input bg-background text-muted-foreground hover:bg-accent hover:text-accent-foreground"
          }`}
        >
          All
        </button>
        {groupNames.map((name, i) => (
          <button
            key={name}
            onClick={() =>
              setGroupFilter(groupFilter === name ? null : name)
            }
            className={`inline-flex h-8 items-center rounded-md border px-3 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
              groupFilter === name
                ? `border-current bg-primary/10 ${GROUP_TEXT_COLORS[i % GROUP_TEXT_COLORS.length]}`
                : "border-input bg-background text-muted-foreground hover:bg-accent hover:text-accent-foreground"
            }`}
          >
            {name}
          </button>
        ))}
        <span className="ml-auto text-xs text-zinc-500">
          {nodes.length} {nodes.length === 1 ? "repo" : "repos"} ·{" "}
          {edges.length} {edges.length === 1 ? "connection" : "connections"}
        </span>
      </div>

      {/* Graph */}
      <div className="h-[550px] w-full overflow-hidden rounded-xl border border-zinc-800 bg-zinc-950">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          nodeTypes={nodeTypes}
          connectionLineType={ConnectionLineType.SmoothStep}
          fitView
          fitViewOptions={{ padding: 0.2 }}
          proOptions={{ hideAttribution: true }}
          minZoom={0.1}
          maxZoom={2}
          defaultEdgeOptions={{ type: "smoothstep" }}
        >
          <Background gap={24} size={1} color="#27272a" />
          <Controls
            showInteractive={false}
            className="!border-zinc-700 !bg-zinc-900 [&>button]:!border-zinc-700 [&>button]:!bg-zinc-900 [&>button]:!fill-zinc-400 [&>button:hover]:!bg-zinc-800"
          />
        </ReactFlow>
      </div>
    </div>
  )
}
