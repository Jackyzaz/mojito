// Flood risk map: zones coloured by the expected flooded share for the
// selected forecast day, plus the X.44 water level chart.

const THAI_MONTHS = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."];
const ALERT_COLORS = { normal: "#2e8b57", watch: "#e0b000", bank: "#d32f2f", flood: "#8e1b1b" };

const state = { forecast: null, horizon: 1, zonesLayer: null, playing: null, dates: null };

const map = L.map("map", { zoomControl: true }).setView([7.0, 100.47], 11);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: "&copy; OpenStreetMap contributors",
  maxZoom: 18,
  className: "basemap",
}).addTo(map);

function thaiDate(iso, withYear = false) {
  const [y, m, d] = iso.split("-").map(Number);
  const text = `${d} ${THAI_MONTHS[m - 1]}`;
  return withYear ? `${text} ${String(y + 543).slice(2)}` : text;
}

// Yellow -> red -> dark red, transparent when nothing is expected to flood
function shareColor(share) {
  const stops = [
    [0.0, [255, 237, 160]],
    [0.25, [254, 178, 76]],
    [0.5, [240, 59, 32]],
    [1.0, [128, 0, 38]],
  ];
  let i = 1;
  while (i < stops.length - 1 && share > stops[i][0]) i += 1;
  const [s0, c0] = stops[i - 1];
  const [s1, c1] = stops[i];
  const t = Math.min(1, Math.max(0, (share - s0) / (s1 - s0)));
  const rgb = c0.map((v, k) => Math.round(v + (c1[k] - v) * t));
  return `rgb(${rgb.join(",")})`;
}

// The bank level of X.44 (m MSL) comes with every forecast
function bankLevel() {
  return state.forecast.alert_levels["ล้นตลิ่ง"];
}

// Water level relative to the X.44 bank, e.g. "ต่ำกว่าตลิ่ง 1.89 ม." / "ล้นตลิ่ง 2.82 ม."
function bankText(stage, unit = " ม.") {
  const d = stage - bankLevel();
  if (Math.abs(d) < 0.005) return "เสมอตลิ่ง";
  return d < 0 ? `ต่ำกว่าตลิ่ง ${(-d).toFixed(2)}${unit}` : `ล้นตลิ่ง ${d.toFixed(2)}${unit}`;
}

function signed(value) {
  return value > 0 ? `+${value}` : `${value}`;
}

function zoneStyle(feature) {
  const share = state.forecast ? state.forecast.horizons[state.horizon - 1].zone_share[feature.properties.index] : 0;
  return {
    stroke: false,
    fillColor: shareColor(share),
    fillOpacity: share > 0 ? 0.25 + 0.6 * Math.min(share * 1.5, 1) : 0,
  };
}

function zonePopup(feature) {
  const p = feature.properties;
  const horizon = state.forecast.horizons[state.horizon - 1];
  const share = horizon.zone_share[p.index];
  const start = p.start_stage <= 12
    ? bankText(p.start_stage)
    : `ล้นตลิ่งมากกว่า ${(12 - bankLevel()).toFixed(2)} ม. (เสี่ยงต่ำ)`;
  return `
    <strong>${thaiDate(horizon.date, true)}</strong><br>
    คาดว่าพื้นที่ในโซนนี้ท่วม <strong>${Math.round(share * 100)}%</strong><br>
    เริ่มท่วมเมื่อ X.44 <strong>${start}</strong><br>
    <span style="color:#5b6878">น้ำท่วมจริง พ.ย. 2568: ${Math.round((p.flooded_share_2025 || 0) * 100)}% ของโซน</span>`;
}

async function loadJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error((await response.text()) || response.statusText);
  return response.json();
}

async function loadZones() {
  const geojson = await loadJSON("/api/zones");
  geojson.features.forEach((feature, index) => { feature.properties.index = index; });
  state.zonesLayer = L.geoJSON(geojson, { style: zoneStyle }).addTo(map);
  state.zonesLayer.bindPopup((layer) => zonePopup(layer.feature));
  map.fitBounds(state.zonesLayer.getBounds(), { padding: [10, 10] });
}

async function loadStations() {
  const stations = await loadJSON("/api/stations");
  stations.forEach((s) => {
    const isCity = s.code === "X.44";
    L.circleMarker([s.lat, s.lon], {
      radius: isCity ? 8 : 5,
      color: "#fff",
      weight: 2,
      fillColor: "#1f6fb2",
      fillOpacity: 1,
    })
      .bindTooltip(`${s.code} · ${s.name}${s.min_bank ? ` (ตลิ่ง ${s.min_bank} ม.)` : ""}`, { permanent: isCity, direction: "right" })
      .addTo(map);
  });
}

function setBadge(element, alert) {
  element.textContent = alert.label;
  element.className = `badge ${alert.code}`;
}

// Big number coloured like its alert: green / yellow near the bank / red above it
function setValue(element, alert) {
  element.className = `value ${alert.code}`;
}

function renderDayButtons() {
  const container = document.getElementById("day-buttons");
  container.innerHTML = "";
  state.forecast.horizons.forEach((h) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = h.h === state.horizon ? "active" : "";
    button.innerHTML = `<span class="dot" style="background:${ALERT_COLORS[h.alert.code]}"></span>+${h.h} วัน<small>${thaiDate(h.date)}</small>`;
    button.addEventListener("click", () => { stopPlaying(); selectHorizon(h.h); });
    container.appendChild(button);
  });
}

function renderStatus() {
  const f = state.forecast;
  const h = f.horizons[state.horizon - 1];
  const issued = thaiDate(f.issued, true);
  document.getElementById("today-label").textContent =
    `ระดับน้ำ X.44 (หาดใหญ่ใน) ณ ${issued}${f.mode === "live" ? " (ข้อมูลล่าสุด)" : ""}`;
  document.getElementById("today-stage").textContent = bankText(f.today.stage);
  setValue(document.getElementById("today-stage"), f.today.alert);
  document.getElementById("today-detail").textContent = `ระดับน้ำ ${f.today.stage.toFixed(2)} ม.รทก. · ตลิ่ง ${bankLevel()} ม.รทก.`;
  setBadge(document.getElementById("today-alert"), f.today.alert);
  document.getElementById("selected-label").textContent = `คาดการณ์ ${thaiDate(h.date, true)} (+${h.h} วัน)`;
  const selected = document.getElementById("selected-stage");
  selected.textContent = `${bankText(h.stage)} `;
  const spread = document.createElement("span");
  spread.className = "spread";
  spread.textContent = `± ${h.sigma.toFixed(1)} ม.`;
  selected.appendChild(spread);
  setValue(selected, h.alert);
  const observed = h.observed !== null ? ` · วัดได้จริง ${bankText(h.observed)}` : "";
  const previous = state.horizon > 1 ? f.horizons[state.horizon - 2].rain_forecast_total_mm : 0;
  const rain = h.rain_forecast_total_mm !== null ? ` · ฝนพยากรณ์วันนั้น ~${Math.round(h.rain_forecast_total_mm - previous)} มม.` : "";
  document.getElementById("selected-detail").textContent =
    `ระดับน้ำ ${h.stage.toFixed(2)} ม.รทก. · พื้นที่คาดว่าท่วม ~${h.flooded_km2} ตร.กม. · ${h.zones_at_risk} โซนเสี่ยงสูง${rain}${observed}`;
  setBadge(document.getElementById("selected-alert"), h.alert);
  document.getElementById("model-line").textContent = `โมเดล: ${f.model_label}`;
}

function renderModeHint() {
  const f = state.forecast;
  const hint = document.getElementById("mode-hint");
  if (f.mode === "live") {
    const time = (iso) => iso.slice(11, 16);
    hint.textContent = `ข้อมูลสด ดึงเมื่อ ${thaiDate(f.live.fetched_at.slice(0, 10))} ${time(f.live.fetched_at)} น. · ` +
      `X.44 อ่านล่าสุด ${time(f.live.x44_last_reading)} น. · ข้อมูลวันนี้ยังไม่ครบวัน`;
  } else {
    hint.textContent = "โหมดย้อนดู: ระบบพยากรณ์จากข้อมูลจริงที่มี ณ สิ้นวันที่เลือก";
  }
  document.getElementById("live-button").classList.toggle("active", f.mode === "live");
}

const CHART_SIZES = {
  small: { W: 360, H: 190, left: 30, right: 10, top: 10, bottom: 24, tick: 2, r: 3, rSelected: 5, stroke: 2, details: false },
  large: { W: 1000, H: 520, left: 52, right: 28, top: 30, bottom: 44, tick: 1, r: 5, rSelected: 8, stroke: 3, details: true, allDates: true },
  // Narrow screens: fewer, bigger labels
  compact: { W: 520, H: 440, left: 44, right: 16, top: 30, bottom: 44, tick: 2, r: 5, rSelected: 8, stroke: 3, details: true, allDates: false },
};

function renderChart() {
  drawChart(document.getElementById("chart"), CHART_SIZES.small);
  if (!document.getElementById("chart-modal").hidden) {
    const large = document.getElementById("chart-large");
    const size = window.innerWidth < 700 ? CHART_SIZES.compact : CHART_SIZES.large;
    large.setAttribute("viewBox", `0 0 ${size.W} ${size.H}`);
    drawChart(large, size);
    const f = state.forecast;
    const h = f.horizons[state.horizon - 1];
    document.getElementById("chart-modal-sub").textContent =
      `ออกพยากรณ์ ณ สิ้นวันที่ ${thaiDate(f.issued, true)} · เลือก +${h.h} วัน (${thaiDate(h.date, true)}): ${bankText(h.stage)} ± ${h.sigma.toFixed(1)} ม.` +
      " · คลิกจุดพยากรณ์เพื่อเลือกวัน";
    renderForecastTable();
  }
}

// Water level chart; `o` sets the size, and `o.details` adds every date and the value of each point
function drawChart(svg, o) {
  const f = state.forecast;
  const { W, H, left, right, top, bottom } = o;
  // Everything on this chart is distance to the X.44 bank (0 = bank, positive = overflowing)
  const bank = bankLevel();
  const rel = (v) => (v === null || v === undefined ? null : v - bank);
  const today = rel(f.today.stage);
  const past = f.history.map((d) => ({ date: d.date, stage: rel(d.stage) }));
  const horizons = f.horizons.map((h) => ({ h: h.h, date: h.date, stage: rel(h.stage), sigma: h.sigma, observed: rel(h.observed), alert: h.alert }));
  const points = [...past, ...horizons];
  const flood = f.alert_levels["ท่วมพื้นที่ลุ่มต่ำ"] - bank;
  const values = points.flatMap((p) => [p.stage, p.observed, p.sigma ? p.stage + p.sigma : null]).filter((v) => v !== null && v !== undefined);
  const yMax = Math.max(1, ...values) + 0.5;
  const yMin = Math.floor(Math.min(...values) - 0.5);
  const x = (i) => left + (i / (points.length - 1)) * (W - left - right);
  const y = (v) => top + (1 - (v - yMin) / (yMax - yMin)) * (H - top - bottom);
  const fmt = (v) => (v > 0 ? `+${v.toFixed(2)}` : v.toFixed(2));

  let html = "";
  // Background zones: green well below the bank, yellow near it, red above it
  const near = -f.near_bank_m;
  const zone = (from, to, color) => {
    const y0 = y(Math.min(Math.max(to, yMin), yMax)), y1 = y(Math.min(Math.max(from, yMin), yMax));
    if (y1 > y0) html += `<rect x="${left}" y="${y0}" width="${W - right - left}" height="${y1 - y0}" fill="${color}"/>`;
  };
  zone(yMin, near, "rgba(46,139,87,0.10)");
  zone(near, 0, "rgba(224,176,0,0.18)");
  zone(0, yMax, "rgba(211,47,47,0.10)");
  for (let v = yMin + (((yMin % o.tick) + o.tick) % o.tick); v <= yMax; v += o.tick) {
    html += `<line x1="${left}" x2="${W - right}" y1="${y(v)}" y2="${y(v)}" stroke="#e3e8ee"/>`;
    html += `<text x="${left - 6}" y="${y(v) + 4}" text-anchor="end">${signed(v)}</text>`;
  }
  html += `<line x1="${left}" x2="${W - right}" y1="${y(0)}" y2="${y(0)}" stroke="${ALERT_COLORS.bank}" stroke-dasharray="4 4"/>`;
  html += `<line x1="${left}" x2="${W - right}" y1="${y(flood)}" y2="${y(flood)}" stroke="${ALERT_COLORS.flood}" stroke-dasharray="4 4"/>`;
  html += `<text x="${left + 6}" y="${y(flood) - 6}" text-anchor="start" style="fill:${ALERT_COLORS.bank}">ตลิ่ง 0 · ท่วมพื้นที่ลุ่มต่ำ +${flood.toFixed(2)} ม.</text>`;
  if (o.details) {
    html += `<text x="${left - 6}" y="${top - 12}" text-anchor="end">ม.</text>`;
  }

  const firstForecast = f.history.length;
  const todayIndex = firstForecast - 1;
  // Uncertainty band from the issue day through the forecast days
  const band = [
    [x(todayIndex), y(today)],
    ...horizons.map((h, i) => [x(firstForecast + i), y(Math.min(yMax, h.stage + h.sigma))]),
    ...horizons.map((h, i) => [x(firstForecast + i), y(Math.max(yMin, h.stage - h.sigma))]).reverse(),
  ];
  html += `<polygon points="${band.map((p) => p.join(",")).join(" ")}" fill="rgba(31,111,178,0.15)"/>`;

  const historyPath = past.map((d, i) => (d.stage === null ? null : `${x(i)},${y(d.stage)}`)).filter(Boolean);
  html += `<polyline points="${historyPath.join(" ")}" fill="none" stroke="#1d2733" stroke-width="${o.stroke}"/>`;
  const forecastPath = [[x(todayIndex), y(today)], ...horizons.map((h, i) => [x(firstForecast + i), y(h.stage)])];
  html += `<polyline points="${forecastPath.map((p) => p.join(",")).join(" ")}" fill="none" stroke="#1f6fb2" stroke-width="${o.stroke}" stroke-dasharray="${o.stroke * 2.5} ${o.stroke * 1.5}"/>`;

  if (o.allDates) {
    past.forEach((d, i) => {
      if (d.stage === null) return;
      html += `<circle cx="${x(i)}" cy="${y(d.stage)}" r="3" fill="#1d2733"/>`;
      html += `<text class="val" x="${x(i)}" y="${y(d.stage) - 10}" text-anchor="middle">${fmt(d.stage)}</text>`;
    });
  }
  horizons.forEach((h, i) => {
    const cx = x(firstForecast + i);
    const selected = h.h === state.horizon;
    if (h.observed !== null) {
      html += `<circle cx="${cx}" cy="${y(h.observed)}" r="${o.r}" fill="#fff" stroke="#1d2733" stroke-width="1.5"/>`;
    }
    const color = o.details ? ALERT_COLORS[h.alert.code] : "#1f6fb2";
    html += `<circle data-h="${h.h}" cx="${cx}" cy="${y(h.stage)}" r="${selected ? o.rSelected : o.r}" fill="${color}" stroke="${selected ? "#1d2733" : "#fff"}" stroke-width="${selected ? 2 : 1.5}" style="cursor:pointer"/>`;
    if (o.details) {
      html += `<text class="val forecast" x="${cx}" y="${y(h.stage) + 26}" text-anchor="middle" style="fill:${color}">${fmt(h.stage)}</text>`;
    }
  });
  html += `<line x1="${x(todayIndex)}" x2="${x(todayIndex)}" y1="${top}" y2="${H - bottom}" stroke="#9aa5b1" stroke-dasharray="3 3"/>`;
  if (o.details) {
    points.forEach((p, i) => {
      if (!o.allDates && i < todayIndex) return;
      const bold = i === todayIndex ? ' style="font-weight:600"' : "";
      if (!o.allDates && i > todayIndex) {
        html += `<text x="${x(i)}" y="${H - 22}" text-anchor="middle">+${i - todayIndex}</text>`;
        return;
      }
      html += `<text x="${x(i)}" y="${H - 22}" text-anchor="middle"${bold}>${thaiDate(p.date)}</text>`;
      const second = i === todayIndex ? "ออกพยากรณ์" : i >= firstForecast ? `+${i - todayIndex} วัน` : "";
      if (second) html += `<text class="sub" x="${x(i)}" y="${H - 5}" text-anchor="middle"${bold}>${second}</text>`;
    });
  } else {
    html += `<text x="${x(todayIndex)}" y="${H - 8}" text-anchor="middle">${thaiDate(f.issued)}</text>`;
    [0, todayIndex - 5, points.length - 1].forEach((i) => {
      if (i > 0 && i !== todayIndex) html += `<text x="${x(i)}" y="${H - 8}" text-anchor="middle">${thaiDate(points[i].date)}</text>`;
    });
    html += `<text x="${x(0)}" y="${H - 8}" text-anchor="start">${thaiDate(points[0].date)}</text>`;
  }
  svg.innerHTML = html;
}

// Per-day numbers under the large chart (the chart itself only labels the forecast value)
function renderForecastTable() {
  const f = state.forecast;
  const cell = (text, attrs = "") => `<td${attrs}>${text}</td>`;
  const rows = f.horizons.map((h) => {
    const selected = h.h === state.horizon ? ' class="selected"' : "";
    const observed = h.observed !== null ? bankText(h.observed) : "–";
    return `<tr${selected} data-h="${h.h}">` +
      cell(`+${h.h} วัน · ${thaiDate(h.date, true)}`) +
      cell(bankText(h.stage), ` style="color:${ALERT_COLORS[h.alert.code]};font-weight:700"`) +
      cell(`± ${h.sigma.toFixed(1)} ม.`) +
      cell(observed) +
      cell(`<span class="badge ${h.alert.code}">${h.alert.label}</span>`) +
      cell(`${h.zones_at_risk} โซน · ~${h.flooded_km2} ตร.กม.`) +
      "</tr>";
  });
  document.getElementById("chart-modal-table").innerHTML =
    "<thead><tr><th>วันที่พยากรณ์</th><th>ทำนาย (เทียบตลิ่ง)</th><th>ความคลาดเคลื่อน</th><th>วัดได้จริง</th><th>ระดับเตือน</th><th>โซนเสี่ยงสูง · พื้นที่คาดว่าท่วม</th></tr></thead>" +
    `<tbody>${rows.join("")}</tbody>`;
}

function openChartModal() {
  if (!state.forecast) return;
  document.getElementById("chart-modal").hidden = false;
  renderChart();
  document.getElementById("chart-modal-close").focus();
}

function closeChartModal() {
  document.getElementById("chart-modal").hidden = true;
  document.getElementById("chart-card").focus();
}

function selectHorizon(h) {
  state.horizon = h;
  state.zonesLayer.setStyle(zoneStyle);
  renderDayButtons();
  renderStatus();
  renderChart();
}

function applyForecast(forecast) {
  state.forecast = forecast;
  document.getElementById("issue-date").value = forecast.issued;
  history.replaceState(null, "", `?date=${forecast.mode === "live" ? "live" : forecast.issued}`);
  renderModeHint();
  selectHorizon(state.horizon);
}

async function loadForecast(date) {
  if (date === "live") return loadLive();
  const card = document.getElementById("status-card");
  card.classList.add("loading");
  try {
    applyForecast(await loadJSON(`/api/forecast?date=${date}`));
  } catch (error) {
    alert(`โหลดพยากรณ์ไม่สำเร็จ: ${error.message}`);
  } finally {
    card.classList.remove("loading");
  }
}

// ---------------------------------------------------------------- live data popup
// The server streams one event per step (fetch each station, rain, build input, GFS, model),
// so the bar shows real progress. Steps take very different times (one station can take 15 s).
const LIVE_GROUPS = {
  waterlevel: "ระดับน้ำ 6 สถานี (ThaiWater)",
  rain: "ฝนที่ตกแล้ว (ERA5 + ECMWF analysis)",
  table: "สร้าง input ย้อนหลัง 30 วัน",
  forecast: "พยากรณ์ฝน GFS ล่าสุด",
  model: "LSTM พยากรณ์ + แผนที่รายโซน",
};
// A cached forecast comes back at once; only show the popup if loading takes longer than this
const LIVE_POPUP_DELAY_MS = 250;
const live = { source: null, steps: [], timer: null, popupTimer: null, started: 0 };

function setLiveProgress(fraction) {
  const percent = Math.round(fraction * 100);
  document.getElementById("live-progress-fill").style.width = `${percent}%`;
  document.getElementById("live-progress").setAttribute("aria-valuenow", percent);
}

// `current` = index of the running step; steps before it are done
function renderLiveSteps(current) {
  const groups = [];
  live.steps.forEach((step, i) => {
    let group = groups.find((g) => g.key === step.group);
    if (!group) groups.push(group = { key: step.group, first: i, total: 0 });
    group.total += 1;
  });
  document.getElementById("live-steps").innerHTML = groups.map((g) => {
    const done = Math.min(Math.max(current - g.first, 0), g.total);
    const status = done === g.total ? "done" : current >= g.first ? "current" : "";
    const count = g.total > 1 ? `<span class="count">${done}/${g.total}</span>` : "";
    return `<li class="${status}">${LIVE_GROUPS[g.key] || g.key}${count}</li>`;
  }).join("");
  setLiveProgress(live.steps.length ? current / live.steps.length : 0);
  const step = live.steps[current];
  document.getElementById("live-step").textContent = step
    ? `ขั้นที่ ${current + 1}/${live.steps.length} · ${step.label}`
    : "เสร็จแล้ว";
}

function openLivePopup() {
  const box = document.querySelector("#live-modal .live-box");
  box.classList.remove("failed", "done");
  document.getElementById("live-error").hidden = true;
  document.getElementById("live-retry").hidden = true;
  document.getElementById("live-cancel").textContent = "ยกเลิก";
  document.getElementById("live-modal").hidden = false;
  document.getElementById("live-cancel").focus();
}

function closeLivePopup() {
  document.getElementById("live-modal").hidden = true;
}

function stopLive() {
  if (live.source) live.source.close();
  live.source = null;
  clearInterval(live.timer);
  clearTimeout(live.popupTimer);
  document.getElementById("live-button").disabled = false;
  document.getElementById("status-card").classList.remove("loading");
}

function failLive(message) {
  stopLive();
  openLivePopup();
  document.querySelector("#live-modal .live-box").classList.add("failed");
  document.getElementById("live-step").textContent = "ไม่สำเร็จ";
  const error = document.getElementById("live-error");
  error.textContent = message;
  error.hidden = false;
  document.getElementById("live-retry").hidden = false;
  document.getElementById("live-cancel").textContent = "ปิด";
}

function loadLive() {
  stopLive();
  live.steps = [];
  live.started = Date.now();
  document.getElementById("live-button").disabled = true;
  document.getElementById("status-card").classList.add("loading");
  document.getElementById("live-elapsed").textContent = "0 วินาที";
  // Jump back to empty instead of animating down from the previous run's full bar
  const fill = document.getElementById("live-progress-fill");
  fill.style.transition = "none";
  renderLiveSteps(0);
  void fill.offsetWidth;
  fill.style.transition = "";
  live.popupTimer = setTimeout(openLivePopup, LIVE_POPUP_DELAY_MS);
  live.timer = setInterval(() => {
    document.getElementById("live-elapsed").textContent = `${Math.floor((Date.now() - live.started) / 1000)} วินาที`;
  }, 500);

  const source = live.source = new EventSource("/api/forecast/live/stream");
  source.addEventListener("start", (event) => {
    live.steps = JSON.parse(event.data).steps;
    renderLiveSteps(0);
  });
  source.addEventListener("progress", (event) => renderLiveSteps(JSON.parse(event.data).step));
  source.addEventListener("result", (event) => {
    const forecast = JSON.parse(event.data);
    const popupShown = !document.getElementById("live-modal").hidden;
    stopLive();  // close before the server ends the stream, or EventSource reconnects
    renderLiveSteps(live.steps.length);
    document.querySelector("#live-modal .live-box").classList.add("done");
    applyForecast(forecast);
    // Leave the full bar on screen for a moment so the finish is visible
    if (popupShown) setTimeout(closeLivePopup, 600);
  });
  source.addEventListener("failed", (event) => failLive(JSON.parse(event.data).message));
  // Connection errors (server down, stream cut); EventSource would otherwise keep retrying
  source.addEventListener("error", () => {
    if (live.source === source) failLive("เชื่อมต่อเซิร์ฟเวอร์ไม่ได้ ลองใหม่อีกครั้ง");
  });
}

document.getElementById("live-cancel").addEventListener("click", () => {
  // The server keeps going and caches the result, so trying again soon is quick
  stopLive();
  closeLivePopup();
  if (state.forecast) renderModeHint();
});
document.getElementById("live-retry").addEventListener("click", loadLive);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !document.getElementById("live-modal").hidden) {
    document.getElementById("live-cancel").click();
  }
});

function stopPlaying() {
  clearInterval(state.playing);
  state.playing = null;
  document.getElementById("play-button").textContent = "▶";
}

document.getElementById("play-button").addEventListener("click", () => {
  if (state.playing) return stopPlaying();
  document.getElementById("play-button").textContent = "❚❚";
  selectHorizon(1);
  state.playing = setInterval(() => selectHorizon((state.horizon % 5) + 1), 1300);
});

const chartCard = document.getElementById("chart-card");
chartCard.addEventListener("click", openChartModal);
chartCard.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openChartModal(); }
});
document.getElementById("chart-modal-close").addEventListener("click", closeChartModal);
document.getElementById("chart-modal").addEventListener("click", (event) => {
  if (event.target.id === "chart-modal") closeChartModal();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !document.getElementById("chart-modal").hidden) closeChartModal();
});
document.getElementById("chart-modal-table").addEventListener("click", (event) => {
  const row = event.target.closest("[data-h]");
  if (row) { stopPlaying(); selectHorizon(Number(row.dataset.h)); }
});
document.getElementById("chart-large").addEventListener("click", (event) => {
  const point = event.target.closest("[data-h]");
  if (point) { stopPlaying(); selectHorizon(Number(point.dataset.h)); }
});

document.getElementById("issue-date").addEventListener("change", (event) => loadForecast(event.target.value));
document.getElementById("demo-button").addEventListener("click", (event) => loadForecast(event.target.dataset.date));
document.getElementById("live-button").addEventListener("click", () => loadForecast("live"));

(async function init() {
  state.dates = await loadJSON("/api/dates");
  const input = document.getElementById("issue-date");
  input.min = state.dates.first;
  input.max = state.dates.last;
  await Promise.all([loadZones(), loadStations()]);
  const requested = new URLSearchParams(location.search).get("date");
  await loadForecast(requested || document.getElementById("demo-button").dataset.date);
})();
