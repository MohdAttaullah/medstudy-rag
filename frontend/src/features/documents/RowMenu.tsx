import { useEffect, useId, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react';
import { Icon } from '../navigation/icons';

export interface RowMenuItem {
  label: string;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
}

/**
 * The ⋯ menu for one row. Rarely used and consequential actions live here, so the row itself
 * stays a list of documents rather than a wall of buttons. Keyboard: Enter/Space opens, arrows
 * move, Escape closes and returns focus to the button.
 */
export function RowMenu({ label, items }: { label: string; items: RowMenuItem[] }) {
  const [open, setOpen] = useState(false);
  // Placed against the viewport, not the row: rows sit in a horizontally scrolling table, which
  // would otherwise clip the menu of the last rows.
  const [place, setPlace] = useState<CSSProperties>({});
  const holder = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useId();

  useEffect(() => {
    if (!open) return;
    // Follows its trigger when the page or the table scrolls, rather than closing on a scroll the
    // user did not make (focusing a row can scroll it into view).
    function follow() {
      const box = trigger.current?.getBoundingClientRect();
      if (!box) return;
      if (box.bottom < 0 || box.top > window.innerHeight) { setOpen(false); return; }
      const below = window.innerHeight - box.bottom > 120;
      setPlace({
        position: 'fixed', insetInlineEnd: Math.max(8, window.innerWidth - box.right),
        ...(below ? { top: box.bottom + 4, bottom: 'auto' } : { top: 'auto', bottom: window.innerHeight - box.top + 4 }),
      });
    }
    follow();
    holder.current?.querySelector<HTMLElement>('[role="menuitem"]:not([disabled])')?.focus({ preventScroll: true });
    function away(event: MouseEvent) {
      if (!holder.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', away);
    window.addEventListener('scroll', follow, true);
    window.addEventListener('resize', follow);
    return () => {
      document.removeEventListener('mousedown', away);
      window.removeEventListener('scroll', follow, true);
      window.removeEventListener('resize', follow);
    };
  }, [open]);

  if (!items.length) return null;

  function close() { setOpen(false); trigger.current?.focus(); }

  function move(event: KeyboardEvent) {
    if (event.key === 'Escape') { event.preventDefault(); close(); return; }
    if (event.key === 'Tab') { setOpen(false); return; }
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    event.preventDefault();
    const entries = [...(holder.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])') ?? [])];
    const at = entries.indexOf(document.activeElement as HTMLElement);
    const next = event.key === 'ArrowDown' ? (at + 1) % entries.length : (at - 1 + entries.length) % entries.length;
    entries[next]?.focus({ preventScroll: true });
  }

  return <div className="row-menu" ref={holder} onKeyDown={open ? move : undefined}>
    <button type="button" ref={trigger} className="icon-button" aria-haspopup="menu"
      aria-expanded={open} aria-controls={open ? menu : undefined} onClick={() => setOpen(value => !value)}>
      <Icon name="more" /><span className="visually-hidden">{label}</span>
    </button>
    {open && <div className="row-menu-list" role="menu" id={menu} aria-label={label} style={place}>
      {items.map(item => <button key={item.label} type="button" role="menuitem" disabled={item.disabled}
        className={item.danger ? 'menu-danger' : undefined}
        // Focus goes back to the trigger first, so a dialog the item opens returns it there.
        onClick={() => { setOpen(false); trigger.current?.focus(); item.onSelect(); }}>
        {item.danger && <Icon name="delete" small />}{item.label}
      </button>)}
    </div>}
  </div>;
}
