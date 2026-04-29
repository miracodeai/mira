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
import { useNavigate } from "react-router"

type BlastNodeData = {
  label: string
  kind: "core" | "dependent" | "cross-repo"
  summary?: string
  symbols?: string[]
}

const KIND_STYLES = {
  core: { border: "border-orange-500", bg: "bg-orange-500/10", text: "text-orange-400", edge: "#f97316" },
  dependent: { border: "border-blue-500", bg: "bg-blue-500/10", text: "text-blue-400", edge: "#3b82f6" },
  "cross-repo": { border: "border-violet-500", bg: "bg-violet-500/10", text: "text-violet-400", edge: "#8b5cf6" },
}

const NODE_W = 200
const NODE_H = 60

function BlastNode({ data }: NodeProps<Node<BlastNodeData>>) {
  const navigate = useNavigate()
  const style = KIND_STYLES[data.kind]

  const handleClick = () => {
    if (data.kind === "cross-repo") {
      const parts = (data.label as string).split("/")
      if (parts.length === 2) navigate(`/repos/${parts[0]}/${parts[1]}`)
    }
  }

  return (
    <div
      onClick={handleClick}
      className={`rounded-lg border-2 px-3 py-2 shadow-md transition-all hover:shadow-lg ${style.border} ${style.bg} ${data.kind === "cross-repo" ? "cursor-pointer" : ""}`}
      style={{ minWidth: 140 }}
    >
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !border-0 !bg-zinc-500" />
      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !border-0 !bg-zinc-500" />
      <div className={`text-xs font-bold ${style.text}`}>
        {data.kind === "core" ? "CORE" : data.kind === "cross-repo" ? "CROSS-REPO" : "DEPENDENT"}
      </div>
      <div className="truncate text-sm font-medium text-zinc-100">
        {(data.label as string).split("/").pop()}
      </div>
      {data.symbols && data.symbols.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-1">
          {data.symbols.slice(0, 3).map((s) => (
            <span key={s} className="rounded bg-zinc-800 px-1 py-0.5 font-mono text-[9px] text-zinc-400">
              {s}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

const nodeTypes = { blast: BlastNode }

export function BlastGraph({
  coreFiles,
  dependents,
  crossRepo,
}: {
  coreFiles: { path: string; symbols: string[] }[]
  dependents: { path: string; summary: string; symbols: string[] }[]
  crossRepo: { repo: string; refCount: number }[]
}) {
  const { nodes, edges } = useMemo(() => {
    const g = new dagre.graphlib.Graph()
    g.setGraph({ rankdir: "LR", nodesep: 30, ranksep: 80 })
    g.setDefaultEdgeLabel(() => ({}))

    const allNodes: Node<BlastNodeData>[] = []
    const allEdges: Edge[] = []

    // Core files
    coreFiles.forEach((f) => {
      const id = `core:${f.path}`
      g.setNode(id, { width: NODE_W, height: NODE_H })
      allNodes.push({
        id,
        type: "blast",
        position: { x: 0, y: 0 },
        data: { label: f.path, kind: "core", symbols: f.symbols },
      })
    })

    // Helper: find the core file that shares the most symbols with a dependent
    const findBestCore = (depSymbols: string[]) => {
      if (coreFiles.length === 0) return null
      if (coreFiles.length === 1) return coreFiles[0]

      let best = coreFiles[0]
      let bestOverlap = 0

      for (const core of coreFiles) {
        const overlap = core.symbols.filter((s) => depSymbols.includes(s)).length
        if (overlap > bestOverlap) {
          bestOverlap = overlap
          best = core
        }
      }

      return best
    }

    // Dependents — connect to the most related core file
    dependents.forEach((d) => {
      const id = `dep:${d.path}`
      g.setNode(id, { width: NODE_W, height: NODE_H })
      allNodes.push({
        id,
        type: "blast",
        position: { x: 0, y: 0 },
        data: { label: d.path, kind: "dependent", summary: d.summary, symbols: d.symbols },
      })

      const bestCore = findBestCore(d.symbols)
      if (bestCore) {
        const targetId = `core:${bestCore.path}`
        g.setEdge(targetId, id)
        allEdges.push({
          id: `e-${targetId}-${id}`,
          source: targetId,
          target: id,
          style: { stroke: KIND_STYLES.dependent.edge, strokeWidth: 1.5 },
          markerEnd: { type: MarkerType.ArrowClosed, width: 12, height: 12, color: KIND_STYLES.dependent.edge },
          type: "smoothstep",
        })
      }
    })

    // Cross-repo — distribute across core files round-robin
    crossRepo.forEach((cr, i) => {
      const id = `cross:${cr.repo}`
      g.setNode(id, { width: NODE_W, height: NODE_H })
      allNodes.push({
        id,
        type: "blast",
        position: { x: 0, y: 0 },
        data: { label: cr.repo, kind: "cross-repo" },
      })

      if (coreFiles.length > 0) {
        const coreIdx = i % coreFiles.length
        const targetId = `core:${coreFiles[coreIdx].path}`
        g.setEdge(targetId, id)
        allEdges.push({
          id: `e-${targetId}-${id}`,
          source: targetId,
          target: id,
          style: { stroke: KIND_STYLES["cross-repo"].edge, strokeWidth: 1.5 },
          markerEnd: { type: MarkerType.ArrowClosed, width: 12, height: 12, color: KIND_STYLES["cross-repo"].edge },
          type: "smoothstep",
          animated: true,
        })
      }
    })

    dagre.layout(g)

    // Apply dagre positions
    for (const node of allNodes) {
      const pos = g.node(node.id)
      if (pos) {
        node.position = { x: pos.x - NODE_W / 2, y: pos.y - NODE_H / 2 }
      }
    }

    return { nodes: allNodes, edges: allEdges }
  }, [coreFiles, dependents, crossRepo])

  const [n, , onNodesChange] = useNodesState(nodes)
  const [e, , onEdgesChange] = useEdgesState(edges)

  if (nodes.length === 0) {
    return null
  }

  return (
    <div className="h-[400px] w-full overflow-hidden rounded-xl border border-zinc-800 bg-zinc-950">
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
        minZoom={0.3}
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
  )
}
