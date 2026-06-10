import { type ReactNode, useState } from "react"

export type SortDir = "asc" | "desc"

export interface Column<T> {
  /** Stable key; also the sort identity. */
  key: string
  header: ReactNode
  align?: "left" | "right" | "center"
  /** Make the header clickable to sort. Requires `sortValue`. */
  sortable?: boolean
  /** Value used for sorting (string → locale compare, number → numeric, null → last). */
  sortValue?: (row: T) => string | number | null
  cell: (row: T) => ReactNode
  headerClassName?: string
  cellClassName?: string
}

/** Shared sort/pagination state, wired between <DataTable> and <DataTablePagination>. */
export interface DataTableApi<T> {
  columns: Column<T>[]
  rows: T[] // the current page's rows
  sort: { key: string; dir: SortDir } | null
  toggleSort: (key: string) => void
  page: number
  totalPages: number
  setPage: (n: number) => void
  total: number
  pageSize: number
  paginate: boolean
}

function sortRows<T>(
  rows: T[],
  columns: Column<T>[],
  sort: { key: string; dir: SortDir } | null,
): T[] {
  if (!sort) return rows
  const col = columns.find((c) => c.key === sort.key)
  if (!col?.sortValue) return rows
  const dir = sort.dir === "asc" ? 1 : -1
  return [...rows].sort((a, b) => {
    const av = col.sortValue!(a)
    const bv = col.sortValue!(b)
    if (av == null && bv == null) return 0
    if (av == null) return 1 // nulls always sort last
    if (bv == null) return -1
    if (typeof av === "string" || typeof bv === "string") {
      return dir * String(av).localeCompare(String(bv))
    }
    return dir * (av - bv)
  })
}

/**
 * Owns the table's sort + pagination state and returns the current page of rows
 * plus the controls. Pass the result to <DataTable> and (optionally) place a
 * <DataTablePagination> for the same api wherever you want it.
 */
export function useDataTable<T>({
  rows,
  columns,
  initialSort,
  pageSize,
}: {
  rows: T[]
  columns: Column<T>[]
  initialSort?: { key: string; dir: SortDir }
  pageSize?: number
}): DataTableApi<T> {
  const [sort, setSort] = useState<{ key: string; dir: SortDir } | null>(initialSort ?? null)
  const [page, setPage] = useState(0)

  // Cheap to sort inline (these tables are bounded); avoids memoizing over the
  // column array, which is typically rebuilt each render.
  const sorted = sortRows(rows, columns, sort)

  const paginate = !!pageSize && pageSize > 0
  const size = pageSize ?? 0
  const totalPages = paginate ? Math.max(1, Math.ceil(sorted.length / size)) : 1
  // Clamp instead of resetting via an effect — keeps us valid when the row set
  // shrinks (e.g. a search filter) without a setState-in-effect.
  const currentPage = Math.min(page, totalPages - 1)
  const pageRows = paginate ? sorted.slice(currentPage * size, currentPage * size + size) : sorted

  const toggleSort = (key: string) => {
    const col = columns.find((c) => c.key === key)
    if (!col?.sortable || !col.sortValue) return
    setSort((prev) =>
      prev?.key === key
        ? { key, dir: prev.dir === "asc" ? "desc" : "asc" }
        : { key, dir: "desc" },
    )
    setPage(0)
  }

  return {
    columns,
    rows: pageRows,
    sort,
    toggleSort,
    page: currentPage,
    totalPages,
    setPage,
    total: sorted.length,
    pageSize: size,
    paginate,
  }
}
