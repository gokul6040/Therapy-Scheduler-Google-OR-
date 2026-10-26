/* Shared drawer/Operations components. Enabled only for the isolated test workspace. */
window.therapyWhatsapp = (() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
  const preview = data => data.mode.startsWith('PREVIEW');
  const label = value => String(value || 'Not reported').toLowerCase().replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase());
  const action = (title, kind, attrs = '', disabled = false) => `<button type="button" class="secondary-button compact-button" data-wa-action="${kind}" ${attrs} ${disabled ? 'disabled' : ''}>${esc(title)}</button>`;
  const table = (headers, rows) => `<div class="wa-table"><table class="data-table"><thead><tr>${headers.map(h => `<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.length ? rows.map(row => `<tr>${row.map(cell => `<td>${cell}</td>`).join('')}</tr>`).join('') : `<tr><td colspan="${headers.length}">Nothing yet.</td></tr>`}</tbody></table></div>`;
  const post = (api, path, body) => api(path, {method:'POST', body:JSON.stringify(body)});
  const readiness = (data, patient) => preview(data) ? 'Preview — no messages sent' : !data.recipients[patient] ? 'Recipient not configured' : data.message_mode === 'session' && !data.windows[patient]?.open ? `${data.recipients[patient]} · offer will wait for a parent message` : `${data.recipients[patient]} · ready to receive`;
  const ready = (data, patient) => preview(data) || Boolean(data.recipients[patient]);

  function offerCard(offer, data, scope) {
    const fieldId = `wa-reply-${scope}-${offer.token}`;
    const reply = {YES:'Yes — visit confirmed', NO:offer.state === 'DECLINED' ? 'Unavailable for the whole day' : 'Cannot attend received — booking still reserved; staff review', VISIT:'Cannot attend this visit', DAY:'Unavailable that day', HELP:'Help requested — see inbox', RESCHEDULE:'Reschedule requested — see inbox'}[offer.last_reply] || (['QUEUED','WAITING_FOR_CONTACT','PROCESSING'].includes(offer.send_status) ? 'Offer not sent yet' : 'Awaiting parent reply');
    const deadline = offer.expires_at ? `Reply deadline: ${new Date(offer.expires_at).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'})} IST` : offer.state === 'PENDING' ? 'The reply window starts when this offer is sent.' : '';
    const simulate = preview(data) && ['PENDING','CONFIRMED'].includes(offer.state)
      ? `<div class="wa-preview-reply"><div class="wa-reply-field"><label for="${esc(fieldId)}">Preview reply for ${esc(offer.patient)}</label><select id="${esc(fieldId)}" data-wa-reply="${esc(offer.token)}">${[['YES','Yes'],['NO','Cannot attend — whole day'],['HELP','Help'],['DAY','Unavailable that day'],['RESCHEDULE','Request reschedule']].map(([value,title]) => `<option value="${value}">${title}</option>`).join('')}</select></div>${action('Simulate reply','simulate',`data-token="${esc(offer.token)}"`)}</div>` : '';
    return `<article class="wa-offer"><div class="wa-offer-heading"><strong>${esc(offer.patient)}</strong><span class="soft-pill">${esc(label(offer.state))}</span></div><dl class="wa-status-grid"><div><dt>Sending</dt><dd>${esc(label(offer.send_status))}</dd></div><div><dt>Delivery receipt</dt><dd>${esc(preview(data) ? 'Preview only' : label(offer.delivery))}</dd></div><div><dt>Parent reply</dt><dd>${esc(reply)}</dd></div></dl>${offer.send_error ? `<p class="${offer.send_status === 'WAITING_FOR_CONTACT' ? 'fine-print' : 'wa-error'}">${esc(offer.send_error)}</p>` : ''}${deadline ? `<p class="fine-print">${esc(deadline)}</p>` : ''}<small class="wa-reference">Reference: ${esc(offer.token)}</small>${offer.message_preview ? `<details><summary>View offered message</summary><pre>${esc(offer.message_preview)}</pre></details>` : ''}${simulate}</article>`;
  }

  function appointmentActions(item, data) {
    if (item.status === 'PROPOSED') return `${action(preview(data) ? 'Publish preview offer' : 'Publish WhatsApp offer','publish',`data-id="${item.id}"`,!ready(data,item.patient))}<small>${esc(readiness(data,item.patient))}</small>`;
    if (item.status === 'CONFIRMED') return `<div class="wa-actions">${action('Completed','attendance',`data-id="${item.id}" data-status="COMPLETED"`)}${action('No-show','attendance',`data-id="${item.id}" data-status="NO_SHOW"`)}</div>`;
    return `<span>${esc(['COMPLETED','NO_SHOW'].includes(item.status) ? label(item.status) : 'Attendance available after confirmation')}</span>`;
  }

  function controller(body, options, render, active, getDate, notice) {
    let data = null, busy = false, timer = null, signature = '';
    const valid = () => body.isConnected && active();
    async function refresh(force = false) {
      if (!valid()) return;
      try {
        const next = await options.api(`/api/state?date=${encodeURIComponent(getDate())}`);
        if (!valid()) return;
        const previousAppointments = data && JSON.stringify(data.appointments);
        data = next;
        const serialized = JSON.stringify(next);
        const editing = body.contains(document.activeElement) && document.activeElement.matches('input,select,textarea');
        if (force || (signature !== serialized && !editing)) {
          const selections = new Map([...body.querySelectorAll('[data-wa-reply]')].map(el => [el.dataset.waReply,el.value]));
          body.innerHTML = render(next);
          body.querySelectorAll('[data-wa-reply]').forEach(el => { if (selections.has(el.dataset.waReply)) el.value = selections.get(el.dataset.waReply); });
          signature = serialized;
        }
        if (!busy && previousAppointments && previousAppointments !== JSON.stringify(next.appointments)) await options.onChanged();
      } catch (error) { if (valid()) notice(error.message, true); }
    }
    async function mutate(button) {
      if (busy || !data) return;
      const kind = button.dataset.waAction;
      busy = true;
      body.querySelectorAll('button').forEach(el => el.disabled = true);
      try {
        let result;
        if (kind === 'publish') result = await post(options.api,'/api/publish',{date:getDate(), ...(button.dataset.id ? {appointment_id:Number(button.dataset.id)} : {})});
        if (kind === 'simulate') {
          const selected = [...body.querySelectorAll('[data-wa-reply]')].find(el => el.dataset.waReply === button.dataset.token);
          result = await post(options.api,'/api/simulate',{token:button.dataset.token,action:selected.value});
        }
        if (kind === 'attendance') result = await post(options.api,'/api/attendance',{id:Number(button.dataset.id),status:button.dataset.status});
        if (kind === 'resolve') result = await post(options.api,'/api/resolve',{id:Number(button.dataset.id)});
        if (preview(data)) await post(options.api,'/api/process',{});
        await refresh(true);
        if (valid()) notice(kind === 'publish' ? `${result.offers} visit offer(s) ${preview(data) ? 'published in preview. No messages sent.' : 'queued for WhatsApp.'}` : 'Saved. Replies, attendance and weekly progress are updated.');
        if (kind !== 'resolve') await options.onChanged();
      } catch (error) { if (valid()) notice(error.message, true); }
      finally {
        busy = false;
        if (valid()) { signature = ''; await refresh(true); }
      }
    }
    body.addEventListener('click', event => {
      const button = event.target.closest('[data-wa-action]');
      if (button && body.contains(button)) mutate(button);
    });
    function poll() {
      timer = setTimeout(async () => {
        if (!valid()) return;
        if (!busy) await refresh();
        if (valid()) poll();
      }, 5000);
    }
    refresh(true).then(poll);
    return {refresh:() => !busy && refresh(true), stop:() => clearTimeout(timer)};
  }

  let drawerController = null, operationsController = null, operationsDate = null;

  function mountAppointment(options) {
    drawerController?.stop();
    const {root,item} = options;
    root.innerHTML = '<h3>WhatsApp & attendance</h3><p class="wa-notice" role="status"></p><div class="wa-body">Loading visit status…</div>';
    const notice = (message,error=false) => { const el = root.querySelector('.wa-notice'); el.textContent = message; el.classList.toggle('wa-error',error); };
    drawerController = controller(root.querySelector('.wa-body'),options,data => {
      const appointment = data.appointments.find(a => a.id === item.id);
      if (!appointment) return '<p>Appointment is no longer on this date. Refresh the calendar.</p>';
      const offers = data.offers.filter(o => o.appointment_ids.includes(item.id));
      return `<p class="wa-mode">${esc(data.mode)}</p><p class="wa-explanation">${esc(readiness(data,appointment.patient))}. Publishing offers the child’s complete continuous visit.</p><div class="wa-drawer-actions">${appointmentActions(appointment,data)}</div>${offers.map(o => offerCard(o,data,'appointment')).join('') || '<p class="fine-print">No WhatsApp offer has been published for this visit.</p>'}`;
    }, () => Boolean(root.closest('.drawer.open')), () => item.date, notice);
  }

  function operationsBody(data) {
    const drafts = data.appointments.filter(a => a.status === 'PROPOSED');
    const publish = action(preview(data) ? 'Publish proposed visits in preview' : 'Publish proposed WhatsApp offers','publish','',!drafts.length || drafts.some(a => !ready(data,a.patient)));
    const unread = data.inbox.filter(i => !i.resolved).length;
    return `<p class="wa-mode">${esc(data.mode)}</p><p class="wa-explanation">${preview(data) ? 'Publishing and simulated replies exercise the real test queue without sending WhatsApp messages.' : 'Ready parents receive their offers. In session mode, other offers wait for a parent message to open the contact window. Delivery receipts never confirm attendance.'}</p><div class="wa-actions">${publish}</div><h3>Visits & attendance</h3>${table(['Child','Therapy / time','Appointment','Action'],data.appointments.map(a => [esc(a.patient),`${esc(a.therapy)}<small>${esc(a.time)}</small>`,esc(label(a.status)),appointmentActions(a,data)]))}<h3>Offers, delivery & parent replies</h3><div class="wa-offers">${data.offers.map(o => offerCard(o,data,'operations')).join('') || '<p class="fine-print">Publish a proposed visit to create an offer.</p>'}</div><h3>Help & staff inbox <span class="soft-pill">${unread} open</span></h3><p class="fine-print">Help and reschedule requests retain the booking until staff act. This inbox includes all test dates.</p>${data.inbox.map(i => `<article class="wa-inbox"><strong>${esc(i.patient || 'Unmatched message')} · ${esc(i.reason)}</strong><p>${esc(i.text)}</p>${i.resolved ? '<span class="soft-pill">Resolved</span>' : action('Mark resolved','resolve',`data-id="${i.id}"`)}</article>`).join('') || '<p class="fine-print">No staff requests.</p>'}<details class="wa-detail"><summary>Queue & delivery history</summary>${table(['Job','Type','State','Delivery / issue'],data.jobs.map(j => [esc(j.id),esc(j.kind),esc(label(j.status)),esc(j.delivery || j.error || 'Not reported')]))}</details><details class="wa-detail"><summary>Weekly therapy progress</summary>${table(['Child','Therapy','Target','Completed','Booked','Remaining'],data.progress.map(p => [p.patient,p.therapy,p.target,p.completed,p.booked,p.remaining].map(esc)))}</details>`;
  }

  function mountOperations(options) {
    operationsController?.stop();
    operationsDate = operationsDate || options.date;
    const panel = document.createElement('section');
    panel.className = 'panel wa-panel';
    panel.innerHTML = '<div class="panel-header"><div><h2>WhatsApp & attendance</h2><p>Publish visits, follow replies, and handle staff requests.</p></div></div><div class="panel-body"><div class="wa-toolbar"><label>WhatsApp planning date<input type="date" class="wa-date"></label><button class="secondary-button" type="button" data-wa-refresh>Refresh WhatsApp status</button></div><p class="wa-notice" role="status"></p><div class="wa-body">Loading WhatsApp status…</div></div>';
    options.root.querySelector('#operations-panel-whatsapp').append(panel);
    const date = panel.querySelector('.wa-date');
    date.value = operationsDate;
    const notice = (message,error=false) => { const el = panel.querySelector('.wa-notice'); el.textContent = message; el.classList.toggle('wa-error',error); };
    operationsController = controller(panel.querySelector('.wa-body'),options,operationsBody,() => options.root.classList.contains('active'),() => date.value,notice);
    date.onchange = () => { if (date.value) { operationsDate = date.value; operationsController.refresh(); } };
    panel.querySelector('[data-wa-refresh]').onclick = () => operationsController.refresh();
  }

  function mountCalendar(options) {
    let button = options.root.querySelector('[data-wa-open]');
    if (!button) {
      button = document.createElement('button');
      button.className = 'secondary-button'; button.dataset.waOpen = 'true';
      button.textContent = 'WhatsApp offers'; options.root.prepend(button);
    }
    button.onclick = () => {
      operationsDate = options.date;
      options.onOpen();
    };
    let push = options.root.querySelector('[data-zoho-open]');
    if (!push) {
      push = document.createElement('button');
      push.type = 'button'; push.className = 'secondary-button';
      push.dataset.zohoOpen = 'true'; push.textContent = 'Push to Zoho';
      button.after(push);
    }
    push.onclick = () => openZohoExport(options, push);
  }

  function openZohoExport(options, opener) {
    if (document.querySelector('.zoho-export-dialog')) return;
    // Capture the selected day. Calendar filters and unsaved previews do not
    // change the saved appointments exported by the server.
    const date = options.date;
    const day = new Date(`${date}T12:00:00+05:30`).toLocaleDateString('en-IN', {day:'numeric',month:'long',year:'numeric',timeZone:'Asia/Kolkata'});
    const dialog = document.createElement('dialog');
    dialog.className = 'test-workspace-login zoho-export-dialog';
    dialog.setAttribute('aria-labelledby','zohoExportTitle');
    dialog.innerHTML = `<h2 id="zohoExportTitle">Push to Zoho</h2><p><strong>${esc(day)}</strong> · Saved appointments for this date, across all calendar filters. Unsaved previews are excluded.</p><div data-zoho-summary aria-live="polite">Loading export preview…</div><p data-zoho-notice role="status"></p><div class="wa-actions"><button type="button" class="secondary-button" data-zoho-close>Cancel</button><button type="button" class="primary-button" data-zoho-confirm disabled>Push to Zoho</button></div>`;
    document.body.append(dialog);
    const summary = dialog.querySelector('[data-zoho-summary]');
    const notice = dialog.querySelector('[data-zoho-notice]');
    const confirm = dialog.querySelector('[data-zoho-confirm]');
    const close = dialog.querySelector('[data-zoho-close]');
    let busy = false;
    const message = (text, error=false) => { notice.textContent = text; notice.classList.toggle('wa-error',error); };
    close.onclick = () => dialog.close();
    dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
    dialog.addEventListener('close', () => { dialog.remove(); opener.focus(); });
    dialog.showModal();

    async function refreshPreview(showEmpty = true) {
      try {
        const result = await post(options.api,'/api/zoho/export/preview',{date});
        if (!dialog.isConnected) return;
        const last = result.lastPush?.completedAtIST;
        const batch = result.openBatch;
        const wrongDate = batch && batch.date !== date;
        summary.innerHTML = `${result.wouldAssumeAcceptedVisits ? `<p><strong>${result.wouldAssumeAcceptedVisits} sent visits</strong> have no reply. Pushing accepts these visits and includes their therapy appointments.</p>` : ''}${result.changes.length ? `<p><strong>${result.changes.length} appointment changes</strong> are ready to send.</p>` : !result.wouldAssumeAcceptedVisits && !batch ? '<p>No new appointment changes are ready.</p>' : ''}<p>Help requests and offers that were not sent stay for staff review. Existing Zoho rows are updated; repeat pushes do not create duplicates.</p>${batch ? `<p>An unfinished push for ${esc(batch.date)} ${wrongDate ? 'must be retried from that calendar date first' : 'will resume first'}.</p>` : ''}<p class="fine-print">Destination: test-gokul / Scheduler_Report${last ? `<br>Last successful push: ${esc(new Date(last).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'}))} IST` : '<br>No successful push yet.'}</p>`;
        confirm.disabled = Boolean(wrongDate) || !(result.changes.length || result.wouldAssumeAcceptedVisits || batch);
        if (showEmpty && confirm.disabled && !wrongDate) message('Nothing to push. Save the schedule and resolve pending staff requests first.');
      } catch (error) { if (dialog.isConnected) message(error.message,true); }
    }
    confirm.onclick = async () => {
      if (busy) return;
      busy = true; confirm.disabled = true; close.disabled = true;
      confirm.textContent = 'Pushing…';
      message('Keep this window open while Zoho confirms the records.');
      try {
        const result = await post(options.api,'/api/zoho/export/push',{date});
        message(`Push complete: ${result.created} added, ${result.updated} updated, ${result.reconciled} already matched. ${result.assumedAcceptedVisits} unanswered visit(s) accepted. ${result.awaitingStaff.length} appointment(s) still need staff review.`);
        confirm.hidden = true; close.textContent = 'Done';
        await refreshPreview(false);
      } catch (error) {
        await refreshPreview();
        message(`${error.message} Refresh or retry this date to continue safely.`,true);
        confirm.textContent = 'Retry push';
      } finally {
        busy = false; close.disabled = false;
        options.onChanged().catch(() => {});
      }
    };
    refreshPreview();
  }

  return {mountAppointment,mountOperations,mountCalendar};
})();
