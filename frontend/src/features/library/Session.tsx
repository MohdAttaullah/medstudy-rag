import { createContext, useContext, useState, type FormEvent, type ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { Identity } from '../../types/documents';
interface Session { token: string; identity: Identity | null; setSession: (token: string, identity: Identity | null) => void }
const Context = createContext<Session>({ token: '', identity: null, setSession: () => undefined });
export function SessionProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState('');
  const [identity, setIdentity] = useState<Identity | null>(null);
  const queries = useQueryClient();
  return <Context.Provider value={{ token, identity, setSession: (next, user) => {
    queries.clear(); setToken(next); setIdentity(user);
  } }}>{children}</Context.Provider>;
}
export const useSession = () => useContext(Context);
export function AccessGate({ children }: { children: ReactNode }) {
  const session = useSession();
  const [key, setKey] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  async function login(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const identity = await api<Identity>(key.trim(), '/auth/me');
      session.setSession(key.trim(), identity); setKey('');
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Sign-in failed.'); }
    finally { setBusy(false); }
  }
  if (!session.identity) return <section className="panel access-panel">
    <h2>Development workspace access</h2><p>Enter your local access key to view and manage documents. This is development authentication.</p>
    <form onSubmit={login}><label htmlFor="access-key">Access key</label>
      <input id="access-key" type="password" autoComplete="off" required value={key} onChange={event => setKey(event.target.value)} />
      {error && <p role="alert" className="error">{error}</p>}
      <button disabled={busy}>{busy ? 'Signing in…' : 'Open workspace'}</button>
    </form><p className="muted">The key stays in this tab's memory and clears on refresh or sign-out.</p>
  </section>;
  return <><div className="session-bar"><span>{session.identity.display_name} · {session.identity.role}</span>
    <button className="secondary" onClick={() => session.setSession('', null)}>Sign out</button></div>{children}</>;
}
