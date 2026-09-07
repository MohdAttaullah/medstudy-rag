import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import { AccessGate, useSession } from '../library/Session';
import './settings.css';

type Value = string | number | boolean | null;
export interface Setting {
  key: string; section: string; display_name: string; description: string;
  effective_value: Value; current_value: Value; desired_value: Value; default_value: Value;
  value_type: string; allowed_values: Value[]; bounds: Record<string, number>;
  lifecycle_class: string; scope: string; editable: boolean; impact_description: string;
  requires_confirmation: boolean; status: string;
}
interface Catalog { revision: number; effective_fingerprint: string; settings: Setting[];
  model_registry: { id: string; provider: string; model_id: string; configured: boolean }[] }
interface Change { key: string; old_value: Value; new_value: Value; lifecycle_class: string; impact_description: string }
interface Preview { revision: number; preview_token: string; changes: Change[]; result: string; requires_confirmation: boolean }
interface Revision { revision: number; actor_id: string; timestamp: string; reason: string; result: string; changes: Change[] }
const show = (value: Value) => value === null ? 'Not selected' : String(value);
const human = (value: string) => value.replaceAll('_', ' ');

export function SettingsPage() {
  return <><p className="eyebrow">ADMINISTRATION</p><h1>Settings</h1>
    <p className="intro">Versioned policy for this tenant. Review the impact before applying a change.</p>
    <AccessGate><SettingsContent /></AccessGate></>;
}

export function SettingsContent() {
  const { token, identity } = useSession();
  const permitted = identity?.permissions.includes('settings:read');
  const queries = useQueryClient();
  const [section, setSection] = useState('All');
  const [search, setSearch] = useState('');
  const [editing, setEditing] = useState<Setting | null>(null);
  const [draft, setDraft] = useState<Value>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [reason, setReason] = useState('');
  const [message, setMessage] = useState('');
  const [historyOffset, setHistoryOffset] = useState(0);
  const editHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { if (editing) editHeading.current?.focus(); }, [editing]);
  const catalog = useQuery({ queryKey: ['settings'], queryFn: () => api<Catalog>(token, '/settings'), enabled: !!permitted });
  const history = useQuery({ queryKey: ['settings-history', historyOffset], queryFn: () => api<Revision[]>(token, '/settings/history?limit=20&offset=' + historyOffset), enabled: !!permitted });
  const check = useMutation({ mutationFn: () => api<Preview>(token, '/settings/preview', { method: 'POST', body: JSON.stringify({ expected_revision: catalog.data!.revision, changes: [{ key: editing!.key, value: draft }] }) }),
    onSuccess: value => { setPreview(value); setConfirmed(false); } });
  const apply = useMutation({ mutationFn: () => api<Revision>(token, '/settings/changes', { method: 'POST', body: JSON.stringify({ expected_revision: preview!.revision, changes: [{ key: editing!.key, value: draft }], preview_token: preview!.preview_token, confirmed, reason }) }),
    onSuccess: result => { setMessage(`Revision ${result.revision}: ${human(result.result)}. ${result.result === 'ACTIVE' ? 'Subsequent requests use this policy.' : 'The effective configuration remains unchanged. An operator must complete the established rebuild and activation workflow outside this UI.'}`); setEditing(null); setPreview(null); void queries.invalidateQueries({queryKey:['settings']}); void queries.invalidateQueries({queryKey:['settings-history']}); } });
  function start(item: Setting) { setEditing(item); setDraft(item.desired_value); setPreview(null); setConfirmed(false); setReason(''); setMessage(''); check.reset(); apply.reset(); }
  function change(value: Value) { setDraft(value); setPreview(null); setConfirmed(false); check.reset(); apply.reset(); }
  function submit(event: FormEvent) { event.preventDefault(); check.mutate(); }
  if (!permitted) return <section className="notice"><h2>Administrator access required</h2><p>Your role cannot read or change configuration. Ask and source access retain their existing permissions.</p></section>;
  if (catalog.isPending) return <p role="status">Loading settings…</p>;
  if (catalog.isError) return <p role="alert">{catalog.error.message} <button onClick={() => void catalog.refetch()}>Retry settings</button></p>;
  const sections = ['All', ...new Set(catalog.data.settings.map(item => item.section))];
  const visible = catalog.data.settings.filter(item => (section === 'All' || item.section === section) && `${item.display_name} ${item.description}`.toLowerCase().includes(search.toLowerCase()));
  return <div className="settings-workspace">
    <p>Tenant policy revision <strong>{catalog.data.revision}</strong>. Shared service settings are managed externally.</p>
    <label htmlFor="settings-search">Find a setting</label><input id="settings-search" type="search" value={search} onChange={e => setSearch(e.target.value)} />
    <nav aria-label="Settings sections" className="settings-sections">{sections.map(name => <button className="secondary" key={name} aria-pressed={section === name} onClick={() => setSection(name)}>{name}</button>)}</nav>
    {message && <p role="status" className="notice">{message}</p>}
    {editing && <section className="panel settings-editor" aria-labelledby="edit-title">
      <h2 id="edit-title" ref={editHeading} tabIndex={-1}>Edit {editing.display_name}</h2><p>{editing.impact_description}</p>
      <form onSubmit={submit}>
        <label htmlFor="setting-value">Desired value</label>
        {editing.allowed_values.length > 0 ? <select id="setting-value" value={JSON.stringify(draft)} onChange={e => change(JSON.parse(e.target.value) as Value)}>{editing.allowed_values.map(v => <option key={JSON.stringify(v)} value={JSON.stringify(v)}>{show(v)}</option>)}</select>
          : editing.value_type === 'boolean' ? <select id="setting-value" value={String(draft)} onChange={e => change(e.target.value === 'true')}><option value="true">true</option><option value="false">false</option></select>
          : <input id="setting-value" required type={['integer','number'].includes(editing.value_type) ? 'number' : 'text'} step={editing.value_type === 'integer' ? 1 : 'any'} min={editing.bounds.minimum} max={editing.bounds.maximum} value={draft === null ? '' : String(draft)} onChange={e => change(editing.value_type === 'string' ? e.target.value : e.target.value === '' ? null : Number(e.target.value))} />}
        <p>Effective: {show(editing.effective_value)} · Default: {show(editing.default_value)}</p>
        <label htmlFor="setting-reason">Reason (optional; do not enter credentials)</label><input id="setting-reason" maxLength={500} value={reason} onChange={e => setReason(e.target.value)} />
        <div className="settings-actions"><button disabled={check.isPending || apply.isPending}>Preview change</button><button type="button" className="secondary" disabled={apply.isPending} onClick={() => setEditing(null)}>Cancel</button></div>
      </form>
      {check.isError && <p role="alert">{check.error.message}</p>}
      {preview && <div className="notice"><h3>Review change impact</h3><p>{human(preview.result)}</p>{preview.changes.map(item => <p key={item.key}>{show(item.old_value)} → {show(item.new_value)}. {item.impact_description}</p>)}
        <label className="settings-confirm"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />I confirm this change and its impact.</label>
        <button disabled={!confirmed || apply.isPending} onClick={() => apply.mutate()}>{apply.isPending ? 'Applying…' : 'Apply confirmed change'}</button>
      </div>}
      {apply.isError && <p role="alert">{apply.error.message} <button onClick={async () => { setPreview(null); const updated = await catalog.refetch(); if (updated.data && editing) setEditing(updated.data.settings.find(item => item.key === editing.key) ?? null); }}>Reload current revision</button></p>}
    </section>}
    <div className="settings-cards">{visible.map(item => <article className="panel setting-card" key={item.key}>
      <p className="eyebrow">{item.section} · {item.scope}</p><h2>{item.display_name}</h2><span className="badge">{human(item.lifecycle_class)}</span><p>{item.description}</p>
      <dl><dt>Effective</dt><dd>{item.lifecycle_class === 'SECRET_MANAGED_EXTERNALLY' ? item.effective_value ? 'Configured' : 'Not configured' : show(item.effective_value)}</dd>
        {item.status !== 'EFFECTIVE' && <><dt>Desired / pending</dt><dd>{show(item.desired_value)} — {human(item.status)}</dd></>}
        <dt>Default</dt><dd>{show(item.default_value)}</dd></dl>
      <p className="muted">{item.impact_description}</p>{item.editable ? <button onClick={() => start(item)}>Edit {item.display_name}</button> : <p>Read only</p>}
    </article>)}</div>{visible.length === 0 && <p>No settings match this filter.</p>}
    <section className="panel"><h2>Approved provider models</h2><p>Choices come from the server’s approved registry. Credentials are managed outside this workspace.</p>{catalog.data.model_registry.length ? <ul>{catalog.data.model_registry.map(model => <li key={model.id}>{model.provider} / {model.model_id}: credential {model.configured ? 'configured' : 'not configured'}</li>)}</ul> : <p>No provider model is approved. Optional providers may remain unconfigured.</p>}</section>
    <section className="panel"><h2>Settings history</h2><p>Immutable change records. Pending rebuild records are proposals; they do not certify index activation.</p>
      {history.isPending ? <p>Loading history…</p> : history.isError ? <p role="alert">{history.error.message}</p> : history.data.length === 0 ? <p>No policy changes on this page.</p> : <ol className="settings-history">{history.data.map(row => <li key={row.revision}><h3>Revision {row.revision} · {human(row.result)}</h3><p>{new Date(row.timestamp).toLocaleString()} · Principal {row.actor_id}</p>{row.reason && <p>{row.reason}</p>}{row.changes.map(change => <p key={change.key}>{change.key}: {show(change.old_value)} → {show(change.new_value)}<br />{change.impact_description}</p>)}</li>)}</ol>}
      <div className="settings-actions"><button className="secondary" disabled={historyOffset === 0} onClick={() => setHistoryOffset(Math.max(0, historyOffset - 20))}>Newer changes</button><button className="secondary" disabled={!history.data || history.data.length < 20} onClick={() => setHistoryOffset(historyOffset + 20)}>Older changes</button></div>
    </section>
  </div>;
}
