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
import { useMemo } from "react"

type DepNodeKind = "file" | "external"

type DepNodeData = {
  label: string
  kind: DepNodeKind
  externalKind?: string
  refCount?: number
}

const KIND_STYLES = {
  file: {
    border: "border-blue-500",
    bg: "bg-blue-500/10",
    text: "text-blue-400",
    edge: "#3b82f6",
    tag: "FILE",
  },
  external: {
    border: "border-emerald-500",
    bg: "bg-emerald-500/10",
    text: "text-emerald-400",
    edge: "#10b981",
    tag: "EXTERNAL",
  },
} as const

const NODE_W = 200
const NODE_H = 60
// Keep graphs readable on repos with hundreds of files.
const MAX_FILES = 25
const MAX_EXTERNAL = 20

function DepNode({ data }: NodeProps<Node<DepNodeData>>) {
  const style = KIND_STYLES[data.kind]
  return (
    <div
      className={`rounded-lg border-2 px-3 py-2 shadow-md transition-all hover:shadow-lg ${style.border} ${style.bg}`}
      style={{ minWidth: 140 }}
    >
      <Handle
        type="target"
        position={Position.Left}
        className="!h-2 !w-2 !border-0 !bg-zinc-500"
      />
      <Handle
        type="source"
        position={Position.Right}
        className="!h-2 !w-2 !border-0 !bg-zinc-500"
      />
      <div className={`text-xs font-bold ${style.text}`}>{style.tag}</div>
      <div className="truncate text-sm font-medium text-zinc-100">
        {data.kind === "file"
          ? (data.label as string).split("/").pop()
          : data.label}
      </div>
      {data.kind === "external" && data.externalKind && (
        <div className="mt-1 text-[10px] uppercase tracking-wide text-zinc-400">
          {data.externalKind}
        </div>
      )}
      {data.refCount && data.refCount > 1 && (
        <div className="mt-1 text-[10px] text-zinc-400">
          {data.refCount} refs
        </div>
      )}
    </div>
  )
}

const nodeTypes = { dep: DepNode }

export interface ImportEdge {
  source: string
  target: string
}

export interface ExternalRef {
  file_path: string
  kind: string
  target: string
  description: string
}

export function DependenciesGraph({
  imports,
  externalRefs,
}: {
  imports: ImportEdge[]
  externalRefs: ExternalRef[]
}) {
  const { nodes, edges, truncatedCount } = useMemo(() => {
    // Count how "connected" each file is, for ranking when we truncate.
    const connectionCount = new Map<string, number>()
    const bump = (path: string) =>
      connectionCount.set(path, (connectionCount.get(path) ?? 0) + 1)

    for (const e of imports) {
      bump(e.source)
      bump(e.target)
    }
    for (const r of externalRefs) {
      bump(r.file_path)
    }

    // Pick the top-N most connected files.
    const ranked = [...connectionCount.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([path]) => path)
    const visibleFiles = new Set(ranked.slice(0, MAX_FILES))
    const truncated = Math.max(0, ranked.length - visibleFiles.size)

    // Deduplicate external refs by target, counting how many files reference each.
    const externalRefCounts = new Map<
      string,
      { kind: string; count: number }
    >()
    for (const r of externalRefs) {
      if (!visibleFiles.has(r.file_path)) continue
      const existing = externalRefCounts.get(r.target)
      if (existing) existing.count += 1
      else externalRefCounts.set(r.target, { kind: r.kind, count: 1 })
    }
    const visibleExternal = [...externalRefCounts.entries()]
      .sort((a, b) => b[1].count - a[1].count)
      .slice(0, MAX_EXTERNAL)

    const g = new dagre.graphlib.Graph()
    g.setGraph({ rankdir: "LR", nodesep: 30, ranksep: 80 })
    g.setDefaultEdgeLabel(() => ({}))

    const allNodes: Node<DepNodeData>[] = []
    const allEdges: Edge[] = []

    visibleFiles.forEach((path) => {
      const id = `f:${path}`
      g.setNode(id, { width: NODE_W, height: NODE_H })
      allNodes.push({
        id,
        type: "dep",
        position: { x: 0, y: 0 },
        data: { label: path, kind: "file" },
      })
    })

    visibleExternal.forEach(([target, info]) => {
      const id = `x:${target}`
      g.setNode(id, { width: NODE_W, height: NODE_H })
      allNodes.push({
        id,
        type: "dep",
        position: { x: 0, y: 0 },
        data: {
          label: target,
          kind: "external",
          externalKind: info.kind,
          refCount: info.count,
        },
      })
    })

    for (const e of imports) {
      if (!visibleFiles.has(e.source) || !visibleFiles.has(e.target)) continue
      const src = `f:${e.source}`
      const dst = `f:${e.target}`
      g.setEdge(src, dst)
      allEdges.push({
        id: `e-${src}-${dst}`,
        source: src,
        target: dst,
        style: { stroke: KIND_STYLES.file.edge, strokeWidth: 1.2 },
        markerEnd: {
          type: MarkerType.ArrowClosed,
          width: 12,
          height: 12,
          color: KIND_STYLES.file.edge,
        },
        type: "smoothstep",
      })
    }

    const visibleExternalIds = new Set(visibleExternal.map(([t]) => t))
    for (const r of externalRefs) {
      if (!visibleFiles.has(r.file_path)) continue
      if (!visibleExternalIds.has(r.target)) continue
      const src = `f:${r.file_path}`
      const dst = `x:${r.target}`
      // Avoid duplicate edges — multiple refs from the same file to the same target.
      const key = `e-${src}-${dst}`
      if (allEdges.some((ed) => ed.id === key)) continue
      g.setEdge(src, dst)
      allEdges.push({
        id: key,
        source: src,
        target: dst,
        style: {
          stroke: KIND_STYLES.external.edge,
          strokeWidth: 1.2,
          strokeDasharray: "4 3",
        },
        markerEnd: {
          type: MarkerType.ArrowClosed,
          width: 12,
          height: 12,
          color: KIND_STYLES.external.edge,
        },
        type: "smoothstep",
      })
    }

    dagre.layout(g)

    for (const node of allNodes) {
      const pos = g.node(node.id)
      if (pos) {
        node.position = { x: pos.x - NODE_W / 2, y: pos.y - NODE_H / 2 }
      }
    }

    return { nodes: allNodes, edges: allEdges, truncatedCount: truncated }
  }, [imports, externalRefs])

  const [n, , onNodesChange] = useNodesState(nodes)
  const [e, , onEdgesChange] = useEdgesState(edges)

  if (nodes.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No dependencies detected yet.
      </p>
    )
  }

  return (
    <div className="space-y-2">
      <div className="h-[500px] w-full overflow-hidden rounded-xl border border-zinc-800 bg-zinc-950">
        <ReactFlow
          nodes={n}
          edges={e}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          nodeTypes={nodeTypes}
          connectionLineType={ConnectionLineType.SmoothStep}
          fitView
          fitViewOptions={{ padding: 0.3 }}
          proOptions={{ hideAttribution: true }}
          minZoom={0.2}
          maxZoom={2}
          defaultEdgeOptions={{ type: "smoothstep" }}
        >
          <Background gap={20} size={1} color="#27272a" />
          <Controls
            showInteractive={false}
            className="!border-zinc-700 !bg-zinc-900 [&>button]:!border-zinc-700 [&>button]:!bg-zinc-900 [&>button]:!fill-zinc-400 [&>button:hover]:!bg-zinc-800"
          />
        </ReactFlow>
      </div>
      {truncatedCount > 0 && (
        <p className="text-xs text-muted-foreground">
          Showing the {MAX_FILES} most connected files of{" "}
          {MAX_FILES + truncatedCount}. Dashed edges indicate external
          dependencies.
        </p>
      )}
      {truncatedCount === 0 && (
        <p className="text-xs text-muted-foreground">
          Dashed edges indicate external dependencies.
        </p>
      )}
    </div>
  )
}
