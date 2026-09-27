import { useState } from 'react';
import { RetrievalInspector } from './features/retrieval/RetrievalInspector';
import { ChunkInspector } from './features/chunking/ChunkInspector';
import { IndexInspector } from './features/embedding/IndexInspector';
import { useQuery } from '@tanstack/react-query';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { getReadiness } from './api/health';
import { Library } from './features/library/Library';
import { DocumentDetails } from './features/documents/DocumentDetails';
import { AccessGate, SessionProvider } from './features/library/Session';
import { Jobs } from './features/operations/Jobs';
import { ParseInspector } from './features/parsing/ParseInspector';
import { SettingsPage } from './features/settings/Settings';
import { Ask } from './features/ask/Ask';
import { Sidebar } from './features/navigation/Sidebar';
import { AccountMenu } from './features/navigation/AccountMenu';
import { Icon } from './features/navigation/icons';

function PlannedPage({ title, description }: { title: string; description: string }) {
  return <><p className="eyebrow">WORKSPACE FOUNDATION</p><h1>{title}</h1>
    <p className="intro">{description}</p><section className="notice">
      <div><h2>Planned capability</h2><p>This page is a navigation shell. Its workflow is not implemented yet.</p></div>
    </section></>;
}

function Operations() {
  const health = useQuery({ queryKey: ['readiness'], queryFn: getReadiness, refetchInterval: 30000 });
  return <><p className="eyebrow">SYSTEM STATUS</p><h1>Operations</h1>
    <p className="intro">Live connectivity checks for the development environment.</p>
    <section className="panel" aria-live="polite">
      {health.isPending ? <p>Checking services…</p> : health.isError ?
        <p role="alert">The API is unavailable. Start the backend to view service health.</p> : <>
          <h2>{health.data.status === 'ready' ? 'Infrastructure ready' : 'Infrastructure not ready'}</h2>
          <ul className="service-list">{Object.entries(health.data.dependencies).map(([name, available]) =>
            <li key={name}><span>{name.replaceAll('_', ' ')}</span><strong>{available ? 'Connected' : 'Unavailable'}</strong></li>)}</ul>
        </>}
      <button className="secondary" onClick={() => void health.refetch()} disabled={health.isFetching}>Refresh status</button>
    </section><AccessGate><Jobs /></AccessGate></>;
}

/**
 * What the header says you are looking at.
 *
 * Deliberately a short, human noun — the name of the page, not of the milestone that built it. The
 * header previously carried a `M10 / Configuration` badge, which is an internal development
 * marker from ADR-015 and means nothing to a clinician, a student or a buyer. Engineering context
 * of that kind belongs on the surfaces that exist for engineers, not in the product shell.
 */
const PAGES: [RegExp, string][] = [
  [/^\/ask/, 'Ask'],
  [/^\/library/, 'Library'],
  [/^\/documents/, 'Library'],
  [/^\/settings/, 'Settings'],
  [/^\/retrieval/, 'Retrieval inspector'],
  [/^\/evaluations/, 'Evaluations'],
  [/^\/operations/, 'Operations'],
  [/^\/audit/, 'Audit history'],
  [/^\/chunk-runs/, 'Chunk inspector'],
  [/^\/index-runs/, 'Index inspector'],
];

function Workspace() {
  // On a narrow screen the rail becomes a drawer over the content; on a wide one this does
  // nothing, because the rail is always in the layout there.
  const [drawer, setDrawer] = useState(false);
  const { pathname } = useLocation();
  const page = PAGES.find(([match]) => match.test(pathname))?.[1] ?? 'Workspace';

  return <div className="workspace">
    <a className="skip-link" href="#main">Skip to content</a>
    <Sidebar open={drawer} onNavigate={() => setDrawer(false)} />
    {drawer && <button type="button" className="drawer-scrim" aria-label="Close navigation"
      onClick={() => setDrawer(false)} />}
    <div className="content">
      <header>
        <button type="button" className="drawer-toggle" aria-expanded={drawer}
          aria-controls="main" onClick={() => setDrawer(value => !value)}>
          <Icon name="menu" />
          <span className="visually-hidden">Navigation</span>
        </button>
        <span className="header-context">{page}</span>
        <div className="header-end"><AccountMenu /></div>
      </header>
      <main id="main" tabIndex={-1}><Routes>
        <Route path="/" element={<Navigate to="/ask" replace />} />
        <Route path="/ask" element={<Ask />} />
        <Route path="/library" element={<Library />} />
        <Route path="/chunk-runs/:runId" element={<ChunkInspector />} />
        <Route path="/index-runs/:runId" element={<IndexInspector />} />
        <Route path="/documents/:id" element={<DocumentDetails />} />
        <Route path="/documents/:id/versions/:versionId/parse/:runId" element={<ParseInspector />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/evaluations" element={<PlannedPage title="Evaluations" description="Measure retrieval, citation support, and appropriate abstention independently." />} />
        <Route path="/retrieval" element={<RetrievalInspector />} />
        <Route path="/operations" element={<Operations />} />
        <Route path="/audit" element={<PlannedPage title="Audit history" description="Inspect authorized activity and reproducible answer provenance." />} />
        <Route path="*" element={<PlannedPage title="Page not found" description="Choose a workspace page from the navigation." />} />
      </Routes></main>
      <footer>Grounded in sources. Designed to abstain.</footer>
    </div>
  </div>;
}

export function App() { return <SessionProvider><Workspace /></SessionProvider>; }
