import { RetrievalInspector } from './features/retrieval/RetrievalInspector';
import { ChunkInspector } from './features/chunking/ChunkInspector';
import { IndexInspector } from './features/embedding/IndexInspector';
import { useQuery } from '@tanstack/react-query';
import { NavLink, Navigate, Route, Routes } from 'react-router-dom';
import { getReadiness } from './api/health';
import { Library } from './features/library/Library';
import { DocumentDetails } from './features/documents/DocumentDetails';
import { AccessGate, SessionProvider } from './features/library/Session';
import { Jobs } from './features/operations/Jobs';
import { ParseInspector } from './features/parsing/ParseInspector';
import { Ask } from './features/ask/Ask';

const navigation = [
  ['ask', 'Ask'], ['library', 'Library'], ['settings', 'Settings'],
  ['retrieval', 'Retrieval inspector'], ['evaluations', 'Evaluations'], ['operations', 'Operations'], ['audit', 'Audit'],
] as const;

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
      <button onClick={() => void health.refetch()} disabled={health.isFetching}>Refresh status</button>
    </section><AccessGate><Jobs /></AccessGate></>;
}

function Workspace() {
  return <div className="workspace">
    <a className="skip-link" href="#main">Skip to content</a>
    <aside className="sidebar"><div className="brand"><span className="brand-mark" aria-hidden="true">+</span>
      <div>MEDICAL<span>Evidence workspace</span></div></div>
      <nav aria-label="Main navigation">{navigation.map(([path, label]) =>
        <NavLink key={path} to={`/${path}`}>{label}</NavLink>)}</nav>
      <div className="sidebar-note">Educational use<br /><span>Not for patient diagnosis or treatment.</span></div>
    </aside>
    <div className="content"><header><span>Knowledge workspace</span><span className="badge">M6 / Evidence</span></header>
      <main id="main" tabIndex={-1}><Routes>
        <Route path="/" element={<Navigate to="/ask" replace />} />
        <Route path="/ask" element={<Ask />} />
        <Route path="/library" element={<Library />} />
        <Route path="/chunk-runs/:runId" element={<ChunkInspector />} />
        <Route path="/index-runs/:runId" element={<IndexInspector />} />
        <Route path="/documents/:id" element={<DocumentDetails />} />
        <Route path="/documents/:id/versions/:versionId/parse/:runId" element={<ParseInspector />} />
        <Route path="/settings" element={<PlannedPage title="Settings" description="Versioned configuration for chunking, retrieval, models, and evidence policy." />} />
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
