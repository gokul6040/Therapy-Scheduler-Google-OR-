const state = {
  activeView: "schedule",
  calendarView: "day",
  perspective: "therapist",
  currentDate: new Date(),
  overview: null,
  schedule: null,
  filters: { therapist: "", patient: "", department: "", location: "", status: "", search: "" },
  patients: [],
  patientPage: 1,
  patientQuery: "",
  latestPreview: null,
  showPreview: false,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function parseLocalDate(value) {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day, 12, 0, 0);
}

function isoDate(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function addDays(value, days) {
  const result = new Date(value);
  result.setDate(result.getDate() + days);
  return result;
}

function startOfWeek(value) {
  const result = new Date(value);
  const weekday = result.getDay();
  result.setDate(result.getDate() - (weekday === 0 ? 6 : weekday - 1));
  return result;
}

function sameDate(a, b) {
  return isoDate(a) === isoDate(b);
}

function formatDate(value, options = {}) {
  return new Intl.DateTimeFormat("en-IN", options).format(value);
}

function formatStatus(status) {
  return String(status || "").toLowerCase().replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function initials(name) {
  return String(name || "EIC").split(/\s+/).filter(Boolean).slice(0, 2).map((word) => word[0]).join("").toUpperCase();
}

function therapyClass(name) {
  let hash = 0;
  for (const char of String(name || "")) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return `therapy-${hash % 5}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let payload = {};
  try { payload = await response.json(); } catch { /* response guard */ }
  if (!response.ok) throw new Error(payload.error || "Something went wrong. Please try again.");
  return payload;
}

function closeSearchableSelect(picker) {
  if (!picker) return;
  const select = $("select", picker);
  const input = $("input", picker);
  const selected = select.options[select.selectedIndex];
  input.value = select.value && selected ? selected.textContent.trim() : "";
  picker.classList.remove("is-open");
  input.setAttribute("aria-expanded", "false");
}

function closeSearchableSelects(except = null) {
  $$(".searchable-select.is-open").forEach((picker) => {
    if (picker !== except) closeSearchableSelect(picker);
  });
}

function makeSearchableSelect(select, placeholder) {
  if (!select) return;
  let picker = select.closest(".searchable-select");
  if (!picker) {
    picker = document.createElement("div");
    picker.className = "searchable-select";
    select.parentNode.insertBefore(picker, select);
    picker.append(select);
    select.classList.add("native-select-hidden");

    const input = document.createElement("input");
    input.type = "search";
    input.className = "searchable-select-input";
    input.autocomplete = "off";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", `${select.id}-options`);
    input.setAttribute("aria-label", select.getAttribute("aria-label") || placeholder);

    const options = document.createElement("div");
    options.className = "searchable-select-options";
    options.id = `${select.id}-options`;
    options.setAttribute("role", "listbox");
    picker.append(input, options);

    const renderOptions = (query = "") => {
      const normalized = query.trim().toLowerCase();
      const matching = [...select.options]
        .map((option, index) => ({ option, index, label: option.textContent.trim() }))
        .filter((item) => !normalized || item.label.toLowerCase().includes(normalized))
        .slice(0, 60);
      options.innerHTML = matching.length
        ? matching.map((item) => `<button type="button" role="option" aria-selected="${item.option.value === select.value}" data-option-index="${item.index}">${escapeHtml(item.label)}</button>`).join("")
        : `<p class="searchable-select-empty">No matches found</p>`;
      $$('[data-option-index]', options).forEach((button) => button.addEventListener("mousedown", (event) => {
        event.preventDefault();
        select.selectedIndex = Number(button.dataset.optionIndex);
        select.dispatchEvent(new Event("change", { bubbles: true }));
        closeSearchableSelect(picker);
      }));
    };

    picker._renderOptions = renderOptions;
    input.addEventListener("focus", () => {
      closeSearchableSelects(picker);
      picker.classList.add("is-open");
      input.setAttribute("aria-expanded", "true");
      input.select();
      renderOptions("");
    });
    input.addEventListener("input", () => renderOptions(input.value));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        input.blur();
        closeSearchableSelect(picker);
      }
      if (event.key === "Enter") {
        const first = $('[data-option-index]', options);
        if (!first) return;
        event.preventDefault();
        first.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
      }
    });
    select.addEventListener("change", () => {
      if (!picker.classList.contains("is-open")) closeSearchableSelect(picker);
    });
  }
  picker.dataset.placeholder = placeholder;
  const input = $("input", picker);
  input.placeholder = placeholder;
  input.setAttribute("aria-label", select.getAttribute("aria-label") || placeholder);
  if (!picker.classList.contains("is-open")) closeSearchableSelect(picker);
  if (picker._renderOptions) picker._renderOptions("");
}

function toast(title, message, type = "success") {
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.innerHTML = `
    <span>${icon(type === "error" ? "alert" : "check")}</span>
    <div><strong>${escapeHtml(title)}</strong><p>${escapeHtml(message)}</p></div>
    <button aria-label="Dismiss">${icon("close")}</button>`;
  $("button", item).addEventListener("click", () => item.remove());
  $("#toastRegion").append(item);
  window.setTimeout(() => item.remove(), 5200);
}

function pageHeading(eyebrow, title, action = "") {
  return `<div class="page-heading"><div><p class="eyebrow">${escapeHtml(eyebrow)}</p><h1>${escapeHtml(title)}</h1></div>${action ? `<div class="heading-actions">${action}</div>` : ""}</div>`;
}

function setView(view) {
  state.activeView = view;
  $$(".view").forEach((item) => item.classList.toggle("active", item.id === `view-${view}`));
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  $("#sidebar").classList.remove("open");
  $("#mainContent").focus({ preventScroll: true });
  if (view === "overview") loadOverviewView();
  if (view === "patients") loadPatientsView();
  if (view === "team") loadTeamView();
  if (view === "operations") loadOperationsView();
  if (view === "generator") loadGeneratorView();
}

async function initialize() {
  bindShellEvents();
  try {
    state.overview = await api("/api/overview");
    state.currentDate = parseLocalDate(state.overview.today);
    const badge = $("#attentionBadge");
    badge.textContent = state.overview.needsAttention;
    badge.hidden = !state.overview.needsAttention;
    await loadSchedule();
  } catch (error) {
    $("#calendarSurface").innerHTML = errorState(error.message);
  }
}

function bindShellEvents() {
  $$("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  $$("[data-view-link]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.viewLink)));
  $("#menuButton").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
  $("#filterButton").addEventListener("click", () => openDrawer("filterDrawer"));
  $$('[data-close-drawer]').forEach((button) => button.addEventListener("click", closeDrawers));
  $("#drawerBackdrop").addEventListener("click", closeDrawers);
  document.addEventListener("pointerdown", (event) => {
    $$(".searchable-select.is-open").forEach((picker) => {
      if (!picker.contains(event.target)) closeSearchableSelect(picker);
    });
  });
  $("#todayButton").addEventListener("click", async () => {
    state.currentDate = parseLocalDate(state.overview.today);
    await loadSchedule();
  });
  $("#previousDate").addEventListener("click", () => stepDate(-1));
  $("#nextDate").addEventListener("click", () => stepDate(1));
  $("#dateDisplay").addEventListener("click", () => {
    const picker = $("#datePicker");
    picker.value = isoDate(state.currentDate);
    if (picker.showPicker) picker.showPicker(); else picker.click();
  });
  $("#datePicker").addEventListener("change", async (event) => {
    if (!event.target.value) return;
    state.currentDate = parseLocalDate(event.target.value);
    await loadSchedule();
  });
  $$('[data-calendar-view]').forEach((button) => button.addEventListener("click", async () => {
    state.calendarView = button.dataset.calendarView;
    $$('[data-calendar-view]').forEach((item) => item.classList.toggle("active", item === button));
    await loadSchedule();
  }));
  $$('[data-perspective]').forEach((button) => button.addEventListener("click", async () => {
    state.perspective = button.dataset.perspective;
    $$('[data-perspective]').forEach((item) => item.classList.toggle("active", item === button));
    if (state.perspective === "patient") {
      state.calendarView = "week";
      if (!state.filters.patient && state.schedule?.appointments.length) {
        state.filters.patient = String(state.schedule.appointments[0].patientId);
      }
    } else {
      state.calendarView = "day";
    }
    $$('[data-calendar-view]').forEach((item) => item.classList.toggle("active", item.dataset.calendarView === state.calendarView));
    populatePerspectivePicker();
    await loadSchedule();
  }));
  $("#perspectiveEntityPicker").addEventListener("change", (event) => {
    state.filters[state.perspective] = event.target.value;
    const drawerSelect = state.perspective === "therapist" ? $("#therapistFilter") : $("#patientFilter");
    drawerSelect.value = event.target.value;
    renderSchedule();
  });
  $("#applyFilters").addEventListener("click", (event) => { event.preventDefault(); readFilters(); closeDrawers(); renderSchedule(); });
  $("#clearFilters").addEventListener("click", (event) => { event.preventDefault(); clearFilters(); closeDrawers(); renderSchedule(); });
  $("#globalSearch").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    const query = event.currentTarget.value.trim();
    if (state.activeView === "schedule") {
      $("#scheduleSearch").value = query;
      readFilters();
      renderSchedule();
    } else {
      state.patientQuery = query;
      setView("patients");
    }
  });
  $("#hidePreview").addEventListener("click", async () => {
    state.showPreview = false;
    await loadSchedule();
  });
  document.addEventListener("keydown", async (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      $("#globalSearch").focus();
      return;
    }
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    if (event.key === "Escape") { closeSearchableSelects(); closeDrawers(); }
    if (state.activeView !== "schedule") return;
    const shortcut = event.key.toLowerCase();
    if ({ d: "day", w: "week", a: "agenda" }[shortcut]) {
      state.calendarView = { d: "day", w: "week", a: "agenda" }[shortcut];
      $$('[data-calendar-view]').forEach((item) => item.classList.toggle("active", item.dataset.calendarView === state.calendarView));
      await loadSchedule();
    }
    if (shortcut === "t") {
      state.currentDate = parseLocalDate(state.overview.today);
      await loadSchedule();
    }
  });
}

async function stepDate(direction) {
  const amount = state.calendarView === "day" ? direction : direction * 7;
  state.currentDate = addDays(state.currentDate, amount);
  if (state.calendarView === "day" && state.currentDate.getDay() === 0) state.currentDate = addDays(state.currentDate, direction);
  await loadSchedule();
}

async function loadSchedule() {
  const start = state.calendarView === "day" ? state.currentDate : startOfWeek(state.currentDate);
  const days = state.calendarView === "day" ? 1 : 6;
  $("#calendarSurface").innerHTML = `<div class="loading-state"><span class="spinner"></span>Loading the practice schedule…</div>`;
  try {
    state.schedule = await api(`/api/schedule?start=${isoDate(start)}&days=${days}`);
    if (state.showPreview && state.latestPreview) {
      const end = addDays(start, days - 1);
      const existing = new Set(state.schedule.appointments.map((item) => `${item.patientId}-${item.therapistId}-${item.date}-${item.slot}`));
      const previewAppointments = state.latestPreview.appointments
        .filter((item) => item.date >= isoDate(start) && item.date <= isoDate(end))
        .filter((item) => !existing.has(`${item.patientId}-${item.therapistId}-${item.date}-${item.slot}`))
        .map((item, index) => ({ ...item, id: `preview-${index}`, preview: true }));
      state.schedule.appointments.push(...previewAppointments);
    }
    populateFilterOptions();
    renderSchedule();
  } catch (error) {
    $("#calendarSurface").innerHTML = errorState(error.message);
  }
}

function populateFilterOptions() {
  const selected = state.filters;
  $("#therapistFilter").innerHTML = `<option value="">All clinicians</option>${state.schedule.therapists.filter((item) => item.active).map((item) => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.code)}</option>`).join("")}`;
  $("#patientFilter").innerHTML = `<option value="">All patients</option>${state.schedule.patients.map((item) => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.code)}</option>`).join("")}`;
  $("#departmentFilter").innerHTML = `<option value="">All therapies</option>${state.schedule.departments.map((item) => `<option value="${item.id}">${escapeHtml(item.name)}</option>`).join("")}`;
  $("#locationFilter").innerHTML = `<option value="">All locations</option>${state.schedule.locations.map((item) => `<option value="${item.id}">${escapeHtml(item.name)}</option>`).join("")}`;
  $("#therapistFilter").value = selected.therapist;
  $("#patientFilter").value = selected.patient;
  $("#departmentFilter").value = selected.department;
  $("#locationFilter").value = selected.location;
  $("#statusFilter").value = selected.status;
  $("#scheduleSearch").value = selected.search;
  makeSearchableSelect($("#therapistFilter"), "Search clinicians");
  makeSearchableSelect($("#patientFilter"), "Search patients");
  makeSearchableSelect($("#departmentFilter"), "Search therapies");
  makeSearchableSelect($("#locationFilter"), "Search branches");
  populatePerspectivePicker();
}

function populatePerspectivePicker() {
  if (!state.schedule) return;
  const picker = $("#perspectiveEntityPicker");
  const label = $("#perspectivePickerLabel");
  const showingTherapists = state.perspective === "therapist";
  const entities = showingTherapists
    ? state.schedule.therapists.filter((item) => item.active)
    : state.schedule.patients;
  label.textContent = showingTherapists ? "Doctor" : "Patient";
  picker.setAttribute("aria-label", showingTherapists ? "Choose doctor" : "Choose patient");
  picker.innerHTML = `<option value="">All ${showingTherapists ? "doctors" : "patients"}</option>${entities.map((item) => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.code)}</option>`).join("")}`;
  picker.value = state.filters[state.perspective];
  makeSearchableSelect(picker, `Search ${showingTherapists ? "doctors" : "patients"}`);
}

function readFilters() {
  state.filters = {
    therapist: $("#therapistFilter").value,
    patient: $("#patientFilter").value,
    department: $("#departmentFilter").value,
    location: $("#locationFilter").value,
    status: $("#statusFilter").value,
    search: $("#scheduleSearch").value.trim(),
  };
}

function clearFilters() {
  state.filters = { therapist: "", patient: "", department: "", location: "", status: "", search: "" };
  populateFilterOptions();
}

function filteredAppointments() {
  const search = state.filters.search.toLowerCase();
  return state.schedule.appointments.filter((item) => {
    if (state.filters.therapist && String(item.therapistId) !== state.filters.therapist) return false;
    if (state.filters.patient && String(item.patientId) !== state.filters.patient) return false;
    if (state.filters.department && String(item.departmentId) !== state.filters.department) return false;
    if (state.filters.location && String(item.locationId) !== state.filters.location) return false;
    if (state.filters.status && item.status !== state.filters.status) return false;
    if (search && !`${item.patientCode} ${item.patientName} ${item.therapistCode} ${item.therapistName} ${item.therapy}`.toLowerCase().includes(search)) return false;
    return true;
  });
}

function renderFilterChips() {
  const labels = [];
  const therapist = state.schedule.therapists.find((item) => String(item.id) === state.filters.therapist);
  const patient = state.schedule.patients.find((item) => String(item.id) === state.filters.patient);
  const department = state.schedule.departments.find((item) => String(item.id) === state.filters.department);
  const location = state.schedule.locations.find((item) => String(item.id) === state.filters.location);
  if (therapist) labels.push(["therapist", therapist.name]);
  if (patient) labels.push(["patient", patient.name]);
  if (department) labels.push(["department", department.name]);
  if (location) labels.push(["location", location.name]);
  if (state.filters.status) labels.push(["status", formatStatus(state.filters.status)]);
  if (state.filters.search) labels.push(["search", `“${state.filters.search}”`]);
  const row = $("#activeFilters");
  const count = $("#filterCount");
  row.hidden = labels.length === 0;
  count.hidden = labels.length === 0;
  count.textContent = labels.length;
  row.innerHTML = labels.map(([key, label]) => `<span class="filter-chip">${escapeHtml(label)}<button data-remove-filter="${key}" aria-label="Remove ${escapeHtml(label)}">${icon("close")}</button></span>`).join("");
  $$('[data-remove-filter]', row).forEach((button) => button.addEventListener("click", () => {
    state.filters[button.dataset.removeFilter] = "";
    populateFilterOptions();
    renderSchedule();
  }));
}

function renderSchedule() {
  if (!state.schedule) return;
  const appointments = filteredAppointments();
  const visibleEntities = new Set(appointments.map((item) => (
    state.perspective === "therapist" ? item.therapistId : item.patientId
  )));
  $("#visibleAppointmentCount").textContent = appointments.length.toLocaleString("en-IN");
  $("#visibleTherapistCount").textContent = visibleEntities.size.toLocaleString("en-IN");
  $("#visibleEntityLabel").textContent = state.perspective === "therapist" ? "doctors scheduled" : "patients scheduled";
  $("#previewBanner").hidden = !(state.showPreview && state.latestPreview);
  renderFilterChips();
  updateDateDisplay();
  if (state.calendarView === "day") renderDayCalendar(appointments);
  if (state.calendarView === "week") renderWeekCalendar(appointments);
  if (state.calendarView === "agenda") renderAgenda(appointments);
}

function updateDateDisplay() {
  const display = $("#dateDisplay");
  if (state.calendarView === "day") {
    display.textContent = formatDate(state.currentDate, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  } else {
    const start = startOfWeek(state.currentDate);
    const end = addDays(start, 5);
    const sameMonth = start.getMonth() === end.getMonth();
    display.textContent = sameMonth
      ? `${start.getDate()}–${formatDate(end, { day: "numeric", month: "long", year: "numeric" })}`
      : `${formatDate(start, { day: "numeric", month: "short" })} – ${formatDate(end, { day: "numeric", month: "short", year: "numeric" })}`;
  }
}

function renderDayCalendar(appointments) {
  const showingTherapists = state.perspective === "therapist";
  const entities = showingTherapists
    ? state.schedule.therapists
        .filter((item) => appointments.some((appointment) => appointment.therapistId === item.id))
        .map((item) => ({ id: item.id, name: item.name, code: item.code, meta: `${item.department} · ${item.location}` }))
    : [...new Map(appointments.map((item) => [item.patientId, {
        id: item.patientId,
        name: item.patientName,
        code: item.patientCode,
        meta: `${new Set(appointments.filter((appointment) => appointment.patientId === item.patientId).map((appointment) => appointment.therapy)).size} therapies`,
      }])).values()];
  if (!entities.length && !appointments.length) {
    $("#calendarSurface").innerHTML = emptySchedule();
    return;
  }
  entities.sort((a, b) => a.name.localeCompare(b.name));
  const columns = entities.length;
  const header = entities.map((entity) => {
    const count = appointments.filter((item) => (
      showingTherapists ? item.therapistId === entity.id : item.patientId === entity.id
    )).length;
    return `<div class="clinician-header ${showingTherapists ? "doctor-header" : "patient-header"}"><span class="avatar">${escapeHtml(initials(entity.name))}</span><span><strong>${escapeHtml(entity.name)}</strong><small>${escapeHtml(entity.code)} · ${escapeHtml(entity.meta)}</small></span><span class="clinician-count">${count}</span></div>`;
  }).join("");
  const rows = state.schedule.slotLabels.map((label, slot) => {
    const cells = entities.map((entity) => {
      const items = appointments.filter((item) => item.slot === slot && (
        showingTherapists ? item.therapistId === entity.id : item.patientId === entity.id
      ));
      return `<div class="schedule-cell ${slot === 5 ? "is-lunch" : ""}">${items.map(appointmentCard).join("")}</div>`;
    }).join("");
    return `<div class="day-grid-row"><div class="time-label">${escapeHtml(label.split("–")[0])}</div>${cells}</div>`;
  }).join("");
  $("#calendarSurface").innerHTML = `<div class="day-calendar" style="--columns:${columns}"><div class="day-grid-header"><div class="time-corner">Time</div>${header}</div>${rows}</div>`;
  bindAppointmentCards();
}

function renderWeekCalendar(appointments) {
  const start = startOfWeek(state.currentDate);
  const days = Array.from({ length: 6 }, (_, index) => addDays(start, index));
  const today = parseLocalDate(state.overview.today);
  const headers = days.map((day) => `<div class="day-header ${sameDate(day, today) ? "is-today" : ""}"><span>${formatDate(day, { weekday: "short" })}</span><strong>${day.getDate()}</strong></div>`).join("");
  const rows = state.schedule.slotLabels.map((label, slot) => {
    const cells = days.map((day) => {
      const items = appointments.filter((item) => item.slot === slot && item.date === isoDate(day));
      const visible = items.slice(0, 3);
      return `<div class="week-cell">${visible.map(appointmentCard).join("")}${items.length > visible.length ? `<button class="overflow-button" data-overflow-date="${isoDate(day)}">+${items.length - visible.length} more</button>` : ""}</div>`;
    }).join("");
    return `<div class="week-grid-row"><div class="time-label">${escapeHtml(label.split("–")[0])}</div>${cells}</div>`;
  }).join("");
  if (!appointments.length) {
    $("#calendarSurface").innerHTML = emptySchedule();
    return;
  }
  $("#calendarSurface").innerHTML = `<div class="week-calendar"><div class="week-grid-header"><div class="time-corner">Time</div>${headers}</div>${rows}</div>`;
  bindAppointmentCards();
  $$('[data-overflow-date]').forEach((button) => button.addEventListener("click", () => {
    state.currentDate = parseLocalDate(button.dataset.overflowDate);
    state.calendarView = "agenda";
    $$('[data-calendar-view]').forEach((item) => item.classList.toggle("active", item.dataset.calendarView === "agenda"));
    renderSchedule();
  }));
}

function renderAgenda(appointments) {
  if (!appointments.length) {
    $("#calendarSurface").innerHTML = emptySchedule();
    return;
  }
  const groups = Object.groupBy ? Object.groupBy(appointments, (item) => item.date) : appointments.reduce((acc, item) => ((acc[item.date] ||= []).push(item), acc), {});
  $("#calendarSurface").innerHTML = `<div class="agenda-list">${Object.entries(groups).sort(([a], [b]) => a.localeCompare(b)).map(([day, items]) => `
    <section class="agenda-day"><div class="agenda-day-title">${formatDate(parseLocalDate(day), { weekday: "long", day: "numeric", month: "long" })}<span>${items.length}</span></div>
    ${items.map((item) => `<button class="agenda-row" data-appointment-id="${item.id}"><span class="agenda-time">${escapeHtml(item.time.split("–")[0])}</span><span class="agenda-person"><strong>${escapeHtml(state.perspective === "therapist" ? item.patientName : item.therapistName)}</strong><small>${escapeHtml(state.perspective === "therapist" ? item.patientCode : item.therapistCode)} · ${escapeHtml(item.therapy)}</small></span><span class="agenda-clinician"><strong>${escapeHtml(state.perspective === "therapist" ? item.therapistName : item.patientName)}</strong><small>${escapeHtml(state.perspective === "therapist" ? item.therapistCode : item.patientCode)}</small></span><span class="soft-pill">${escapeHtml(item.location)}</span><span class="status-pill status-${item.status.toLowerCase()}">${escapeHtml(formatStatus(item.status))}</span>${icon("chevron-right")}</button>`).join("")}</section>`).join("")}</div>`;
  bindAppointmentCards();
}

function appointmentCard(item) {
  const primary = state.perspective === "therapist" ? item.patientName : item.therapistName;
  const secondary = state.perspective === "therapist" ? item.patientCode : item.therapistCode;
  return `<button class="appointment-card ${therapyClass(item.therapy)} status-${item.status.toLowerCase()}" data-appointment-id="${item.id}"><strong>${escapeHtml(primary)}</strong><small>${escapeHtml(secondary)} · ${escapeHtml(item.therapy)}</small><span class="card-time">${escapeHtml(item.time.split("–")[0])}</span></button>`;
}

function bindAppointmentCards() {
  $$('[data-appointment-id]').forEach((card) => card.addEventListener("click", () => {
    const item = state.schedule.appointments.find((appointment) => String(appointment.id) === card.dataset.appointmentId);
    if (item) openAppointment(item);
  }));
}

function emptySchedule() {
  const filtersActive = Object.values(state.filters).some(Boolean);
  const perspective = state.perspective === "therapist" ? "doctor" : "patient";
  return `<div class="empty-state"><span class="empty-state-icon">${icon(filtersActive ? "filter" : "calendar")}</span><h3>${filtersActive ? `No ${perspective} schedules match these filters` : "This schedule is ready to plan"}</h3><p>${filtersActive ? "Clear or adjust the calendar filters to broaden the view." : "Generate a validated Monday–Saturday plan to place patient sessions into available clinician time."}</p><button class="${filtersActive ? "secondary-button" : "primary-button"}" id="emptyAction">${filtersActive ? "Clear filters" : "Plan this week"}</button></div>`;
}

function errorState(message) {
  return `<div class="empty-state"><span class="empty-state-icon">${icon("alert")}</span><h3>We couldn’t load this view</h3><p>${escapeHtml(message)}</p><button class="secondary-button" onclick="location.reload()">Try again</button></div>`;
}

function bindEmptyAction() {
  const action = $("#emptyAction");
  if (!action) return;
  action.addEventListener("click", () => {
    if (Object.values(state.filters).some(Boolean)) { clearFilters(); renderSchedule(); }
    else setView("generator");
  });
}

const scheduleObserver = new MutationObserver(bindEmptyAction);
scheduleObserver.observe($("#calendarSurface"), { childList: true });

async function loadOverviewView() {
  const root = $("#view-overview");
  root.innerHTML = `<div class="loading-state"><span class="spinner"></span>Loading today’s overview…</div>`;
  try {
    state.overview = await api("/api/overview");
    const todaySchedule = await api(`/api/schedule?start=${state.overview.today}&days=1`);
    const todayAppointments = todaySchedule.appointments;
    const filterOptions = (field) => [...new Set(todayAppointments.map((item) => item[field]).filter(Boolean))]
      .sort((left, right) => left.localeCompare(right))
      .map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
    root.innerHTML = `${pageHeading("Practice pulse", "Today at EIC", `<button class="primary-button" data-overview-schedule>${icon("calendar")}Open calendar</button>`)}
      <div class="metric-grid">
        ${metricCard("Appointments today", state.overview.appointmentsToday, "Across the clinical team")}
        ${metricCard("Active patients", state.overview.patients, `${state.overview.activeRequirements.toLocaleString("en-IN")} active therapy requirements`)}
        ${metricCard("Clinical team", state.overview.therapists, "Across all therapy departments", "accent")}
        ${metricCard("Needs attention", state.overview.needsAttention, "Released or disrupted appointments", state.overview.needsAttention ? "warn" : "")}
      </div>
      <div class="content-grid">
        <section class="panel"><div class="panel-header"><div><h2>Today’s sessions</h2><p>${formatDate(parseLocalDate(state.overview.today), { weekday: "long", day: "numeric", month: "long" })}</p></div><span class="soft-pill" id="overviewSessionCount">${todayAppointments.length} appointments</span></div>
          <div class="overview-session-filters" aria-label="Filter today’s sessions">
            <label>Branch<select id="overviewBranchFilter"><option value="">All branches</option>${filterOptions("location")}</select></label>
            <label>Therapist<select id="overviewTherapistFilter"><option value="">All therapists</option>${filterOptions("therapistName")}</select></label>
            <label>Patient<select id="overviewPatientFilter"><option value="">All patients</option>${filterOptions("patientName")}</select></label>
            <button class="secondary-button overview-filter-clear" type="button" id="clearOverviewFilters">Clear filters</button>
          </div>
          <div id="overviewSessionList"></div>
        </section>
        <aside class="panel"><div class="panel-header"><div><h2>Planning readiness</h2><p>Current local workspace</p></div></div><div class="panel-body">
          <div class="detail-list">
            <div class="detail-item"><span>${icon("patients")}</span><div><small>Patient records</small><strong>${state.overview.patients.toLocaleString("en-IN")} ready</strong></div></div>
            <div class="detail-item"><span>${icon("team")}</span><div><small>Active therapists</small><strong>${state.overview.therapists.toLocaleString("en-IN")} available</strong></div></div>
            <div class="detail-item"><span>${icon("activity")}</span><div><small>Import warnings</small><strong>${state.overview.importWarnings.toLocaleString("en-IN")} recorded for review</strong></div></div>
          </div>
          <button class="primary-button" data-overview-generate style="width:100%;margin-top:18px">Generate the next 14 days</button>
        </div></aside>
      </div>`;
    $('[data-overview-schedule]').addEventListener("click", () => setView("schedule"));
    $('[data-overview-generate]').addEventListener("click", () => setView("generator"));
    makeSearchableSelect($("#overviewBranchFilter", root), "Search branches");
    makeSearchableSelect($("#overviewTherapistFilter", root), "Search therapists");
    makeSearchableSelect($("#overviewPatientFilter", root), "Search patients");
    const renderTodaySessions = () => {
      const branch = $("#overviewBranchFilter", root).value;
      const therapist = $("#overviewTherapistFilter", root).value;
      const patient = $("#overviewPatientFilter", root).value;
      const matching = todayAppointments.filter((item) => (
        (!branch || item.location === branch)
        && (!therapist || item.therapistName === therapist)
        && (!patient || item.patientName === patient)
      ));
      const visible = matching.slice(0, 8);
      $("#overviewSessionCount", root).textContent = `${matching.length} appointment${matching.length === 1 ? "" : "s"}`;
      $("#overviewSessionList", root).innerHTML = visible.length
        ? `<div>${visible.map((item) => `<button class="agenda-row" data-overview-appointment="${item.id}"><span class="agenda-time">${escapeHtml(item.time.split("–")[0])}</span><span class="agenda-person"><strong>${escapeHtml(item.patientName)}</strong><small>${escapeHtml(item.patientCode)} · ${escapeHtml(item.therapy)}</small></span><span class="agenda-clinician"><strong>${escapeHtml(item.therapistName)}</strong><small>${escapeHtml(item.therapistCode)}</small></span><span class="soft-pill">${escapeHtml(item.location)}</span><span class="status-pill status-${item.status.toLowerCase()}">${escapeHtml(formatStatus(item.status))}</span>${icon("chevron-right")}</button>`).join("")}</div>${matching.length > visible.length ? `<p class="overview-session-note">Showing the first ${visible.length} matching sessions.</p>` : ""}`
        : `<div class="panel-empty overview-empty">${icon("calendar")}<strong>No sessions match these filters</strong><span>Clear one or more filters to see today’s appointments.</span></div>`;
      $$('[data-overview-appointment]', root).forEach((button) => button.addEventListener("click", () => {
        const item = todayAppointments.find((appointment) => String(appointment.id) === button.dataset.overviewAppointment);
        if (item) openAppointment(item);
      }));
    };
    ["#overviewBranchFilter", "#overviewTherapistFilter", "#overviewPatientFilter"].forEach((selector) => {
      $(selector, root).addEventListener("change", renderTodaySessions);
    });
    $("#clearOverviewFilters", root).addEventListener("click", () => {
      ["#overviewBranchFilter", "#overviewTherapistFilter", "#overviewPatientFilter"].forEach((selector) => {
        $(selector, root).value = "";
        makeSearchableSelect($(selector, root), $(selector, root).closest(".searchable-select")?.dataset.placeholder || "Search");
      });
      renderTodaySessions();
    });
    renderTodaySessions();
  } catch (error) { root.innerHTML = errorState(error.message); }
}

function metricCard(label, value, note, style = "") {
  return `<article class="metric-card ${style}"><span class="metric-label">${escapeHtml(label)}</span><div class="metric-value">${Number(value).toLocaleString("en-IN")}</div><span class="metric-note">${escapeHtml(note)}</span></article>`;
}

async function loadPatientsView() {
  const root = $("#view-patients");
  root.innerHTML = `<div class="loading-state"><span class="spinner"></span>Loading patient directory…</div>`;
  try {
    const payload = await api(`/api/patients?q=${encodeURIComponent(state.patientQuery)}`);
    state.patients = payload.patients;
    state.patientPage = 1;
    renderPatientsView();
  } catch (error) { root.innerHTML = errorState(error.message); }
}

function renderPatientsView() {
  const root = $("#view-patients");
  const pageSize = 25;
  const pages = Math.max(1, Math.ceil(state.patients.length / pageSize));
  state.patientPage = Math.min(state.patientPage, pages);
  const start = (state.patientPage - 1) * pageSize;
  const rows = state.patients.slice(start, start + pageSize);
  root.innerHTML = `${pageHeading("Care directory", "Patients")}
    <div class="directory-toolbar"><label class="search-field">${icon("search")}<input id="patientSearch" type="search" value="${escapeHtml(state.patientQuery)}" placeholder="Search by patient name or code" aria-label="Search patients"></label><span class="soft-pill">${state.patients.length.toLocaleString("en-IN")} patients</span></div>
    <div class="table-shell"><table class="data-table"><thead><tr><th>Patient ID</th><th>Patient</th><th>Preference day</th><th>Preferred time</th><th>Status</th><th></th></tr></thead><tbody>
      ${rows.map((item) => `<tr><td><strong>${escapeHtml(item.code)}</strong></td><td><div class="person-cell"><span class="avatar">${escapeHtml(initials(item.name === "Name not provided" ? item.code : item.name))}</span><span><strong>${escapeHtml(item.name)}</strong><small>${item.therapies} active therap${item.therapies === 1 ? "y" : "ies"}</small></span></div></td><td>${escapeHtml(item.preferenceDay)}</td><td>${escapeHtml(item.preferencePeriod)}</td><td><span class="status-pill ${item.active ? "status-confirmed" : "status-needs_rescheduling"}">${item.active ? "Active" : "Inactive"}</span></td><td><button class="ghost-button" type="button" data-patient-preferences="${item.id}">Edit</button></td></tr>`).join("")}
    </tbody></table><div class="table-footer"><span>Showing ${state.patients.length ? start + 1 : 0}–${Math.min(start + pageSize, state.patients.length)} of ${state.patients.length.toLocaleString("en-IN")}</span><div><button class="ghost-button" id="patientPrev" ${state.patientPage === 1 ? "disabled" : ""}>Previous</button><button class="ghost-button" id="patientNext" ${state.patientPage === pages ? "disabled" : ""}>Next</button></div></div></div>`;
  let searchTimer;
  $("#patientSearch").addEventListener("input", (event) => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => { state.patientQuery = event.target.value.trim(); loadPatientsView(); }, 280);
  });
  $("#patientPrev").addEventListener("click", () => { state.patientPage--; renderPatientsView(); });
  $("#patientNext").addEventListener("click", () => { state.patientPage++; renderPatientsView(); });
  $$('[data-patient-preferences]', root).forEach((button) => button.addEventListener("click", () => {
    const patient = state.patients.find((item) => String(item.id) === button.dataset.patientPreferences);
    if (patient) openPatientPreferences(patient);
  }));
}

function ensurePatientPreferencesDrawer() {
  let drawer = $("#patientPreferencesDrawer");
  if (drawer) return drawer;
  drawer = document.createElement("aside");
  drawer.id = "patientPreferencesDrawer";
  drawer.className = "drawer";
  drawer.setAttribute("aria-hidden", "true");
  drawer.setAttribute("aria-labelledby", "patientPreferencesTitle");
  document.body.append(drawer);
  return drawer;
}

async function openPatientPreferences(patient) {
  const drawer = ensurePatientPreferencesDrawer();
  drawer.innerHTML = `<div class="drawer-header"><div><p class="eyebrow">Patient preferences</p><h2 id="patientPreferencesTitle">Loading availability…</h2></div><button class="icon-button" data-close-patient-preferences aria-label="Close">${icon("close")}</button></div><div class="drawer-body"><div class="loading-state"><span class="spinner"></span>Loading preference table…</div></div>`;
  $('[data-close-patient-preferences]', drawer).addEventListener("click", closeDrawers);
  openDrawer("patientPreferencesDrawer");
  try {
    const payload = await api(`/api/patients/${patient.id}/preferences`);
    drawer.innerHTML = `<div class="drawer-header"><div><p class="eyebrow">Patient preferences</p><h2 id="patientPreferencesTitle">${escapeHtml(payload.patient.name)}</h2><p>${escapeHtml(payload.patient.code)}</p></div><button class="icon-button" data-close-patient-preferences aria-label="Close">${icon("close")}</button></div>
      <form class="drawer-body preference-form" id="patientPreferencesForm"><p class="candidate-intro">The scheduler ranks the selected day and time band ahead of other available slots. This is a preference, not a restriction: it can use another time when needed to complete therapy safely.</p>
        <label>Preferred day<select id="patientPreferenceDay"><option value="">No priority</option>${["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"].map((label, day) => `<option value="${day}" ${payload.preference.day === day ? "selected" : ""}>${label}</option>`).join("")}</select></label>
        <label>Preferred time<select id="patientPreferencePeriod"><option value="">No priority</option><option value="morning" ${payload.preference.period === "morning" ? "selected" : ""}>Morning · 09:00–12:35</option><option value="afternoon" ${payload.preference.period === "afternoon" ? "selected" : ""}>Afternoon · 13:15–16:50</option></select></label>
        <button class="primary-button" type="submit">Save preference</button>
      </form>`;
    $('[data-close-patient-preferences]', drawer).addEventListener("click", closeDrawers);
    $("#patientPreferencesForm", drawer).addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $('button[type="submit"]', event.currentTarget);
      button.disabled = true;
      button.textContent = "Saving…";
      try {
        await api(`/api/patients/${patient.id}/preferences`, { method: "POST", body: JSON.stringify({ day: $("#patientPreferenceDay", drawer).value, period: $("#patientPreferencePeriod", drawer).value }) });
        toast("Preference saved", "The next schedule will prioritize this day and time.");
        closeDrawers();
        await loadPatientsView();
      } catch (error) {
        toast("Couldn’t save preferences", error.message, "error");
        button.disabled = false;
        button.textContent = "Save preference";
      }
    });
  } catch (error) {
    drawer.innerHTML = `<div class="drawer-header"><div><p class="eyebrow">Patient preferences</p><h2 id="patientPreferencesTitle">Availability unavailable</h2></div><button class="icon-button" data-close-patient-preferences aria-label="Close">${icon("close")}</button></div><div class="drawer-body">${errorState(error.message)}</div>`;
    $('[data-close-patient-preferences]', drawer).addEventListener("click", closeDrawers);
  }
}

async function loadTeamView() {
  const root = $("#view-team");
  root.innerHTML = `<div class="loading-state"><span class="spinner"></span>Loading clinical team…</div>`;
  try {
    const week = isoDate(startOfWeek(state.currentDate));
    const payload = await api(`/api/therapists?start=${week}`);
    root.innerHTML = `${pageHeading("Capacity & coverage", "Clinical team", `<button class="secondary-button" data-team-calendar>${icon("calendar")}View team calendar</button>`)}
      <div class="table-shell"><table class="data-table"><thead><tr><th>Clinician</th><th>Department</th><th>Location</th><th>Weekly workload</th><th>Status</th></tr></thead><tbody>
      ${payload.therapists.map((item) => { const capacityLabel = item.capacity == null ? "Not supplied" : item.capacity; const utilization = item.capacity ? Math.min(100, Math.round(item.appointments / item.capacity * 100)) : 0; return `<tr><td><div class="person-cell"><span class="avatar">${escapeHtml(initials(item.name))}</span><span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.code)}</small></span></div></td><td>${escapeHtml(item.department)}</td><td>${escapeHtml(item.location)}</td><td><div style="display:flex;align-items:center;gap:10px"><div class="progress-track"><div class="progress-fill" style="width:${utilization}%"></div></div><span>${item.appointments} / ${capacityLabel}</span></div></td><td><span class="status-pill ${item.active ? "status-confirmed" : "status-needs_rescheduling"}">${item.active ? "Active" : "Inactive"}</span></td></tr>`; }).join("")}
      </tbody></table><div class="table-footer"><span>${payload.therapists.length} therapists · week of ${formatDate(parseLocalDate(payload.weekStart), { day: "numeric", month: "long", year: "numeric" })}</span><span>Capacity assumes 10 daily slots</span></div></div>`;
    $('[data-team-calendar]').addEventListener("click", () => setView("schedule"));
  } catch (error) { root.innerHTML = errorState(error.message); }
}

async function loadOperationsView() {
  const root = $("#view-operations");
  root.innerHTML = `<div class="loading-state"><span class="spinner"></span>Loading operations…</div>`;
  try {
    const [operations, patients] = await Promise.all([api("/api/operations"), api("/api/patients")]);
    const therapists = state.schedule?.therapists || [];
    root.innerHTML = `${pageHeading("Schedule continuity", "Operations")}
      <div class="operations-grid">
        <section class="panel"><div class="panel-header"><div><h2>Record an absence</h2><p>Affected appointments will be released for safe rescheduling.</p></div></div><form class="panel-body form-grid" id="absenceForm">
          <div class="segmented-field"><label><input type="radio" name="absenceKind" value="therapist" checked><span>Therapist</span></label><label><input type="radio" name="absenceKind" value="patient"><span>Patient</span></label></div>
          <label id="absenceEntityLabel">Therapist<select id="absenceEntity" required>${therapists.filter((item) => item.active).map((item) => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.code)}</option>`).join("")}</select></label>
          <label>Date<input id="absenceDate" type="date" value="${state.overview.today}" required></label>
          <label>Reason<textarea id="absenceReason" placeholder="Optional operational note"></textarea></label>
          <button class="primary-button" type="submit">Record full-day absence</button>
        </form></section>
        <div style="display:grid;gap:18px;align-content:start">
          <section class="panel"><div class="panel-header"><div><h2>Needs rescheduling</h2><p>Appointments released by an absence or disruption.</p></div><span class="soft-pill">${operations.needsRescheduling.length}</span></div>
            ${operations.needsRescheduling.length ? `<div>${operations.needsRescheduling.map((item) => `<button class="agenda-row" data-operation-appointment="${item.id}"><span class="agenda-time">${escapeHtml(item.time.split("–")[0])}</span><span class="agenda-person"><strong>${escapeHtml(item.patientName)}</strong><small>${escapeHtml(item.patientCode)} · ${escapeHtml(item.therapy)}</small></span><span class="agenda-clinician"><strong>${escapeHtml(item.therapistName)}</strong><small>${formatDate(parseLocalDate(item.date), { day: "numeric", month: "short" })}</small></span><span class="soft-pill">${escapeHtml(item.location)}</span><span class="status-pill status-needs_rescheduling">Needs rescheduling</span>${icon("chevron-right")}</button>`).join("")}</div>` : `<div class="panel-empty">${icon("check")}<strong>No released appointments</strong><span>The schedule has no appointments waiting for repair.</span></div>`}
          </section>
          <section class="panel"><div class="panel-header"><div><h2>Recent absences</h2><p>Latest therapist and patient availability changes.</p></div></div>
            ${operations.absences.length ? `<div>${operations.absences.map((item) => `<div class="absence-row"><span class="soft-pill">${escapeHtml(item.kind)}</span><span><strong>${escapeHtml(item.name)}</strong><br>${escapeHtml(item.code)}</span><span>${formatDate(parseLocalDate(item.date), { day: "numeric", month: "short", year: "numeric" })}</span><span>${escapeHtml(item.reason)}</span></div>`).join("")}</div>` : `<div class="panel-empty">${icon("operations")}<strong>No absences recorded</strong><span>Recent changes will appear here.</span></div>`}
          </section>
        </div>
      </div>`;
    const entitySelect = $("#absenceEntity");
    const label = $("#absenceEntityLabel");
    makeSearchableSelect(entitySelect, "Search therapists");
    $$('input[name="absenceKind"]').forEach((radio) => radio.addEventListener("change", () => {
      const kind = $('input[name="absenceKind"]:checked').value;
      label.firstChild.textContent = kind === "therapist" ? "Therapist" : "Patient";
      entitySelect.innerHTML = kind === "therapist"
        ? therapists.filter((item) => item.active).map((item) => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.code)}</option>`).join("")
        : patients.patients.filter((item) => item.active).map((item) => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.code)}</option>`).join("");
      makeSearchableSelect(entitySelect, `Search ${kind === "therapist" ? "therapists" : "patients"}`);
    }));
    $("#absenceForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $('button[type="submit"]', event.currentTarget);
      button.disabled = true; button.textContent = "Recording…";
      try {
        const result = await api("/api/absences", { method: "POST", body: JSON.stringify({ kind: $('input[name="absenceKind"]:checked').value, entityId: Number(entitySelect.value), date: $("#absenceDate").value, reason: $("#absenceReason").value.trim() }) });
        const candidates = (result.replacementGroups || []).reduce((total, group) => total + group.candidates.length, 0);
        toast("Absence recorded", candidates ? `${candidates} continuity-preserving replacement option${candidates === 1 ? "" : "s"} found.` : `${result.affected} appointment${result.affected === 1 ? "" : "s"} now need rescheduling.`);
        state.overview = await api("/api/overview");
        await loadOperationsView();
      } catch (error) { toast("Couldn’t record absence", error.message, "error"); button.disabled = false; button.textContent = "Record full-day absence"; }
    });
    $$('[data-operation-appointment]').forEach((button) => button.addEventListener("click", async () => {
      const item = operations.needsRescheduling.find((appointment) => String(appointment.id) === button.dataset.operationAppointment);
      if (!item) return;
      button.disabled = true;
      try {
        const group = await api(`/api/appointments/${item.id}/replacement-candidates`);
        showReplacementCandidates([group]);
      } catch (error) { toast("Couldn’t find replacements", error.message, "error"); }
      finally { button.disabled = false; }
    }));
  } catch (error) { root.innerHTML = errorState(error.message); }
}

function loadGeneratorView() {
  const root = $("#view-generator");
  root.innerHTML = `${pageHeading("Rolling therapy planning", "Schedule generator")}
    <section class="generator-hero">
      <div><p class="eyebrow" style="color:#5ed5cd">Today through two future working weeks</p><h2>Build a safe rolling schedule around every child, therapist and room.</h2><p>The planner preserves saved visits, fills only the remaining working days this week, prioritizes disrupted appointments, and carries unmet demand into the next week.</p><div class="rule-strip"><span>${icon("check")}Patient availability</span><span>${icon("check")}Therapist coverage</span><span>${icon("check")}Absence priority</span><span>${icon("check")}Weekly targets</span></div></div>
      <form class="generator-form" id="generatorForm">
        <label>Run from<input id="generatorStart" type="date" value="${state.overview.today}" required></label>
        <label>Search time<select id="generatorSearchSeconds"><option value="20">Quick preview · about 20 seconds</option><option value="60">Thorough · up to 1 minute</option><option value="120">Extended · up to 2 minutes</option><option value="300">Deep search · up to 5 minutes</option></select></label>
        <button class="primary-button" type="submit">Generate validated preview</button>
        <p class="fine-print">The planner always uses best effort: it keeps every safety rule, schedules only from the selected day forward, and reports any demand that still needs a later week. Existing active appointments remain fixed. Nothing is saved until you approve the preview.</p>
      </form>
    </section>
    <div id="generatorResults">${state.latestPreview ? renderGeneratorResults(state.latestPreview) : `<section class="panel"><div class="panel-empty">${icon("activity")}<strong>No preview generated yet</strong><span>Choose the planning window above to see coverage and unmet demand.</span></div></section>`}</div>`;
  $("#generatorForm").addEventListener("submit", generateSchedule);
  bindSavePreview();
}

async function generateSchedule(event) {
  event.preventDefault();
  const button = $('button[type="submit"]', event.currentTarget);
  const start = $("#generatorStart").value;
  const searchSeconds = Number($("#generatorSearchSeconds").value);
  const searchLabel = searchSeconds >= 60 ? `${searchSeconds / 60} minute${searchSeconds === 60 ? "" : "s"}` : `${searchSeconds} seconds`;
  button.disabled = true;
  button.innerHTML = `<span class="spinner" style="width:16px;height:16px;border-color:rgba(255,255,255,.35);border-top-color:white"></span>Planning ahead…`;
  $("#generatorResults").innerHTML = `<section class="panel"><div class="loading-state" style="min-height:260px"><span class="spinner"></span><strong>Balancing remaining weekly demand and future capacity</strong><span>This can take up to ${searchLabel}. The result is independently validated before it can be saved.</span></div></section>`;
  try {
    const result = await api("/api/schedules/generate", { method: "POST", body: JSON.stringify({ start, searchSeconds }) });
    state.latestPreview = result;
    $("#generatorResults").innerHTML = renderGeneratorResults(result);
    bindSavePreview();
    toast("Schedule preview ready", `${result.scheduled.toLocaleString("en-IN")} sessions placed; ${result.unscheduled.toLocaleString("en-IN")} remain unscheduled.`);
  } catch (error) {
    $("#generatorResults").innerHTML = errorState(error.message);
    toast("Scheduling didn’t complete", error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Generate validated preview";
  }
}

function renderGeneratorResults(result) {
  const validationTotal = Object.values(result.validation || {}).reduce((sum, value) => sum + Number(value || 0), 0);
  return `<div class="result-banner ${result.canSave ? "" : "error"}"><div><h3>${result.canSave ? "Preview passed every validation check" : "Preview needs review before it can be saved"}</h3><p>${result.status} · ${validationTotal} validation issue${validationTotal === 1 ? "" : "s"} · New visits from ${escapeHtml(result.planningStart || result.start)} to ${escapeHtml(result.end)}</p></div><div class="heading-actions"><button class="secondary-button" id="reviewPreview">${icon("calendar")}Review calendar</button>${result.canSave ? `<button class="primary-button" id="savePreview">Save as proposed</button>` : ""}</div></div>
    <div class="metric-grid">
      ${metricCard("Requested sessions", result.requested, "Across the rolling planning window")}
      ${metricCard("Scheduled", result.scheduled, `${result.requested ? Math.round(result.scheduled / result.requested * 100) : 0}% coverage`)}
      ${metricCard("Unscheduled", result.unscheduled, "Never forced through a constraint", result.unscheduled ? "accent" : "")}
      ${metricCard("Solver status", 0, result.status).replace('<div class="metric-value">0</div>', `<div class="metric-value" style="font-size:1.15rem;margin-top:20px">${escapeHtml(result.status)}</div>`)}
    </div>
    <section class="panel"><div class="panel-header"><div><h2>Unmet demand</h2><p>Clear reasons for every session the planner could not place.</p></div><span class="soft-pill">${result.unmet.length.toLocaleString("en-IN")} requirements</span></div>
      ${result.unmet.length ? `<div class="unmet-list">${result.unmet.map((item) => `<div class="unmet-row"><strong>${escapeHtml(item.patientCode)}</strong><span>${escapeHtml(item.therapy)}</span><span class="unmet-reasons"><strong>Primary: ${escapeHtml(item.reasonLabel)}</strong>${item.secondaryReasonLabels?.length ? `<small>Also affected by: ${item.secondaryReasonLabels.map(escapeHtml).join(" · ")}</small>` : `<small>No additional blocking reason detected</small>`}</span><strong>${item.missing_sessions}</strong></div>`).join("")}</div>` : `<div class="panel-empty">${icon("check")}<strong>All requested sessions were placed</strong><span>No unmet therapy demand remains in this planning window.</span></div>`}
    </section>`;
}

function bindSavePreview() {
  const reviewButton = $("#reviewPreview");
  if (reviewButton && state.latestPreview) {
    reviewButton.addEventListener("click", async () => {
      state.showPreview = true;
      state.currentDate = parseLocalDate(state.latestPreview.start);
      setView("schedule");
      await loadSchedule();
    });
  }
  const button = $("#savePreview");
  if (!button || !state.latestPreview) return;
  button.addEventListener("click", async () => {
    button.disabled = true; button.textContent = "Saving…";
    try {
      const result = await api(`/api/schedules/${state.latestPreview.previewId}/save`, { method: "POST", body: "{}" });
      toast("Proposed schedule saved", result.message);
      state.latestPreview = null;
      state.showPreview = false;
      await loadSchedule();
      setView("schedule");
    } catch (error) { toast("Couldn’t save preview", error.message, "error"); button.disabled = false; button.textContent = "Save as proposed"; }
  });
}

function openDrawer(id) {
  closeDrawers();
  const drawer = document.getElementById(id);
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  $("#drawerBackdrop").hidden = false;
  document.body.style.overflow = "hidden";
}

function closeDrawers() {
  $$(".drawer.open").forEach((drawer) => { drawer.classList.remove("open"); drawer.setAttribute("aria-hidden", "true"); });
  $("#drawerBackdrop").hidden = true;
  document.body.style.overflow = "";
}

function ensureAppointmentDrawer() {
  let drawer = $("#appointmentDrawer");
  if (drawer) return drawer;
  drawer = document.createElement("aside");
  drawer.id = "appointmentDrawer";
  drawer.className = "drawer";
  drawer.setAttribute("aria-hidden", "true");
  drawer.setAttribute("aria-labelledby", "appointmentDrawerTitle");
  document.body.append(drawer);
  return drawer;
}

function openAppointment(item) {
  const drawer = ensureAppointmentDrawer();
  const canChange = !item.preview && ["PROPOSED", "PENDING_CONFIRMATION", "CONFIRMED"].includes(item.status);
  drawer.innerHTML = `<div class="drawer-header"><div><p class="eyebrow">Appointment details</p><h2 id="appointmentDrawerTitle">${formatDate(parseLocalDate(item.date), { weekday: "long", day: "numeric", month: "long" })}</h2></div><button class="icon-button" data-close-appointment aria-label="Close appointment">${icon("close")}</button></div>
    <div class="drawer-body"><div class="appointment-hero"><span class="status-pill status-${item.status.toLowerCase()}">${escapeHtml(formatStatus(item.status))}</span><h3>${escapeHtml(item.patientName)}</h3><p>${escapeHtml(item.patientCode)} · ${escapeHtml(item.therapy)}</p></div>
      <div class="detail-list">
        <div class="detail-item"><span>${icon("clock")}</span><div><small>Time</small><strong>${escapeHtml(item.time)}</strong></div></div>
        <div class="detail-item"><span>${icon("user")}</span><div><small>Clinician</small><strong>${escapeHtml(item.therapistName)} · ${escapeHtml(item.therapistCode)}</strong></div></div>
        <div class="detail-item"><span>${icon("location")}</span><div><small>Location</small><strong>${escapeHtml(item.location)}</strong></div></div>
        <div class="detail-item"><span>${icon("activity")}</span><div><small>Schedule source</small><strong>${escapeHtml(item.source)}${item.bundle ? ` · bundle ${escapeHtml(item.bundle)}` : ""}</strong></div></div>
      </div>
      ${canChange ? `<section class="drawer-section"><h3>Move this visit</h3><form class="form-grid" id="moveAppointmentForm"><label>New date<input type="date" id="moveDate" value="${item.date}" required></label><label>New starting time<select id="moveSlot">${state.overview.slotLabels.map((label, index) => `<option value="${index}" ${index === item.slot ? "selected" : ""}>${escapeHtml(label)}</option>`).join("")}</select></label><button class="secondary-button" type="submit">Validate and move</button></form></section><div class="drawer-actions"><button class="danger-button" id="cancelAppointment">Record patient cancellation</button></div>` : ""}
    </div>`;
  $('[data-close-appointment]', drawer).addEventListener("click", closeDrawers);
  if (canChange) {
    $("#moveAppointmentForm", drawer).addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $('button[type="submit"]', event.currentTarget); button.disabled = true; button.textContent = "Validating…";
      try {
        const result = await api(`/api/appointments/${item.id}/move`, { method: "POST", body: JSON.stringify({ date: $("#moveDate").value, slot: Number($("#moveSlot").value) }) });
        toast("Appointment moved", result.message); closeDrawers(); await loadSchedule();
      } catch (error) { toast("Move blocked by scheduling rules", error.message, "error"); button.disabled = false; button.textContent = "Validate and move"; }
    });
    $("#cancelAppointment", drawer).addEventListener("click", async () => {
      if (!window.confirm(`Record a patient cancellation for ${item.patientName} on ${formatDate(parseLocalDate(item.date), { day: "numeric", month: "long" })}?`)) return;
      const button = $("#cancelAppointment", drawer); button.disabled = true; button.textContent = "Recording…";
      try {
        const result = await api(`/api/appointments/${item.id}/cancel`, { method: "POST", body: "{}" });
        const candidates = (result.replacementGroups || []).reduce((total, group) => total + group.candidates.length, 0);
        toast("Cancellation recorded", candidates ? `${candidates} continuity-preserving replacement option${candidates === 1 ? "" : "s"} found.` : "The opening is ready for the next repair run.");
        closeDrawers(); await loadSchedule();
      } catch (error) { toast("Couldn’t record cancellation", error.message, "error"); button.disabled = false; button.textContent = "Record patient cancellation"; }
    });
  }
  openDrawer("appointmentDrawer");
}

function showReplacementCandidates(groups) {
  const drawer = ensureAppointmentDrawer();
  drawer.innerHTML = `<div class="drawer-header"><div><p class="eyebrow">Released appointment</p><h2 id="appointmentDrawerTitle">Possible replacements</h2></div><button class="icon-button" data-close-appointment aria-label="Close">${icon("close")}</button></div>
    <div class="drawer-body"><p class="candidate-intro">Only children assigned to the same therapist, for the same therapy and branch, are shown. This list does not book anyone automatically.</p>
      ${groups.map((group) => `<section class="candidate-group"><h3>${escapeHtml(group.therapy)}</h3><p>${escapeHtml(group.branch)} · ${formatDate(parseLocalDate(group.date), { day: "numeric", month: "short" })} · ${escapeHtml(group.time)}</p>
        ${group.candidates.length ? `<div class="candidate-list">${group.candidates.map((candidate) => `<div class="candidate-row"><span class="avatar">${escapeHtml(initials(candidate.patient_name || candidate.patient_code))}</span><span><strong>${escapeHtml(candidate.patient_name || candidate.patient_code)}</strong><small>${escapeHtml(candidate.patient_code)} · ${escapeHtml(candidate.weekly_progress)} sessions scheduled</small></span></div>`).join("")}</div>` : `<div class="panel-empty candidate-empty"><strong>No suitable child is free for this opening</strong><span>Try the schedule generator to repair the released appointment.</span></div>`}
      </section>`).join("")}
    </div>`;
  $('[data-close-appointment]', drawer).addEventListener("click", closeDrawers);
  openDrawer("appointmentDrawer");
}

initialize();
