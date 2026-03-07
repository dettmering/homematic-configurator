// ── State ────────────────────────────────────────────────────────────────────

const DAY_NAMES = {
  MONDAY: 'Montag', TUESDAY: 'Dienstag', WEDNESDAY: 'Mittwoch',
  THURSDAY: 'Donnerstag', FRIDAY: 'Freitag', SATURDAY: 'Samstag', SUNDAY: 'Sonntag',
};
const DAYS_ORDER = ['MONDAY','TUESDAY','WEDNESDAY','THURSDAY','FRIDAY','SATURDAY','SUNDAY'];

let currentDevice = null;
let schedule = {}; // { MONDAY: [{end:"06:00", temp:17}, ...], ... }
let editingSlot = null; // { day, index }

// ── Init ─────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  loadDevices();
  document.getElementById('btn-save').addEventListener('click', saveSchedule);
  document.getElementById('btn-reload').addEventListener('click', () => { if (currentDevice) loadSchedule(currentDevice); });
  document.getElementById('modal-save').addEventListener('click', modalSave);
  document.getElementById('modal-delete').addEventListener('click', modalDelete);
  document.getElementById('modal-cancel').addEventListener('click', modalClose);
  document.getElementById('modal-overlay').addEventListener('click', (e) => {
    if (e.target === e.currentTarget) modalClose();
  });
});

// ── API ──────────────────────────────────────────────────────────────────────

async function api(url, opts = {}) {
  try {
    const res = await fetch(url, opts);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    return data;
  } catch (e) {
    showToast(e.message, 'error');
    throw e;
  }
}

// ── Devices ──────────────────────────────────────────────────────────────────

async function loadDevices() {
  const status = document.getElementById('connection-status');
  const loading = document.getElementById('devices-loading');

  try {
    const cfg = await api('/api/config');
    status.textContent = `RPC: ${cfg.rpc_url}`;
    const data = await api('/api/devices');
    loading.style.display = 'none';

    const ul = document.getElementById('devices');
    ul.innerHTML = '';

    if (data.devices.length === 0) {
      loading.style.display = 'block';
      loading.textContent = 'Keine Thermostate gefunden';
      return;
    }

    data.devices.forEach(dev => {
      const li = document.createElement('li');
      li.innerHTML = `${dev.name || dev.address}<span class="dev-type">${dev.type} (ID: ${dev.id})</span>`;
      li.addEventListener('click', () => selectDevice(dev, li));
      ul.appendChild(li);
    });
  } catch (e) {
    loading.textContent = 'Fehler beim Laden der Geraete';
    status.textContent = 'Verbindungsfehler';
  }
}

function selectDevice(dev, li) {
  document.querySelectorAll('#devices li').forEach(el => el.classList.remove('active'));
  li.classList.add('active');
  currentDevice = dev;
  document.getElementById('device-name').textContent = `${dev.name || dev.address} (${dev.type})`;
  document.getElementById('editor').classList.remove('hidden');
  loadSchedule(dev);
}

// ── Schedule loading ─────────────────────────────────────────────────────────

async function loadSchedule(dev) {
  try {
    const data = await api(`/api/devices/${dev.id}/schedule`);
    schedule = {};
    DAYS_ORDER.forEach(day => {
      schedule[day] = data.schedule[day] || [{ end: '24:00', temp: 17 }];
    });
    renderSchedule();
  } catch (e) {
    // already toasted
  }
}

// ── Rendering ────────────────────────────────────────────────────────────────

function tempToColor(temp) {
  // 4.5 -> blue, 17 -> blue, 20 -> amber, 24+ -> red
  if (temp <= 16) return '#3b82f6';
  if (temp <= 18) return '#06b6d4';
  if (temp <= 20) return '#22c55e';
  if (temp <= 22) return '#f59e0b';
  if (temp <= 24) return '#f97316';
  return '#ef4444';
}

function timeToMinutes(hhmm) {
  const [h, m] = hhmm.split(':').map(Number);
  return h * 60 + m;
}

function minutesToTime(mins) {
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}`;
}

function renderSchedule() {
  const grid = document.getElementById('schedule-grid');
  grid.innerHTML = '';

  DAYS_ORDER.forEach(day => {
    const row = document.createElement('div');
    row.className = 'day-row';

    const header = document.createElement('div');
    header.className = 'day-header';

    const nameEl = document.createElement('span');
    nameEl.className = 'day-name';
    nameEl.textContent = DAY_NAMES[day];

    const actions = document.createElement('div');
    actions.className = 'day-actions';

    // Add slot button
    const addBtn = document.createElement('button');
    addBtn.className = 'btn-small';
    addBtn.textContent = '+ Slot';
    addBtn.addEventListener('click', () => addSlot(day));
    actions.appendChild(addBtn);

    // Copy dropdown
    const copyControls = document.createElement('div');
    copyControls.className = 'copy-controls';
    const copySelect = document.createElement('select');
    copySelect.innerHTML = '<option value="">Kopieren nach...</option>';
    DAYS_ORDER.forEach(d => {
      if (d !== day) {
        const opt = document.createElement('option');
        opt.value = d;
        opt.textContent = DAY_NAMES[d];
        copySelect.appendChild(opt);
      }
    });
    const allOpt = document.createElement('option');
    allOpt.value = '__ALL__';
    allOpt.textContent = 'Alle Tage';
    copySelect.appendChild(allOpt);

    copySelect.addEventListener('change', () => {
      if (copySelect.value === '__ALL__') {
        DAYS_ORDER.forEach(d => {
          if (d !== day) schedule[d] = JSON.parse(JSON.stringify(schedule[day]));
        });
        renderSchedule();
      } else if (copySelect.value) {
        schedule[copySelect.value] = JSON.parse(JSON.stringify(schedule[day]));
        renderSchedule();
      }
      copySelect.value = '';
    });
    copyControls.appendChild(copySelect);
    actions.appendChild(copyControls);

    header.appendChild(nameEl);
    header.appendChild(actions);
    row.appendChild(header);

    // Timeline
    const timelineContainer = document.createElement('div');
    timelineContainer.className = 'timeline-container';

    const slots = schedule[day];
    let prevEnd = 0;

    slots.forEach((slot, idx) => {
      const endMin = timeToMinutes(slot.end);
      const startMin = prevEnd;
      const duration = endMin - startMin;
      if (duration <= 0) return;

      const leftPct = (startMin / 1440) * 100;
      const widthPct = (duration / 1440) * 100;

      const el = document.createElement('div');
      el.className = 'timeline-slot';
      el.style.left = `${leftPct}%`;
      el.style.width = `${widthPct}%`;
      el.style.background = tempToColor(slot.temp);

      const startTime = minutesToTime(startMin);
      const label = document.createElement('div');
      label.className = 'slot-label';

      // Only show label if slot is wide enough
      if (widthPct > 5) {
        label.innerHTML = `<span>${slot.temp}&deg;C</span><span class="slot-time">${startTime}-${slot.end}</span>`;
      } else if (widthPct > 2.5) {
        label.innerHTML = `<span>${slot.temp}&deg;</span>`;
      }

      el.appendChild(label);
      el.addEventListener('click', (e) => {
        e.stopPropagation();
        openSlotEditor(day, idx);
      });

      timelineContainer.appendChild(el);
      prevEnd = endMin;
    });

    row.appendChild(timelineContainer);

    // Time axis
    const axis = document.createElement('div');
    axis.className = 'time-axis';
    for (let h = 0; h <= 24; h += 3) {
      const span = document.createElement('span');
      span.textContent = `${h}:00`;
      axis.appendChild(span);
    }
    row.appendChild(axis);

    grid.appendChild(row);
  });
}

// ── Slot editing ─────────────────────────────────────────────────────────────

function openSlotEditor(day, index) {
  editingSlot = { day, index };
  const slot = schedule[day][index];

  const endInput = document.getElementById('slot-end');
  const tempInput = document.getElementById('slot-temp');

  // time input doesn't support 24:00, use 23:59 as proxy
  if (slot.end === '24:00') {
    endInput.value = '23:59';
  } else {
    endInput.value = slot.end;
  }
  tempInput.value = slot.temp;

  // Can't delete if it's the only slot
  document.getElementById('modal-delete').disabled = schedule[day].length <= 1;

  document.getElementById('modal-overlay').classList.remove('hidden');
}

function modalClose() {
  document.getElementById('modal-overlay').classList.add('hidden');
  editingSlot = null;
}

function modalSave() {
  if (!editingSlot) return;
  const { day, index } = editingSlot;

  let endVal = document.getElementById('slot-end').value;
  const tempVal = parseFloat(document.getElementById('slot-temp').value);

  if (!endVal || isNaN(tempVal)) {
    showToast('Bitte gueltige Werte eingeben', 'error');
    return;
  }

  // Round temp to 0.5
  const roundedTemp = Math.round(tempVal * 2) / 2;

  // If it's the last slot, force 24:00
  const isLast = index === schedule[day].length - 1;
  if (isLast) {
    endVal = '24:00';
  } else if (endVal === '23:59') {
    endVal = '24:00';
  }

  schedule[day][index] = { end: endVal, temp: roundedTemp };

  // Re-sort by end time
  schedule[day].sort((a, b) => timeToMinutes(a.end) - timeToMinutes(b.end));

  // Ensure last slot ends at 24:00
  const lastSlot = schedule[day][schedule[day].length - 1];
  lastSlot.end = '24:00';

  modalClose();
  renderSchedule();
}

function modalDelete() {
  if (!editingSlot) return;
  const { day, index } = editingSlot;

  if (schedule[day].length <= 1) return;

  schedule[day].splice(index, 1);

  // Ensure last slot ends at 24:00
  schedule[day][schedule[day].length - 1].end = '24:00';

  modalClose();
  renderSchedule();
}

function addSlot(day) {
  const slots = schedule[day];
  if (slots.length >= 13) {
    showToast('Maximal 13 Slots pro Tag', 'error');
    return;
  }

  // Find a good split point: split the longest slot
  let longestIdx = 0;
  let longestDur = 0;
  let prevEnd = 0;
  slots.forEach((s, i) => {
    const end = timeToMinutes(s.end);
    const dur = end - prevEnd;
    if (dur > longestDur) {
      longestDur = dur;
      longestIdx = i;
    }
    prevEnd = end;
  });

  const slotEnd = timeToMinutes(slots[longestIdx].end);
  const slotStart = longestIdx > 0 ? timeToMinutes(slots[longestIdx - 1].end) : 0;
  const midpoint = Math.round((slotStart + slotEnd) / 2);
  // Round to 5 min
  const splitTime = Math.round(midpoint / 5) * 5;

  const newSlot = { end: minutesToTime(splitTime), temp: slots[longestIdx].temp };
  slots.splice(longestIdx, 0, newSlot);

  renderSchedule();
  // Open editor for the new slot
  openSlotEditor(day, longestIdx);
}

// ── Save ─────────────────────────────────────────────────────────────────────

async function saveSchedule() {
  if (!currentDevice) return;

  const btn = document.getElementById('btn-save');
  btn.disabled = true;
  btn.textContent = 'Speichere...';

  try {
    await api(`/api/devices/${currentDevice.id}/schedule`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        peer_id: currentDevice.id,
        days: schedule,
      }),
    });
    showToast('Wochenplan gespeichert! (Bei Batteriegeraeten ggf. erst nach Wakeup wirksam)', 'success');
  } catch (e) {
    // already toasted
  } finally {
    btn.disabled = false;
    btn.textContent = 'Speichern';
  }
}

// ── Toast ────────────────────────────────────────────────────────────────────

function showToast(msg, type = 'success') {
  const toast = document.getElementById('toast');
  toast.textContent = msg;
  toast.className = type;
  setTimeout(() => { toast.className = 'hidden'; }, 4000);
}
