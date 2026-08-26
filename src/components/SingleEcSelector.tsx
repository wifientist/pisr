import React, { useState, useMemo } from "react";
import { useSingleEc } from "@/hooks/useSingleEc";
import {
  useReactTable,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  flexRender,
  createColumnHelper
} from '@tanstack/react-table';

interface SingleEcSelectorProps {
  controllerId: number | null;
  onEcSelect: (ecId: string | null, ec: any) => void;
  selectedEcId?: string | null;
}

export default function SingleEcSelector({ controllerId, onEcSelect, selectedEcId }: SingleEcSelectorProps) {
  const { ecData, loading, error } = useSingleEc(controllerId);
  const [globalFilter, setGlobalFilter] = useState("");

  const columnHelper = createColumnHelper<any>();

  const columns = useMemo(() => [
    columnHelper.accessor('name', {
      header: 'EC Name',
      cell: info => info.getValue()
    }),
    columnHelper.accessor('tenantType', {
      header: 'Type',
      cell: info => info.getValue() || 'N/A'
    }),
    columnHelper.accessor('id', {
      header: 'ID',
      cell: info => info.getValue()
    }),
    columnHelper.display({
      id: 'select',
      header: 'Action',
      cell: info => {
        const ec = info.row.original;
        const isSelected = selectedEcId === ec.id;
        return (
          <button
            className={`px-3 py-1 rounded text-sm font-medium ${
              isSelected
                ? "bg-blue-600 text-white"
                : "bg-gray-200 text-gray-700 hover:bg-gray-300"
            }`}
            onClick={() => onEcSelect(isSelected ? null : ec.id, ec)}
          >
            {isSelected ? "Selected" : "Select"}
          </button>
        );
      }
    })
  ], [selectedEcId, onEcSelect]);

  const table = useReactTable({
    data: ecData,
    columns,
    state: {
      globalFilter
    },
    globalFilterFn: (row, columnId, filterValue) => {
      const searchString = Object.values(row.original)
        .join(" ")
        .toLowerCase();
      return searchString.includes(filterValue.toLowerCase());
    },
    getCoreRowModel: getCoreRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getSortedRowModel: getSortedRowModel()
  });

  if (loading) {
    return <div className="text-center p-4">Loading ECs...</div>;
  }

  if (error) {
    return <div className="text-center p-4 text-red-600">Error: {error}</div>;
  }

  if (!ecData || ecData.length === 0) {
    return <div className="text-center p-4 text-gray-500">No ECs found</div>;
  }

  return (
    <div>
      <input
        type="text"
        placeholder="Search ECs..."
        value={globalFilter}
        onChange={e => setGlobalFilter(e.target.value)}
        className="w-full px-4 py-2 border border-gray-300 rounded-lg mb-4 focus:ring-2 focus:ring-blue-500 focus:border-transparent"
      />

      <div className="overflow-x-auto">
        <table className="w-full border-collapse border border-gray-300">
          <thead className="bg-gray-100">
            {table.getHeaderGroups().map(headerGroup => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map(header => (
                  <th key={header.id} className="border border-gray-300 px-4 py-2 text-left font-semibold">
                    {flexRender(header.column.columnDef.header, header.getContext())}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map(row => (
              <tr
                key={row.id}
                className={`hover:bg-gray-50 ${
                  selectedEcId === row.original.id ? 'bg-blue-50' : ''
                }`}
              >
                {row.getVisibleCells().map(cell => (
                  <td key={cell.id} className="border border-gray-300 px-4 py-2">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
