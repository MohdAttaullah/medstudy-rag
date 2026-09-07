import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from '../../App';
import type { Setting } from './Settings';

const setting: Setting = { key:'retrieval.rrf_k', section:'Retrieval', display_name:'retrieval / rrf k', description:'Rank fusion policy', effective_value:60,current_value:60,desired_value:60,default_value:60,value_type:'integer',allowed_values:[],bounds:{minimum:1,maximum:1000},lifecycle_class:'RUNTIME_SAFE',scope:'TENANT',editable:true,impact_description:'Applies to subsequent tenant requests.',requires_confirmation:true,status:'EFFECTIVE' };
let reader=false; let fail=false; let revision=0; let pending=false; let requests: {path:string;body:Record<string,unknown>}[]=[];
let item: Setting;
beforeEach(() => {
  reader=false; fail=false; revision=0; pending=false; requests=[]; item={...setting};
  vi.stubGlobal('fetch',vi.fn(async (path:string,init?:RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    if(init?.body) requests.push({path,body});
    let data: unknown; let status=200;
    if(path.includes('/auth/me')) data={role:reader?'reader':'admin',display_name:'Operator',permissions:reader?['ask:submit']:['settings:read','settings:write']};
    else if(path.includes('/settings/history')) data=[];
    else if(path.includes('/settings/preview')) {data={revision,preview_token:'a'.repeat(64),changes:[{key:item.key,old_value:60,new_value:50,impact_description:item.impact_description}],requires_confirmation:true,result:pending?'PENDING_REBUILD':'ACTIVE'};if(fail){status=422;data={error:{message:'Values violate typed bounds.',code:'SETTING_VALIDATION_FAILED'}};}}
    else if(path.includes('/settings/changes')) {revision++;data={revision,result:pending?'PENDING_REBUILD':'ACTIVE'};}
    else data={revision,effective_fingerprint:'fingerprint',settings:[item,{...setting,key:'ask.requires_verified_pass',section:'Safety',display_name:'Verified answer required',editable:false,effective_value:true,lifecycle_class:'IMMUTABLE'}],model_registry:[]};
    return new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
  }));
});
afterEach(() => vi.unstubAllGlobals());
async function open() {
  render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}})}><MemoryRouter initialEntries={['/settings']}><App /></MemoryRouter></QueryClientProvider>);
  fireEvent.change(screen.getByLabelText('Access key'),{target:{value:'local-test'}});
  fireEvent.click(screen.getByRole('button',{name:'Open workspace'}));
}
it('does not fetch settings for a reader', async()=>{reader=true;await open();await screen.findByText('Administrator access required');expect(vi.mocked(fetch).mock.calls.some(call=>String(call[0]).includes('/settings'))).toBe(false);});
it('filters server-provided sections and keeps invariants read only',async()=>{await open();await screen.findByText('Verified answer required');fireEvent.click(screen.getByRole('button',{name:'Safety'}));expect(screen.queryByRole('button',{name:'Edit retrieval / rrf k'})).not.toBeInTheDocument();expect(screen.queryByRole('button',{name:'Edit Verified answer required'})).not.toBeInTheDocument();});
it('requires preview and confirmation and submits no lifecycle or tenant',async()=>{await open();fireEvent.click(await screen.findByRole('button',{name:'Edit retrieval / rrf k'}));fireEvent.change(screen.getByLabelText('Desired value'),{target:{value:'50'}});fireEvent.click(screen.getByRole('button',{name:'Preview change'}));const button=await screen.findByRole('button',{name:'Apply confirmed change'});expect(button).toBeDisabled();fireEvent.click(screen.getByLabelText('I confirm this change and its impact.'));fireEvent.click(button);await screen.findByText(/Revision 1: ACTIVE/);const body=requests.find(r=>r.path.includes('/changes'))!.body;expect(Object.keys(body).sort()).toEqual(['changes','confirmed','expected_revision','preview_token','reason']);});
it('invalidates preview when the proposed value changes',async()=>{await open();fireEvent.click(await screen.findByRole('button',{name:'Edit retrieval / rrf k'}));fireEvent.click(screen.getByRole('button',{name:'Preview change'}));await screen.findByRole('button',{name:'Apply confirmed change'});fireEvent.change(screen.getByLabelText('Desired value'),{target:{value:'55'}});expect(screen.queryByRole('button',{name:'Apply confirmed change'})).not.toBeInTheDocument();});
it('shows backend validation without applying',async()=>{fail=true;await open();fireEvent.click(await screen.findByRole('button',{name:'Edit retrieval / rrf k'}));fireEvent.click(screen.getByRole('button',{name:'Preview change'}));expect(await screen.findByRole('alert')).toHaveTextContent('Values violate typed bounds.');expect(requests.some(r=>r.path.includes('/changes'))).toBe(false);});
it('states pending rebuild is not effective',async()=>{pending=true;item={...setting,key:'chunking.child_target_tokens',section:'Chunking',lifecycle_class:'RECHUNK_REINDEX_REQUIRED',status:'PENDING_REBUILD',desired_value:320,effective_value:384};await open();await screen.findByText(/320.*PENDING REBUILD/);expect(screen.getByText('384')).toBeInTheDocument();fireEvent.click(screen.getByRole('button',{name:'Edit retrieval / rrf k'}));fireEvent.click(screen.getByRole('button',{name:'Preview change'}));await screen.findByRole('button',{name:'Apply confirmed change'});fireEvent.click(screen.getByLabelText('I confirm this change and its impact.'));fireEvent.click(screen.getByRole('button',{name:'Apply confirmed change'}));await waitFor(()=>expect(screen.getByRole('status')).toHaveTextContent('effective configuration remains unchanged'));});
