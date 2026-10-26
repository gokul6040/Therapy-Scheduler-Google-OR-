"use strict";
let demo = null;
let activeTab = "schedule";
let polling = null;
let booking = null;
let bookingPreview = null;
let pickerMode = "all";
let patientPreviewVersion = 0;
let reservationRequestId = null;
const savedStatuses = new Set(["PROPOSED", "PENDING_CONFIRMATION", "CONFIRMED", "COMPLETED"]);
const $ = id => document.getElementById(id);
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const badge = value => `<span class="badge ${String(value).toLowerCase()}">${escapeHtml(String(value).replaceAll("_", " "))}</span>`;
const patient = row => `<span class="patient-cell">${escapeHtml(row.name || row.patient)}<small>${escapeHtml(row.patient)}</small></span>`;
const empty = (title, body) => `<div class="empty"><div class="empty-icon">◈</div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(body)}</span></div>`;
const table = (headers, rows) => rows.length ? `<div class="table-wrap"><table><thead><tr>${headers.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(v => `<td>${v}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : empty("No matching records", "Try another search or branch filter.");
function matches(row) {
  const query = $("search").value.trim().toLowerCase();
  return (!$("branch-filter").value || row.branch === $("branch-filter").value) && (!query || Object.values(row).join(" ").toLowerCase().includes(query));
}
function setTab(name) {
  activeTab = name;
  document.querySelectorAll("[data-tab]").forEach(button => {
    const selected = button.dataset.tab === name;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  renderView();
}
function renderView() {
  if (!demo) return;
  const target = demo.target_date;
  const before = demo.before;
  const after = demo.after;
  const outcome = demo.outcome;
  $("branch-filter").hidden = ["weekly", "replies", "alternatives"].includes(activeTab);
  const descriptions = {
    schedule: ["Monday, before and after", "Original and updated session slots. Confirmed and unanswered visits stay in place."],
    changes: ["Who filled each released opening?", "Click a slot to choose any patient, or select its suggestion. Admins can reserve despite warnings."],
    replies: ["Assumed parent availability", "Replies cover a complete visit. In this dataset, No explicitly means unavailable all Monday."],
    weekly: ["The whole week still counts", "Per therapy: weekly target − completed − reserved sessions = still to arrange. Suggestions are not bookings."],
    alternatives: ["Other times for families who said No", "These are valid suggestions within this week. They are not saved as appointments or sent to anyone."],
  };
  $("view-title").textContent = descriptions[activeTab][0];
  $("view-description").textContent = descriptions[activeTab][1];
  if (activeTab === "schedule") {
    const scheduleTable = (snapshot, isAfter) => {
      const rows = snapshot.appointments.filter(row => row.date === target && (isAfter ? savedStatuses.has(row.status) : true)).filter(matches);
      return table(["Time / therapist", "Patient", "Therapy", isAfter ? "Status" : "Reply"], rows.map(row => [
        `${escapeHtml(row.time)}<br><small>${escapeHtml(row.therapist)}</small>`, patient(row), escapeHtml(row.therapy), badge(isAfter ? row.status : row.reply),
      ]));
    };
    $("view-content").innerHTML = `<div class="split"><div><div class="split-heading">BEFORE · ${before.summary.day_sessions} session slots</div>${scheduleTable(before, false)}</div><div><div class="split-heading">AFTER · ${after ? after.appointments.filter(r => r.date === target && savedStatuses.has(r.status)).length + " session slots" : "awaiting run"}</div>${after ? scheduleTable(after, true) : empty("The next version starts here", "Click Run scheduler to process the saved replies and propose replacements.")}</div></div><div class="note-row">Open Released openings to reserve a slot. Admin reservations are confirmed immediately; superseded proposals stay in the history.</div>`;
  } else if (activeTab === "changes") {
    if (!outcome) {
      $("view-content").innerHTML = empty("Run the scheduler to see replacements", "We will show the original patient, the replacement, and any opening that could not be filled.");
      return;
    }
    const changes = outcome.changes.filter(change => matches({...change.original, replacement: change.replacement?.patient || ""}));
    $("view-content").innerHTML = table(["Slot / choose patient", "Therapy / branch", "Assigned therapist", "Original patient · No", "Suggested / reserved patient", "Action"], changes.map(({original, replacement, admin_reserved}) => [
      admin_reserved ? `<strong>${escapeHtml(original.time)}</strong><br>${badge("RESERVED")}` : `<button class="slot-choice" data-open-slot="${original.id}" aria-label="Choose patient for ${escapeHtml(original.time)} ${escapeHtml(original.therapy)} ${escapeHtml(original.therapist)}"><strong>${escapeHtml(original.time)}</strong><small>Choose patient ↗</small></button>`,
      `${escapeHtml(original.therapy)}<br><small>${escapeHtml(original.branch)}</small>`, escapeHtml(original.therapist), patient(original),
      replacement ? admin_reserved ? patient(replacement) : `<button class="suggestion-choice" data-suggest-slot="${original.id}" data-patient-id="${replacement.patient_id}">${patient(replacement)}</button>` : "No automatic match",
      admin_reserved ? badge("CONFIRMED") : replacement ? `<button class="mini-confirm" data-suggest-slot="${original.id}" data-patient-id="${replacement.patient_id}">Confirm suggestion</button>` : `<button class="mini-confirm" data-open-slot="${original.id}">Choose patient</button>`,
    ])) + `<div class="note-row">${outcome.reserved_sessions || 0} sessions reserved by admin. ${outcome.unfilled_slots} slot(s) still open. Choose any patient; warnings can be overridden explicitly.</div>`;
  } else if (activeTab === "replies") {
    const visits = [...new Map(before.appointments.filter(r => r.date === target).map(row => [row.bundle, row])).values()];
    const query = $("search").value.toLowerCase().trim();
    $("view-content").innerHTML = table(["Patient", "Visit time", "Branch", "Parent reply", "What the scheduler does"], visits.filter(r => !query || Object.values(r).join(" ").toLowerCase().includes(query)).map(row => [
      patient(row), escapeHtml(before.appointments.find(r => r.bundle === row.bundle).time), escapeHtml(row.branch), badge(row.reply), row.reply === "YES" ? "Keep time and confirm" : row.reply === "NO" ? "Release visit; look for replacements" : "Keep unchanged; awaiting response",
    ]));
  } else if (activeTab === "weekly") {
    const snapshot = after || before;
    const query = $("search").value.toLowerCase().trim();
    $("view-content").innerHTML = table(["Patient", "Therapy", "Weekly target", "Completed", "Booked / proposed", "Still to arrange"], snapshot.weekly.filter(r => !query || Object.values(r).join(" ").toLowerCase().includes(query)).map(row => [
      patient(row), escapeHtml(row.therapy), row.required, row.completed, `<span class="weekly-bar"><i style="width:${Math.min(100, 100 * (row.booked + row.completed) / Math.max(1, row.required))}%"></i></span>${row.booked}`, `<strong>${row.remaining}</strong>`,
    ])) + `<div class="note-row">${after ? "After" : "Before"} the run. Reserved includes proposed, pending, and confirmed sessions; it is separate from delivered therapy.</div>`;
  } else {
    if (outcome?.alternatives_stale) {
      $("view-content").innerHTML = empty("The schedule changed after an admin reservation", "The original alternative suggestions have been hidden because they may be out of date. Weekly progress reflects the new reservations.");
      return;
    }
    if (!outcome) {
      $("view-content").innerHTML = empty("Alternative times appear after a run", "The declining family's original visit is released, but their weekly therapy requirement remains.");
      return;
    }
    const query = $("search").value.toLowerCase().trim();
    $("view-content").innerHTML = table(["Patient", "Date", "Time", "Therapy", "Assigned therapist", "Status"], outcome.alternatives.filter(r => !query || Object.values(r).join(" ").toLowerCase().includes(query)).map(row => [patient(row), escapeHtml(new Date(row.date + "T12:00:00").toLocaleDateString("en-IN", {weekday:"short",day:"numeric",month:"short"})), escapeHtml(row.time), escapeHtml(row.therapy), escapeHtml(row.therapist), badge("SUGGESTION")])) + (outcome.unmet.length ? `<div class="note-row">Still needs staff attention: ${outcome.unmet.map(row => `${escapeHtml(row.patient)} · ${escapeHtml(row.therapy)} (${escapeHtml(row.reason)})`).join("; ")}</div>` : "");
  }
}
function renderState(next) {
  const justCompleted = demo?.status === "running" && next.status === "completed";
  const newRun = demo?.run_id !== next.run_id;
  demo = next;
  const s = demo.before.summary;
  $("target-date").textContent = new Date(demo.target_date + "T12:00:00").toLocaleDateString("en-IN", {weekday:"short",day:"numeric",month:"short",year:"numeric"});
  $("stats").innerHTML = [
    ["Fictional patients", s.patients, `${s.therapists} therapists · ${s.branches} branches`],
    ["Monday appointments", s.day_sessions, `${s.day_visits} visits · 3 therapy types`],
    ["Saved parent replies", s.replies, `${s.yes} Yes · ${s.no} No · ${s.no_reply} awaiting reply`],
    ["Existing weekly sessions", s.appointments, `${s.therapy_requirements} therapy requirements`],
  ].map(([label,value,note]) => `<div class="stat"><div class="stat-label">${label}</div><div class="stat-value">${value}</div><div class="stat-note">${note}</div></div>`).join("");
  if (newRun) {
    const branches = [...new Set(demo.before.appointments.map(r => r.branch))].sort();
    $("branch-filter").innerHTML = '<option value="">All branches</option>' + branches.map(b => `<option>${escapeHtml(b)}</option>`).join("");
    $("search").value = "";
  }
  const running = demo.status === "running";
  $("run-button").disabled = demo.status !== "ready";
  $("reset-button").disabled = running;
  $("run-button").textContent = running ? "Solving…" : demo.status === "completed" ? "✓ Run complete" : "▶  Run scheduler";
  $("run-title").textContent = running ? "Scheduler is running" : demo.status === "completed" ? "Monday has been updated" : demo.status === "failed" ? "Run needs attention" : "Ready to run";
  $("run-description").textContent = running ? `Processing saved replies and solving scheduling constraints · ${demo.elapsed_seconds.toFixed(1)}s` : demo.status === "completed" ? `Real scheduling run finished in ${demo.elapsed_seconds.toFixed(2)}s. Reset to replay the same input.` : "The original appointments and assumed parent replies are loaded.";
  $("run-icon").textContent = demo.status === "completed" ? "✓" : "↗";
  $("run-progress").hidden = !running;
  $("error").hidden = !demo.error;
  $("error").textContent = demo.error || "";
  $("database-path").textContent = demo.database_path;
  $("outcome").hidden = !demo.outcome;
  $("change-count").textContent = demo.outcome?.released_sessions || "";
  if (demo.outcome) {
    const o = demo.outcome;
    const description = o.reserved_sessions ? `${o.reserved_sessions} session${o.reserved_sessions === 1 ? "" : "s"} reserved by admin without messaging. ${o.override_warning_count || 0} warning(s) acknowledged; weekly counts updated.` : `${o.confirmed_sessions} Yes sessions confirmed in place. Eight unanswered visits stayed where they were. Automatic scheduling checks passed.`;
    $("outcome").innerHTML = `<div><strong>${o.reserved_sessions ? "Admin reservations saved locally." : "Parent replies processed. Existing visits protected."}</strong><p>${description}</p></div><div class="numbers"><div><b>${o.filled_sessions ?? o.proposed_sessions}</b><span>replacement sessions</span></div><div><b>${o.unfilled_slots}</b><span>slots still open</span></div></div>`;
  }
  if (newRun) setTab("schedule"); else if (justCompleted) setTab("changes"); else renderView();
  if (running && !polling) polling = setTimeout(poll, 350);
}
async function request(path, method="GET", body=null) {
  const response = await fetch(path, {method, ...(body ? {headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)} : {})});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "The demo request failed");
  return payload;
}
async function poll() {
  polling = null;
  try {renderState(await request("/api/demo/state"));}
  catch (error) {$("error").hidden=false; $("error").textContent=error.message; $("run-button").disabled=false;}
}
$("run-button").addEventListener("click", async () => {
  $("run-button").disabled=true;
  try {renderState(await request("/api/demo/run", "POST"));}
  catch (error) {$("error").hidden=false; $("error").textContent=error.message; $("run-button").disabled=false;}
});
$("reset-button").addEventListener("click", async () => {
  $("reset-button").disabled=true;
  try {renderState(await request("/api/demo/reset", "POST"));}
  catch (error) {$("error").hidden=false; $("error").textContent=error.message; $("reset-button").disabled=false;}
});
document.querySelectorAll("[data-tab]").forEach(button => button.addEventListener("click", () => setTab(button.dataset.tab)));
$("branch-filter").addEventListener("change", renderView);
$("search").addEventListener("input", renderView);

function renderPatientPicker() {
  if (!booking) return;
  const query = $("patient-search").value.trim().toLowerCase();
  const rows = (pickerMode === "all" ? booking.patients : booking.suggestions).filter(item =>
    !query || `${item.name} ${item.code} ${item.therapies.join(" ")}`.toLowerCase().includes(query));
  $("picker-all").classList.toggle("active", pickerMode === "all");
  $("picker-suggestions").classList.toggle("active", pickerMode === "suggestions");
  $("patient-results").innerHTML = rows.length ? rows.map(item => `<button class="patient-option ${booking.selectedId === item.id ? "selected" : ""}" data-choose-patient="${item.id}"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.code)} · ${escapeHtml(item.therapies.join(" + ") || "No active plan")}</span><small>${item.active ? escapeHtml(item.scenario || "Patient in the demo list") : "Inactive patient · admin can override"}${item.current_proposal ? " · Current suggestion" : ""}</small></button>`).join("") : empty("No patients found", pickerMode === "suggestions" ? "Choose patient shows the full list, including people with rule mismatches." : "Try another name or patient code.");
}

async function openBooking(sourceId, suggestedPatientId=null) {
  booking = null;
  bookingPreview = null;
  const openVersion = ++patientPreviewVersion;
  pickerMode = "all";
  $("booking-body").hidden = true;
  $("booking-loading").hidden = false;
  $("booking-loading").textContent = "Loading the patient list…";
  $("booking-subtitle").textContent = "";
  $("booking-error").hidden = true;
  $("patient-search").value = "";
  $("reserve-without").disabled = true;
  if (!$("booking-dialog").open) $("booking-dialog").showModal();
  try {
    const details = await request("/api/demo/slot", "POST", {run_id:demo.run_id, source_id:sourceId});
    if (!$("booking-dialog").open || openVersion !== patientPreviewVersion) return;
    booking = {...details, selectedId:null};
    $("booking-loading").hidden = true;
    $("booking-body").hidden = false;
    $("whole-visit").checked = true;
    $("whole-visit").disabled = details.slots.length <= 1;
    $("whole-visit-label").textContent = details.slots.length > 1 ? `Reserve complete continuous visit (${details.slots.length} sessions)` : "Reserve this single session";
    $("booking-subtitle").textContent = `${details.source.time} · ${details.source.therapy} · ${details.source.therapist} · ${details.source.branch}`;
    $("booking-selection").innerHTML = '<div class="selection-empty">Select any patient to review this reservation.</div>';
    $("booking-warnings").innerHTML = "";
    renderPatientPicker();
    if (suggestedPatientId) await chooseBookingPatient(suggestedPatientId);
  } catch (error) {if (openVersion === patientPreviewVersion) $("booking-loading").textContent = error.message;}
}

async function chooseBookingPatient(patientId) {
  if (!booking) return;
  const version = ++patientPreviewVersion;
  booking.selectedId = patientId;
  bookingPreview = null;
  reservationRequestId = null;
  $("reserve-without").disabled = true;
  $("booking-error").hidden = true;
  $("booking-selection").textContent = "Checking this patient's reservation…";
  $("booking-warnings").innerHTML = "";
  renderPatientPicker();
  try {
    const preview = await request("/api/demo/booking-preview", "POST", {
      run_id:booking.run_id, source_id:booking.source.id, patient_id:patientId,
      whole_visit:$("whole-visit").checked,
    });
    if (version !== patientPreviewVersion || !$("booking-dialog").open) return;
    bookingPreview = preview;
    reservationRequestId = crypto.randomUUID();
    $("booking-selection").innerHTML = `<h3>${escapeHtml(preview.name)}</h3><p>${escapeHtml(preview.patient)} · ${preview.slots.length} session(s) selected</p><div class="selected-times">${preview.slots.map(slot => `<span>${escapeHtml(slot.time)}</span>`).join("")}</div>`;
    $("booking-warnings").innerHTML = preview.warnings.length ? `<div class="warning-box" role="status"><strong>${preview.warnings.length} warning(s) · admin can override</strong><ul>${preview.warnings.map(item => `<li>${escapeHtml(item.message)}</li>`).join("")}</ul><p>Reserve anyway confirms your choice and records these warnings.</p></div>` : '<div class="matching-box">✓ No scheduling mismatches found for this reservation.</div>';
    $("reserve-without").textContent = preview.warnings.length ? "Reserve anyway without messaging" : "Confirm without messaging";
    $("reserve-without").disabled = false;
  } catch (error) {
    if (version === patientPreviewVersion) {$("booking-error").hidden = false; $("booking-error").textContent = error.message;}
  }
}

$("view-content").addEventListener("click", event => {
  const button = event.target.closest("[data-open-slot], [data-suggest-slot]");
  if (!button) return;
  openBooking(Number(button.dataset.openSlot || button.dataset.suggestSlot), button.dataset.patientId ? Number(button.dataset.patientId) : null);
});
$("patient-results").addEventListener("click", event => {
  const button = event.target.closest("[data-choose-patient]");
  if (button) chooseBookingPatient(Number(button.dataset.choosePatient));
});
$("patient-search").addEventListener("input", renderPatientPicker);
$("picker-all").addEventListener("click", () => {pickerMode="all"; renderPatientPicker();});
$("picker-suggestions").addEventListener("click", () => {pickerMode="suggestions"; renderPatientPicker();});
$("whole-visit").addEventListener("change", () => {if (booking?.selectedId) chooseBookingPatient(booking.selectedId);});
$("booking-close").addEventListener("click", () => $("booking-dialog").close());
$("booking-dialog").addEventListener("close", () => {patientPreviewVersion++; booking=null; bookingPreview=null;});
$("reserve-without").addEventListener("click", async () => {
  if (!bookingPreview || !booking) return;
  const preview = bookingPreview;
  $("reserve-without").disabled = true;
  $("booking-error").hidden = true;
  try {
    const result = await request("/api/demo/reserve", "POST", {
      run_id:booking.run_id, source_id:booking.source.id, patient_id:preview.patient_id,
      whole_visit:preview.whole_visit, preview_token:preview.preview_token,
      request_id:reservationRequestId, override_warnings:preview.warnings.length > 0,
      mode:"without_messaging",
    });
    $("booking-dialog").close();
    renderState(result.state);
    setTab("changes");
    $("reservation-toast").hidden = false;
    $("reservation-toast").textContent = `${preview.name}: ${result.reservation.appointment_ids.length} session(s) reserved. No message sent.`;
    setTimeout(() => {$("reservation-toast").hidden=true;}, 7000);
  } catch (error) {$("booking-error").hidden=false; $("booking-error").textContent=error.message; $("reserve-without").disabled=false;}
});
poll();
