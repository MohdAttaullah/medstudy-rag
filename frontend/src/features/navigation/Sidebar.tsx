import { useEffect, useState } from 'react';
import { Link, NavLink } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { useSession } from '../library/Session';
import type { Page } from '../../types/documents';
import type { ConversationSummary } from '../../types/retrieval';

/**
 * The workspace rail: where you are, what you asked before, and the tools you are allowed to use.
 *
 * Three decisions are encoded here.
 *
 * *Conversations belong beside the navigation, not above the chat.* A reader opening Ask wants the
 * conversation they are in, not a directory of every conversation they have ever had.
 *
 * *Engineering tools are grouped and collapsed.* A reader with `ask:submit` and nothing else was
 * being shown four inspector pages they cannot open; permissions now decide what is listed, and
 * what remains is folded under one disclosure so the everyday three stay obvious.
 *
 * *Collapsing is a local preference and nothing more.* It is remembered in this browser, it never
 * reaches the server, and it cannot change the conversation, the session or anything persisted.
 */

const PRIMARY = [
  ['ask', 'Ask', '◆'],
  ['library', 'Library', '▤'],
  ['settings', 'Settings', '⚙'],
] as const;

/** Each advanced tool with the permission that makes it usable. */
const ADVANCED = [
  ['retrieval', 'Retrieval inspector', '◎', 'retrieval:search'],
  ['evaluations', 'Evaluations', '◇', 'retrieval:search'],
  ['operations', 'Operations', '▣', 'ingestion:read'],
  ['audit', 'Audit', '☰', 'audit:read'],
] as const;

const COLLAPSED_KEY = 'medrag.sidebar.collapsed';

function remembered(key: string, fallback: boolean): boolean {
  try {
    const stored = window.localStorage.getItem(key);
    return stored === null ? fallback : stored === 'true';
  } catch {
    // A browser refusing storage is not a reason to fail: the preference simply does not persist.
    return fallback;
  }
}

export function Sidebar({ open, onNavigate }: { open: boolean; onNavigate: () => void }) {
  const { token, identity, conversation, setConversation } = useSession();
  const [collapsed, setCollapsed] = useState(() => remembered(COLLAPSED_KEY, false));
  const [showAdvanced, setShowAdvanced] = useState(() => remembered('medrag.sidebar.tools', false));

  useEffect(() => {
    try { window.localStorage.setItem(COLLAPSED_KEY, String(collapsed)); } catch { /* ignore */ }
  }, [collapsed]);
  useEffect(() => {
    try { window.localStorage.setItem('medrag.sidebar.tools', String(showAdvanced)); } catch { /* ignore */ }
  }, [showAdvanced]);

  // The rail lists conversations; their content is always read from the server.
  const conversations = useQuery({
    queryKey: ['conversations'],
    queryFn: () => api<Page<ConversationSummary>>(token, '/conversations?limit=50'),
    enabled: Boolean(token),
  });
  const listed = conversations.data?.items ?? [];
  const permissions = identity?.permissions ?? [];
  const tools = ADVANCED.filter(([, , , permission]) => permissions.includes(permission));

  function label(text: string) {
    return collapsed ? <span className="visually-hidden">{text}</span> : <span>{text}</span>;
  }

  return <aside
    className={`sidebar${collapsed ? ' sidebar-collapsed' : ''}${open ? ' sidebar-open' : ''}`}
    aria-label="Workspace navigation"
  >
    <div className="brand">
      <span className="brand-mark" aria-hidden="true">+</span>
      {!collapsed && <div>MEDICAL<span>Evidence workspace</span></div>}
    </div>

    <button type="button" className="rail-toggle secondary" aria-expanded={!collapsed}
      onClick={() => setCollapsed(value => !value)}>
      <span aria-hidden="true">{collapsed ? '»' : '«'}</span>
      <span className="visually-hidden">{collapsed ? 'Expand sidebar' : 'Collapse sidebar'}</span>
    </button>

    <nav aria-label="Main navigation">
      {PRIMARY.map(([path, text, icon]) =>
        <NavLink key={path} to={`/${path}`} onClick={onNavigate} title={collapsed ? text : undefined}>
          <span className="nav-icon" aria-hidden="true">{icon}</span>{label(text)}
        </NavLink>)}
    </nav>

    {identity && <section className="rail-conversations" aria-label="Recent conversations">
      {/* A plain link, not a NavLink: "current" here means the conversation that is open, and
          NavLink would mark this as current on every visit to /ask. */}
      <Link to="/ask" className="new-conversation" onClick={() => { setConversation(null); onNavigate(); }}
        title={collapsed ? 'New conversation' : undefined}>
        <span className="nav-icon" aria-hidden="true">+</span>{label('New conversation')}
      </Link>
      {!collapsed && <>
        <p className="rail-heading" id="recent-conversations">Recent</p>
        {conversations.isPending && <p className="muted">Loading…</p>}
        {conversations.isError && <p role="alert" className="muted">Conversations unavailable.</p>}
        {conversations.isSuccess && !listed.length &&
          <p className="muted">No conversations yet.</p>}
        {/* Scrolls independently, so a long history never pushes the tools off the rail. */}
        <ul className="rail-list" aria-labelledby="recent-conversations">
          {listed.map(item => <li key={item.conversation_id}>
            {/* NavLink matches on the path alone, so every conversation would read as current.
                Which conversation is open is what `aria-current` has to say here. */}
            <Link
              to={`/ask?conversation=${item.conversation_id}`}
              onClick={() => { setConversation(item.conversation_id); onNavigate(); }}
              aria-current={item.conversation_id === conversation ? 'page' : undefined}
              className={item.conversation_id === conversation ? 'rail-current' : undefined}
              title={item.title}
            >{item.title}</Link>
          </li>)}
        </ul>
      </>}
    </section>}

    {!!tools.length && <section className="rail-tools">
      <button type="button" className="rail-disclosure" aria-expanded={showAdvanced}
        onClick={() => setShowAdvanced(value => !value)}>
        <span className="nav-icon" aria-hidden="true">{showAdvanced ? '▾' : '▸'}</span>
        {label('Advanced tools')}
      </button>
      {showAdvanced && <nav aria-label="Advanced tools">
        {tools.map(([path, text, icon]) =>
          <NavLink key={path} to={`/${path}`} onClick={onNavigate} title={collapsed ? text : undefined}>
            <span className="nav-icon" aria-hidden="true">{icon}</span>{label(text)}
          </NavLink>)}
      </nav>}
    </section>}

    {!collapsed && <div className="sidebar-note">
      Educational use<br /><span>Not for patient diagnosis or treatment.</span>
    </div>}
  </aside>;
}
