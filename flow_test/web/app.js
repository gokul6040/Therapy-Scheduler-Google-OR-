const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.get('token')) {
  sessionStorage.setItem('flowAdminToken', fragment.get('token'));
  history.replaceState(null, '', location.pathname);
}
let token = sessionStorage.getItem('flowAdminToken') || '';
const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let latest;
let busy = false;
const selectedRoster = new Set();
let rosterInitialized = false;
const today = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Kolkata', year:'numeric', month:'2-digit', day:'2-digit'}).format(new Date());
$('date').value = today;

async function api(path, body) {
  const response = await fetch((window.flowTestMount ? '/test' : '') + path, {method:body ? 'POST' : 'GET', headers:{'Authorization':`Bearer ${token}`, ...(body ? {'Content-Type':'application/json'} : {})}, ...(body ? {body:JSON.stringify(body)} : {})});
  const result = await response.json();
  if (window.flowTestMount && response.status === 401) {
    $('testLogin').hidden = false;
    $('mode').textContent = 'Admin key required';
    throw new Error('Enter your test admin key to open this workspace.');
  }
  if (!response.ok) throw new Error(result.error || 'Request failed');
  return result;
}
function notice(text, error = false) { $('notice').textContent = text; $('notice').classList.toggle('error', error); }
function table(headers, rows) {
  return `<div class="table-wrap"><table><thead><tr>${headers.map(h=>`<th>${escape(h)}</th>`).join('')}</tr></thead><tbody>${rows.length ? rows.map(row=>`<tr>${row.map(cell=>`<td>${cell}</td>`).join('')}</tr>`).join('') : `<tr><td colspan="${headers.length}">Nothing yet.</td></tr>`}</tbody></table></div>`;
}
function button(label, action, attributes='', style='secondary small') {
  return `<button class="${style}" data-action="${action}" ${attributes}>${escape(label)}</button>`;
}
async function refresh() {
  latest = await api(`/api/state?date=${encodeURIComponent($('date').value)}`);
  if (!rosterInitialized) {
    latest.scenario.patients.filter(p=>p.initial_roster).forEach(p=>selectedRoster.add(p.code));
    rosterInitialized = true;
  }
  const preview = latest.mode.startsWith('PREVIEW');
  $('mode').textContent = latest.mode;
  $('process').hidden = !preview;
  $('replyHelp').textContent = preview ? 'Use the preview buttons to exercise the same response worker. No WhatsApp requests are made.' : `${latest.message_mode==='session'?'No template needed: each coworker must send START to the test WhatsApp number first. Offers use real Yes / Cannot attend / Help buttons within the 24-hour response window. ':''}A Cannot attend button asks for VISIT <reference> or DAY <reference>. HELP/RESCHEDULE <reference> creates an inbox task. Replies and delivery receipts appear automatically.`;
  $('inputs').innerHTML = latest.scenario.patients.map(p=>`<article class="card"><h3><label><input type="checkbox" data-roster="${escape(p.code)}" ${selectedRoster.has(p.code)?'checked':''}> ${escape(p.code)}</label></h3><p>${escape(p.name)}</p><p>${p.requirements.map(r=>`${escape(r.therapy)} ×${r.weekly}/week · ${escape(r.therapist)}`).join('<br>')}</p><p class="muted">Recipient: ${escape(latest.recipients[p.code] || (preview?'preview only':'not configured'))}${!preview && latest.message_mode==='session' ? '<br>'+escape(latest.windows[p.code]?.open?'Ready · 24-hour window open':'Coworker must send START first') : ''}</p></article>`).join('');
  $('appointments').innerHTML = table(['Child','Therapy / therapist','Time','Status','Admin action'], latest.appointments.map(a=>[
    escape(a.patient), `${escape(a.therapy)}<br><span class="muted">${escape(a.therapist)}</span>`, escape(a.time), escape(a.status),
    a.status==='PROPOSED' ? button('Offer this visit','offer',`data-id="${a.id}"`) : a.status==='CONFIRMED' ? `<div class="actions">${button('Completed','attendance',`data-id="${a.id}" data-status="COMPLETED"`)}${button('No-show','attendance',`data-id="${a.id}" data-status="NO_SHOW"`)}</div>` : ''
  ]));
  $('offers').innerHTML = latest.offers.length ? latest.offers.map(o=>`<article class="card"><h3>${escape(o.patient)} · ${escape(o.state)}</h3><p class="ref">${escape(o.token)}</p><p class="muted">${o.state==='PENDING' ? (o.expires_at ? 'Reply deadline '+escape(new Date(o.expires_at).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'}))+' IST' : 'Waiting for send') : escape(o.state==='CONFIRMED'?'Visit confirmed':'Offer closed; see appointments and inbox')}</p><div class="actions">${preview && ['PENDING','CONFIRMED'].includes(o.state) ? ['YES','NO','VISIT','DAY','HELP','RESCHEDULE'].map(action=>button(action,'reply',`data-token="${o.token}" data-reply="${action}"`)).join('') : ''}${['PENDING','EXPIRED'].includes(o.state)?button('Admin release','release',`data-token="${o.token}"`,'danger small'):''}</div></article>`).join('') : '<p class="muted">Publish a draft to create offers.</p>';
  $('progress').innerHTML = table(['Child','Therapy','Target','Completed','Booked','Remaining'], latest.progress.map(p=>[p.patient,p.therapy,p.target,p.completed,p.booked,p.remaining].map(escape)));
  $('inbox').innerHTML = latest.inbox.length ? latest.inbox.map(i=>`<div class="inbox-item"><strong>${escape(i.patient || 'Unmatched')} · ${escape(i.reason)}</strong><p>${escape(i.text)}</p>${i.resolved ? '<span class="muted">Resolved</span>' : button('Mark resolved','resolve',`data-id="${i.id}"`)}</div>`).join('') : '<p class="muted">No staff requests.</p>';
  $('jobs').innerHTML = table(['ID','Type','State','Delivery / issue'], latest.jobs.map(j=>[escape(j.id),escape(j.kind),escape(j.status),escape(j.delivery || j.error || '—')]));
  $('audit').innerHTML = table(['Action','Record','Time (IST)'], latest.audit.map(a=>[escape(a.action),escape(a.entity),escape(new Date(a.time).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'}))]));
  if (window.flowTestUpdated) window.flowTestUpdated(latest);
  if (window.flowTestMount) $('testLogin').hidden = true;
}
async function run(path, body) {
  if (busy) return;
  busy = true;
  document.querySelectorAll('button').forEach(b=>b.disabled=true);
  try {
    const result = await api(path,body);
    if (latest?.mode.startsWith('PREVIEW') && path!='/api/process') await api('/api/process',{});
    await refresh();
    notice(result.added !== undefined ? `Draft saved: ${result.added} new sessions. Weekly unmet demand: ${result.unmet.map(u=>`${u.missing} (${u.reason})`).join(', ') || 'none'}.` : 'Saved. The queue and weekly progress are updated.');
    if (window.flowTestMount && path === '/api/generate') testView('schedule');
  } catch(error) { notice(error.message,true); }
  finally {busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);}
}
$('generate').onclick = ()=>run('/api/generate',{date:$('date').value, patients:[...selectedRoster]});
$('publish').onclick = ()=>run('/api/publish',{date:$('date').value});
$('process').onclick = ()=>run('/api/process',{});
$('refresh').onclick = ()=>refresh().catch(e=>notice(e.message,true));
$('date').onchange = ()=>refresh().catch(e=>notice(e.message,true));
document.addEventListener('change', event=>{const code=event.target.dataset.roster;if(code){event.target.checked?selectedRoster.add(code):selectedRoster.delete(code);}});
document.addEventListener('click', event=>{
  const b=event.target.closest('button[data-action]');if(!b)return;
  const d=b.dataset;
  if(d.action==='offer')run('/api/publish',{date:$('date').value,appointment_id:Number(d.id)});
  if(d.action==='reply')run('/api/simulate',{token:d.token,action:d.reply});
  if(d.action==='release')run('/api/release',{token:d.token});
  if(d.action==='attendance')run('/api/attendance',{id:Number(d.id),status:d.status});
  if(d.action==='resolve')run('/api/resolve',{id:Number(d.id)});
});
if (window.flowTestMount) {
  $('testLogin').onsubmit = async event => {
    event.preventDefault();
    token = $('testAdminKey').value.trim();
    sessionStorage.setItem('flowAdminToken', token);
    try { await refresh(); $('testLogin').hidden = true; $('testAdminKey').value = ''; notice('Test workspace ready.'); }
    catch (error) { notice(error.message, true); }
  };
}
refresh().then(()=>notice('Ready. Select a Monday–Saturday date and generate the daily draft.')).catch(e=>notice(e.message,true));
setInterval(()=>{if(!busy && token)refresh().catch(()=>{});},5000);
