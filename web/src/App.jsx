import React, { useEffect, useState } from 'react'
const post = (p, b) => fetch(p, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(b)}).then(r=>r.json())
export function pending(incidents){ return incidents.filter(i=>i.proposals?.some(p=>p.status==='pending')) }
export default function App(){
  const [data,setData]=useState({incidents:[],audit:[],emr_writes:[],calls:[]})
  const load=()=>fetch('/api/state').then(r=>r.json()).then(setData).catch(()=>{})
  useEffect(()=>{load(); const t=setInterval(load,3000); return ()=>clearInterval(t)},[])
  const act=async(p,b)=>{ const d=await post(p,b); if(!d.error) setData(d) }
  return <main style={{fontFamily:'system-ui',maxWidth:1100,margin:'0 auto',padding:20}}>
    <h1>Care Ops — Operator Console</h1>
    <p>{data.incidents.length} incidents · {pending(data.incidents).length} pending approvals · {data.emr_writes.length} EMR writes · {data.calls.length} voice calls (mock)</p>
    <h2>Open incidents &amp; pending approvals</h2>
    {data.incidents.map(i=><section key={i.id} style={{border:'1px solid #ccc',borderRadius:8,padding:12,marginBottom:10}}>
      <b>{i.kind}</b> — {i.id} · visit {i.visit_id} · <span>{i.status}</span>
      {(i.proposals||[]).map(p=><div key={p.id} style={{marginTop:6}}>→ {p.caregiver_id} (score {p.score}) [{p.status}]
        {p.status==='pending' && <>
          <button onClick={()=>act('/api/approve',{proposal_id:p.id})}>Approve &amp; write EMR</button>
          <button onClick={()=>act('/api/reject',{proposal_id:p.id})}>Reject</button>
          <button onClick={()=>{const c=prompt('Override caregiver id'); if(c) act('/api/override',{proposal_id:p.id,caregiver_id:c})}}>Override…</button></>}
      </div>)}
    </section>)}
    <h2>Agent actions (audit)</h2>
    <ul>{data.audit.map(a=><li key={a.id}><code>{a.actor}</code> {a.action} {a.ref} <small>{a.detail}</small></li>)}</ul>
    <h2>EMR writes (approved only)</h2>
    <ul>{data.emr_writes.map(w=><li key={w.id}>{w.id} by {w.approved_by}: {w.payload}</li>)}</ul>
  </main>
}
