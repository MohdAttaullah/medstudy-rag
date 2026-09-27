import { useEffect, useRef, useState } from 'react';
import { useSession } from '../library/Session';
import { Icon } from './icons';

/**
 * Who you are signed in as, and how to stop being signed in as them.
 *
 * This used to be a line of text and a loose button sitting at the top of the *content* area —
 * above the educational notice, above the question, above the answer. It appeared on every gated
 * page, and on Operations it appeared twice, because two `AccessGate`s render on that page. It is
 * session chrome, so it belongs in the header with the rest of the session chrome.
 *
 * Nothing about authentication changed: the same token, the same `setSession('', null)`, the same
 * clearing of the cached queries and the open conversation.
 */

/** Initials for the avatar — the first letter of the first two words, which is enough to identify
 *  a workspace at a glance without printing the whole principal name in the header. */
function initials(name: string) {
  const words = name.trim().split(/\s+/).filter(Boolean);
  return (words.slice(0, 2).map(word => word[0]).join('') || '?').toUpperCase();
}

export function AccountMenu() {
  const { identity, setSession } = useSession();
  const [open, setOpen] = useState(false);
  const holder = useRef<HTMLDivElement>(null);

  // Closes the way a menu is expected to close: click away, or Escape.
  useEffect(() => {
    if (!open) return;
    function away(event: MouseEvent) {
      if (!holder.current?.contains(event.target as Node)) setOpen(false);
    }
    function key(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false);
    }
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', key);
    return () => {
      document.removeEventListener('mousedown', away);
      document.removeEventListener('keydown', key);
    };
  }, [open]);

  if (!identity) return null;

  return <div className="account" ref={holder}>
    <button type="button" className="account-button" aria-expanded={open} aria-haspopup="menu"
      onClick={() => setOpen(value => !value)}>
      <span className="avatar" aria-hidden="true">{initials(identity.display_name)}</span>
      <span className="visually-hidden">Account and workspace</span>
      <Icon name="chevron-down" small />
    </button>
    {open && <div className="account-menu" role="menu" aria-label="Account and workspace">
      <h2>{identity.display_name}</h2>
      {/* The role in plain words. The permission list behind it is an engineering concern and
          stays out of the everyday interface. */}
      <span className="muted">{ROLES[identity.role] ?? identity.role}</span>
      <hr />
      <button type="button" role="menuitem" className="secondary"
        onClick={() => { setOpen(false); setSession('', null); }}>
        <Icon name="sign-out" small />Sign out
      </button>
    </div>}
  </div>;
}

/** Roles as a reader would describe them, rather than as the authorization layer names them. */
const ROLES: Record<string, string> = {
  admin: 'Curator · full access',
  curator: 'Curator',
  reader: 'Reader · view only',
};
