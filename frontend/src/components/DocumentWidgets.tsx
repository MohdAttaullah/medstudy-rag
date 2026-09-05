export function StatusBadge({ status }: { status: string }) {
  return <span className={'state state-' + status.toLowerCase()}>{status.toLowerCase().replaceAll('_', ' ')}</span>;
}
export function Pagination({ offset, total, onChange, limit = 20 }: { offset: number; total: number; onChange: (offset: number) => void; limit?: number }) {
  return <div className="pagination"><span>{total === 0 ? 'No results' : `${offset + 1}–${Math.min(offset + limit, total)} of ${total}`}</span>
    <button className="secondary" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>Previous</button>
    <button className="secondary" disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>Next</button></div>;
}
export const date = (value: string | null) => value ? new Date(value).toLocaleString() : '—';
export const size = (bytes: number) => (bytes / 1024 / 1024).toFixed(2) + ' MB';
