import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight, ChevronsUpDown } from "lucide-react"
import type { ReactNode } from "react"

import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import type { DataTableApi } from "@/components/ui/use-data-table"

const ALIGN: Record<string, string> = {
  left: "text-left",
  right: "text-right",
  center: "text-center",
}

export function DataTable<T>({
  table,
  rowKey,
  loading = false,
  emptyMessage = "No results.",
  onRowClick,
}: {
  table: DataTableApi<T>
  rowKey: (row: T) => string | number
  loading?: boolean
  emptyMessage?: ReactNode
  onRowClick?: (row: T) => void
}) {
  const { columns, rows, sort, toggleSort } = table
  const skeletonRows = table.paginate ? Math.min(table.pageSize, 8) : 6

  return (
    <Table>
      <TableHeader>
        <TableRow>
          {columns.map((col) => {
            const active = sort?.key === col.key
            const Icon = !active ? ChevronsUpDown : sort?.dir === "asc" ? ArrowUp : ArrowDown
            return (
              <TableHead
                key={col.key}
                className={`${ALIGN[col.align ?? "left"]} ${col.headerClassName ?? ""}`}
              >
                {col.sortable ? (
                  <button
                    type="button"
                    onClick={() => toggleSort(col.key)}
                    className={`inline-flex items-center gap-1 hover:text-foreground ${
                      active ? "text-foreground" : ""
                    } ${col.align === "right" ? "flex-row-reverse" : ""}`}
                  >
                    {col.header}
                    <Icon className={`h-3.5 w-3.5 ${active ? "" : "opacity-50"}`} />
                  </button>
                ) : (
                  col.header
                )}
              </TableHead>
            )
          })}
        </TableRow>
      </TableHeader>
      <TableBody>
        {loading ? (
          Array.from({ length: skeletonRows }).map((_, i) => (
            <TableRow key={i}>
              {columns.map((col) => (
                <TableCell key={col.key} className={ALIGN[col.align ?? "left"]}>
                  <Skeleton className="h-4 w-full max-w-[120px]" />
                </TableCell>
              ))}
            </TableRow>
          ))
        ) : rows.length > 0 ? (
          rows.map((row) => (
            <TableRow
              key={rowKey(row)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              className={onRowClick ? "cursor-pointer" : undefined}
            >
              {columns.map((col) => (
                <TableCell
                  key={col.key}
                  className={`${ALIGN[col.align ?? "left"]} ${col.cellClassName ?? ""}`}
                >
                  {col.cell(row)}
                </TableCell>
              ))}
            </TableRow>
          ))
        ) : (
          <TableRow>
            <TableCell colSpan={columns.length} className="py-8 text-center text-muted-foreground">
              {emptyMessage}
            </TableCell>
          </TableRow>
        )}
      </TableBody>
    </Table>
  )
}

/** Pagination controls for a DataTable — render it wherever you like (e.g. below the card). */
export function DataTablePagination<T>({ table }: { table: DataTableApi<T> }) {
  if (!table.paginate || table.total === 0) return null
  const { page, totalPages, setPage, pageSize, total } = table
  return (
    <div className="flex items-center justify-between text-sm text-muted-foreground">
      <span>
        {page * pageSize + 1}–{Math.min((page + 1) * pageSize, total)} of {total}
      </span>
      <div className="flex items-center gap-2">
        <Button variant="outline" size="sm" onClick={() => setPage(page - 1)} disabled={page <= 0}>
          <ChevronLeft className="h-4 w-4" />
          Prev
        </Button>
        <span className="tabular-nums">
          Page {page + 1} of {totalPages}
        </span>
        <Button
          variant="outline"
          size="sm"
          onClick={() => setPage(page + 1)}
          disabled={page >= totalPages - 1}
        >
          Next
          <ChevronRight className="h-4 w-4" />
        </Button>
      </div>
    </div>
  )
}
