// BheedBus front end: plain JS, no build step.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const hh = (h) => String(h).padStart(2, "0") + ":00";
const debounce = (fn, ms = 200) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const KEYS = ["seats", "standing", "crowded", "packed"];
const CONF = ["low", "medium", "high"];
let LEVELS = [];
const LV = {};

// ---------------------------------------------------------------- theme, tabs, segments, tooltip
try { const t = localStorage.getItem("bb-theme"); if (t) document.documentElement.dataset.theme = t; } catch {}
$("#theme").onclick = () => {
  const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("bb-theme", next); } catch {}
};
const loaded = {};
function showTab(name) {
  $$("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("on", t.id === name));
  if (!loaded[name]) { loaded[name] = true; ({ routes: initRoutes, city: loadCity, ops: loadOps, about: loadAbout })[name]?.(); }
  if (name === "routes" && map) setTimeout(() => { map.invalidateSize(); drawMap(true); }, 60);
  if (name === "city" && cmap) setTimeout(() => { cmap.invalidateSize(); clayer?._reset(); }, 60);
  history.replaceState(null, "", "#" + name);
}
$$("#tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
addEventListener("hashchange", () => { const t = location.hash.slice(1); if ($("#" + t)?.classList.contains("tab")) showTab(t); });
function seg(id, cb) {
  $$(`#${id} button`).forEach((b) => (b.onclick = () => {
    $$(`#${id} button`).forEach((x) => x.classList.toggle("on", x === b)); cb?.(b.dataset.v);
  }));
  return () => $(`#${id} button.on`).dataset.v;
}
function setSeg(id, v) { $$(`#${id} button`).forEach((b) => b.classList.toggle("on", b.dataset.v === String(v))); }

const tip = $("#tip");
const moveTip = (e) => {
  tip.style.left = Math.min(innerWidth - tip.offsetWidth - 8, e.clientX + 12) + "px";
  tip.style.top = Math.max(8, e.clientY - tip.offsetHeight - 12) + "px";
};
document.addEventListener("mouseover", (e) => { const t = e.target.closest("[data-tip]"); if (t) { tip.innerHTML = t.dataset.tip; tip.hidden = false; moveTip(e); } });
document.addEventListener("mousemove", (e) => { if (!tip.hidden) moveTip(e); });
document.addEventListener("mouseout", (e) => { if (e.target.closest("[data-tip]")) tip.hidden = true; });

function crowdPill(key) {
  const l = LV[key];
  return `<span class="crowd" style="background:${l.color};color:${key === "standing" || key === "crowded" ? "#111" : "#fff"}">${l.icon} ${esc(l.label)}</span>`;
}

// ---------------------------------------------------------------- type-ahead dropdown
// combo(input, {fetch, render, value, onPick}): matches highlighted, ↑/↓ to move, Enter to pick, Esc to close.
function hl(text, q) {
  const t = esc(text); const words = (q || "").trim().split(/\s+/).filter(Boolean).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  return words.length ? t.replace(new RegExp(`(${words.join("|")})`, "gi"), "<mark>$1</mark>") : t;
}
function combo(input, cfg) {
  const box = input.closest(".combo");
  const menu = document.createElement("div"); menu.className = "menu"; menu.hidden = true; menu.setAttribute("role", "listbox");
  box.appendChild(menu);
  let rows = [], act = 0, seq = 0;
  const open = (v) => { menu.hidden = !v; input.setAttribute("aria-expanded", String(v)); };
  const paint = () => {
    const q = input.value.trim();
    menu.innerHTML = rows.length ? rows.map((r, i) => `<div class="opt-i ${i === act ? "act" : ""} ${cfg.value(r) === input.dataset.picked ? "cur" : ""}" role="option" aria-selected="${i === act}" data-i="${i}">${cfg.render(r, q)}<span class="tick">✓</span></div>`).join("")
      : `<div class="empty-i">${q.length < (cfg.min ?? 1) ? "Keep typing…" : "No match. Try a shorter name."}</div>`;
    menu.querySelector(".act")?.scrollIntoView({ block: "nearest" });
  };
  const pick = (i) => { const r = rows[i]; if (!r) return; input.value = cfg.value(r); input.dataset.picked = input.value; open(false); cfg.onPick?.(r); };
  const refresh = debounce(async () => {
    const q = input.value.trim(), my = ++seq;
    if (q.length < (cfg.min ?? 1)) { rows = []; paint(); open(true); return; }
    const got = await cfg.fetch(q).catch(() => []);
    if (my !== seq) return;
    rows = got; act = 0; paint(); open(true);
  }, 140);
  input.addEventListener("input", refresh);
  input.addEventListener("focus", () => { input.select(); refresh(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault(); if (menu.hidden) return refresh();
      act = (act + (e.key === "ArrowDown" ? 1 : -1) + rows.length) % Math.max(rows.length, 1); paint();
    } else if (e.key === "Enter") { if (!menu.hidden && rows.length) { e.preventDefault(); e.stopPropagation(); pick(act); } }
    else if (e.key === "Escape") open(false);
  });
  menu.addEventListener("mousedown", (e) => { const el = e.target.closest(".opt-i"); if (el) { e.preventDefault(); pick(+el.dataset.i); } });
  input.addEventListener("blur", () => setTimeout(() => open(false), 120));
  input.dataset.picked = input.value;
}

// ================================================================ TRIP
const getDay = seg("day", () => plan());
const getPref = seg("pref", () => plan());
$("#swap").onclick = () => { const a = $("#from").value; $("#from").value = $("#to").value; $("#to").value = a; plan(); };
for (const id of ["from", "to"]) {
  combo($("#" + id), {
    min: 2, fetch: (q) => api(`/api/places?q=${encodeURIComponent(q)}&limit=8`), value: (r) => r.name,
    render: (r, q) => `<span>📍</span><span>${hl(r.name, q)}<span class="sub">${r.routes} bus route${r.routes === 1 ? "" : "s"}</span></span>`,
    onPick: () => plan(),
  });
}
$("#planform").onsubmit = (e) => { e.preventDefault(); plan(); };

let lastPlan = null;
async function plan(p) {
  p = p || { from: $("#from").value, to: $("#to").value, time: $("#time").value, day: getDay(), comfort: getPref() };
  $("#results").innerHTML = `<div class="empty">Finding buses…</div>`;
  try { lastPlan = await api(`/api/plan?${new URLSearchParams(p)}`); renderPlan(lastPlan); }
  catch (err) { $("#results").innerHTML = `<div class="empty">${esc(err.message)}</div>`; }
}
function setPlanForm(p) {
  $("#from").value = p.origin; $("#to").value = p.destination; if (p.time) $("#time").value = p.time;
  setSeg("day", p.day || "weekday");
  setSeg("pref", +p.comfort >= 0.75 ? "1" : +p.comfort <= 0.25 ? "0" : "0.5");
}
function renderPlan(r) {
  if (r.error || !r.options.length) {
    $("#results").innerHTML = `<div class="empty">${esc(r.error || "No bus trip found. Try a nearby stop name.")}</div>`; return;
  }
  $("#results").innerHTML = r.options.map((o, i) => tripCard(o, i)).join("");
  $$(".trip").forEach((el) => ($(".trip-main", el).onclick = (e) => { if (!e.target.closest(".rt")) el.classList.toggle("open"); }));
  $$(".trip .rate button").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); rateRide(b); }));
  $$(".trip .rt").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); openRoute(b.dataset.route, b.dataset.from, b.dataset.to, +b.dataset.h); }));
}
function tripCard(o, i) {
  const worst = KEYS[o.worst_level];
  const tags = (o.badges || []).map((b) => `<span class="tag ${b === "Fastest" ? "fast" : b === "Most comfortable" ? "calm" : ""}">${esc(b)}</span>`).join("");
  const first = o.legs[0], last = o.legs.at(-1);
  const buses = o.legs.map((l) => `<button class="rt" type="button" title="See ${esc(l.route)} on the map" data-route="${esc(l.route)}" data-from="${esc(l.from)}" data-to="${esc(l.to)}" data-h="${Math.floor(l.board_min / 60) % 24}">${esc(l.route)}</button>`).join(`<span>→</span>`);
  let strip = "";
  for (const l of o.legs) {
    strip += `<i class="wait" style="flex:${l.wait_min}" data-tip="Wait ~${Math.round(l.wait_min)} min for ${esc(l.route)}"></i>`;
    for (const s of l.stretches) strip += `<i style="flex:${s.minutes};background:${LV[s.level].color}" data-tip="<b>${esc(l.route)}</b> ${esc(s.from)} → ${esc(s.to)}<br>${LV[s.level].icon} ${LV[s.level].label} · ~${Math.round(s.load)} on board"></i>`;
  }
  const legs = o.legs.map((l) => `<div class="leg"><div class="t">${esc(l.board)}</div><div>
      <div class="what">Board <b>${esc(l.route)}</b> at ${esc(l.from)}</div>
      <div class="sub">${l.stops} stops to ${esc(l.to)} · arrive ${esc(l.arrive)} · a bus every ~${Math.round(l.headway_min)} min · ${LV[l.worst_level].icon} ${LV[l.worst_level].label} at worst · 💺 ${l.seat_board_pct}% seat when you board</div>
      ${l.seat_tip ? `<div class="tip-seat">💡 Want a seat? Board at <b>${esc(l.seat_tip.stop)}</b>, ${l.seat_tip.stops_back} stop${l.seat_tip.stops_back > 1 ? "s" : ""} earlier (~${l.seat_tip.minutes_back} min back): <b>${l.seat_tip.seat_pct}%</b> seat chance instead of ${l.seat_board_pct}%.</div>`
        : (l.seat_board_pct < 50 && l.seat_after_min ? `<div class="tip-seat">🪑 A seat usually frees up after ~${l.seat_after_min} min.</div>` : "")}
      ${l.bunch?.tip ? `<div class="tip-seat" data-tip="Measured from pairs of ${esc(l.route)} buses ticketing at the same stop minutes apart (bunching ${l.bunch.g}; 0 = even, 1 = random)">🚌🚌 <b>${esc(l.route)} buses often come in pairs</b> at this hour. If the first is packed, there's ~${l.bunch.p_next_within}% chance another comes within ${l.bunch.window_min} min.</div>` : ""}
    </div></div>`).join("");
  const rate = `<div class="rate">Took ${esc(first.route)}? How full was it? ${KEYS.map((k) => `<button type="button" data-r="${k}">${LV[k].icon} ${esc(LV[k].label)}</button>`).join("")}</div>`;
  return `<article class="trip" data-i="${i}">
    <div class="trip-main">
      <div>${tags}
        <div class="dur tnum">${Math.round(o.duration_min)} min</div>
        <div class="times">${esc(first.board)} → ${esc(last.arrive)} · ${o.transfers ? o.transfers + " change" + (o.transfers > 1 ? "s" : "") : "direct"}</div>
        <div class="buses">${buses}</div>
      </div>
      <div class="right">${crowdPill(worst)}${o.legs.some((l) => l.bunch?.tip) ? `<span class="tag" data-tip="Buses on this route often arrive in pairs at this hour. Tap the card for details.">🚌🚌 pairs</span>` : ""}${o.legs.some((l) => l.seat_tip) ? `<span class="tag calm" data-tip="Board a few stops earlier for a much better chance of a seat. Tap the card for details.">💡 seat tip</span>` : ""}<span class="seat" data-tip="Chance a bus arrives with a free seat when you board">💺 <b>${o.seat_pct}%</b> seat</span></div>
    </div>
    <div class="strip" role="img" aria-label="Crowding along the trip">${strip}</div>
    <div class="trip-more">${legs}<div class="sub muted">Estimate confidence: ${o.confidence}. Click a bus number to see its route on the map.</div>${rate}</div>
  </article>`;
}

// ================================================================ ROUTES
const getRday = seg("rday", () => loadRoute());
let prof = null, pendingLeg = null, legFocus = null;
$("#rhour").oninput = (e) => { $("#rhourout").textContent = hh(e.target.value); drawLine(); drawMap(false); };
const lvKey = (load) => (load <= 34 ? "seats" : load <= 70 ? "standing" : load <= 100 ? "crowded" : "packed");
combo($("#rq"), {
  fetch: (q) => api(`/api/routes?q=${encodeURIComponent(q.replace(/\s+/g, ""))}&limit=12`), value: (r) => r.route,
  render: (r, q) => `<span class="rt">${hl(r.route, q.toUpperCase())}</span><span>${esc(r.from)} → ${esc(r.to)}<span class="sub">${r.stops} stops · a bus every ~${Math.round(60 / Math.max(r.dep_per_hour, 0.1))} min</span></span><span>${LV[lvKey(r.peak_load)]?.icon || ""}</span>`,
  onPick: () => loadRoute(),
});
$("#rq").addEventListener("keydown", (e) => e.key === "Enter" && loadRoute());

async function initRoutes() {
  const hot = await api(`/api/operator/hotspots?day=weekday&limit=8`).catch(() => []);
  $("#rchips").insertAdjacentHTML("beforeend", hot.map((r) => `<button data-r="${esc(r.route)}" data-h="${r.hour}">${esc(r.route)}</button>`).join(""));
  $$("#rchips button").forEach((b) => (b.onclick = () => { $("#rq").value = b.dataset.r; setHour(b.dataset.h); loadRoute(); }));
  $("#maplegend").innerHTML = LEVELS.map((l) => `<span><i class="sw" style="background:${l.color}"></i>${l.label}</span>`).join("") + `<span>● exact position</span><span>○ approximate</span>`;
  loadRoute();
}
function setHour(h) { $("#rhour").value = Math.min(23, Math.max(5, h)); $("#rhourout").textContent = hh($("#rhour").value); }
async function openRoute(route, from, to, hour) {
  $("#rq").value = route; $("#rq").dataset.picked = route; setHour(hour); pendingLeg = { from, to };
  if (!loaded.routes) { loaded.routes = true; showTab("routes"); await initRoutes(); } else { showTab("routes"); await loadRoute(); }
  scrollTo({ top: 0, behavior: "smooth" });
}
async function resolveRoute(q) {
  const rows = await api(`/api/routes?q=${encodeURIComponent(q)}&limit=40`);
  if (rows.some((r) => r.route === q)) return q;
  const same = rows.filter((r) => r.base === q);
  return (same.find((r) => r.dir === "UP") || same[0] || rows[0])?.route;
}
async function loadRoute() {
  const q = $("#rq").value.trim().toUpperCase().replace(/\s+/g, "");
  if (!q) return;
  const rn = await resolveRoute(q);
  if (!rn) { $("#rhead").innerHTML = `<span>No route matches “${esc(q)}”. Try 433, 717A or 522.</span>`; return; }
  $("#rq").value = rn; $("#rq").dataset.picked = rn;
  prof = await api(`/api/route/${encodeURIComponent(rn)}?day=${getRday()}`);
  legFocus = null;
  if (pendingLeg) { const a = prof.stops.indexOf(pendingLeg.from), b = prof.stops.indexOf(pendingLeg.to); if (a >= 0 && b > a) legFocus = [a, b]; pendingLeg = null; }
  const others = (await api(`/api/routes?q=${encodeURIComponent(prof.base)}&limit=40`)).filter((r) => r.base === prof.base && r.route !== rn);
  $("#rhead").innerHTML = `<span class="name">${esc(rn)}</span><span>${esc(prof.stops[0])} → ${esc(prof.stops.at(-1))}</span>
    <span>${prof.stops.length} stops · a bus every ~${Math.round(prof.headway_min)} min</span>
    ${others.length ? `<span class="alt">Also: ${others.map((r) => `<button class="ghost" data-r="${esc(r.route)}">${esc(r.route)}</button>`).join("")}</span>` : ""}`;
  $$("#rhead .alt button").forEach((b) => (b.onclick = () => { $("#rq").value = b.dataset.r; loadRoute(); }));
  $$("#rchips button").forEach((b) => b.classList.toggle("on", b.dataset.r === rn));
  drawLine(); drawHeat(); loadMap(rn);
}
function drawLine() {
  if (!prof) return;
  const h = +$("#rhour").value;
  $("#line").innerHTML = prof.stops.map((s, i) => {
    const last = i === prof.stops.length - 1, lv = last ? null : KEYS[prof.level[h][i]];
    const inLeg = legFocus && i >= legFocus[0] && i <= legFocus[1];
    return `<div class="stoprow ${inLeg ? "leg" : ""}" data-i="${i}">
      ${last ? "" : `<span class="seg-line" style="background:${LV[lv].color};opacity:${prof.confidence[h][i] ? 1 : .45}"></span>`}
      <span class="dotx"></span><span class="nm">${esc(s)}</span>
      ${last ? "" : `<span class="ld" data-tip="${LV[lv].label} on the next stretch · ${prof.seat_pct ? prof.seat_pct[h][i] + "% chance of a seat · " : ""}confidence ${CONF[prof.confidence[h][i]]}">~${Math.round(prof.load[h][i])} 👤</span>`}</div>`;
  }).join("");
  $$("#line .stoprow").forEach((r) => (r.onclick = () => focusStop(+r.dataset.i)));
}
function drawHeat() {
  const n = prof.stops.length - 1, hours = [...Array(19)].map((_, i) => i + 5);
  const cw = 22, ch = 13, left = 200, top = 20, W = left + hours.length * cw + 4, H = top + n * ch + 4;
  let o = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Crowding by stretch and hour">`;
  hours.forEach((h, j) => { if (h % 3 === 0) o += `<text x="${left + j * cw + cw / 2}" y="13" text-anchor="middle" font-size="10" fill="currentColor">${String(h).padStart(2, "0")}</text>`; });
  for (let i = 0; i < n; i++) {
    const nm = prof.stops[i].length > 30 ? prof.stops[i].slice(0, 29) + "…" : prof.stops[i];
    o += `<text x="${left - 6}" y="${top + i * ch + ch - 3}" text-anchor="end" font-size="10" fill="currentColor">${esc(nm)}</text>`;
    hours.forEach((h, j) => {
      const lv = KEYS[prof.level[h][i]], c = prof.confidence[h][i];
      o += `<rect x="${left + j * cw + 1}" y="${top + i * ch + 1}" width="${cw - 2}" height="${ch - 2}" rx="2" fill="${LV[lv].color}" opacity="${[0.35, 0.75, 1][c]}" data-h="${h}" data-tip="${esc(prof.stops[i])} → ${esc(prof.stops[i + 1])}<br>${hh(h)} · ${LV[lv].label} · ~${Math.round(prof.load[h][i])} on board"/>`;
    });
  }
  $("#heat").innerHTML = o + "</svg>";
  $$("#heat rect").forEach((r) => (r.onclick = () => { setHour(r.dataset.h); drawLine(); drawMap(false); }));
}

// ---- map (Leaflet + OpenStreetMap tiles, stop positions from OpenStreetMap)
let map = null, mapLayer = null, mapPts = null, mapRoute = null, stopMarkers = [], arrowLayer = null;
function ensureMap() {
  if (map || !window.L) return map;
  map = L.map("map", { scrollWheelZoom: false, zoomSnap: 0.25, zoomDelta: 0.5 }).setView([28.61, 77.21], 11);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' }).addTo(map);
  map.on("zoomend", drawArrows);
  map.on("click", () => map.scrollWheelZoom.enable());
  map.on("mouseout", () => map.scrollWheelZoom.disable());
  return map;
}
async function loadMap(rn) {
  if (!ensureMap()) { $("#mapstat").textContent = "Map could not load (offline?)"; return; }
  if (mapRoute !== rn) { mapPts = null; mapRoute = rn; if (mapLayer) map.removeLayer(mapLayer); if (arrowLayer) { map.removeLayer(arrowLayer); arrowLayer = null; } }
  $("#mapstat").textContent = "Placing stops on the map…";
  try {
    let r = await api(`/api/route/${encodeURIComponent(rn)}/map`);
    if (mapRoute !== rn) return;
    mapPts = r; drawMap(true);
    const status = (x) => `${x.exact} of ${x.total} stops at exact positions`;
    $("#mapstat").textContent = status(r) + (r.exact < r.total && !r.refined ? " · finding the rest…" : "");
    if (!r.refined && r.exact < r.total) {
      r = await api(`/api/route/${encodeURIComponent(rn)}/map?refine=true`);
      if (mapRoute !== rn) return;
      mapPts = r; drawMap(false); $("#mapstat").textContent = status(r);
    }
  } catch (err) { $("#mapstat").textContent = "Map: " + err.message; }
}
function drawMap(fit) {
  if (!map || !mapPts || !prof || prof.route !== mapPts.route) return;
  const h = +$("#rhour").value, pts = mapPts.stops;
  if (mapLayer) map.removeLayer(mapLayer);
  mapLayer = L.layerGroup().addTo(map); stopMarkers = [];
  const ll = pts.filter((p) => p.lat != null).map((p) => [p.lat, p.lon]);
  if (ll.length < 2) { $("#mapstat").textContent = "Not enough stops could be placed for this route."; return; }
  L.polyline(ll, { color: "#111", weight: 11, opacity: 0.18, lineJoin: "round" }).addTo(mapLayer); // casing so the line reads on any background
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i], b = pts[i + 1]; if (a.lat == null || b.lat == null) continue;
    const lv = KEYS[prof.level[h][i]], inLeg = legFocus && i >= legFocus[0] && i < legFocus[1];
    L.polyline([[a.lat, a.lon], [b.lat, b.lon]], { color: LV[lv].color, weight: inLeg ? 9 : 6, opacity: legFocus && !inLeg ? 0.35 : 1, lineCap: "round" })
      .bindTooltip(`<b>${esc(a.stop)} → ${esc(b.stop)}</b><br>${LV[lv].label} · ~${Math.round(prof.load[h][i])} on board${prof.seat_pct ? ` · 💺 ${prof.seat_pct[h][i]}%` : ""}`, { className: "stoptip", sticky: true }).addTo(mapLayer);
  }
  pts.forEach((p, i) => {
    if (p.lat == null) { stopMarkers.push(null); return; }
    const end = i === 0 || i === pts.length - 1, inLeg = legFocus && i >= legFocus[0] && i <= legFocus[1];
    const mk = L.circleMarker([p.lat, p.lon], { radius: end ? 7 : 4.5, color: "#111", weight: 2, dashArray: p.approx ? "2 2" : null,
      fillColor: end ? "#111" : "#fff", fillOpacity: p.approx ? 0.55 : 1, opacity: legFocus && !inLeg ? 0.4 : 1 })
      .bindTooltip(`<b>${i + 1}. ${esc(p.stop)}</b>${p.approx ? `<br><span style="opacity:.75">approximate position</span>` : ""}`, { className: "stoptip" })
      .on("click", () => focusStop(i, false)).addTo(mapLayer);
    if (end) L.tooltip({ permanent: true, direction: i === 0 ? "left" : "right", className: "endlabel", offset: [i === 0 ? -8 : 8, 0] })
      .setLatLng([p.lat, p.lon]).setContent(`${i === 0 ? "Start" : "End"}: ${esc(p.stop)}`).addTo(mapLayer);
    stopMarkers.push(mk);
  });
  drawArrows();
  if (fit) {
    const sel = legFocus ? pts.slice(legFocus[0], legFocus[1] + 1).filter((p) => p.lat != null).map((p) => [p.lat, p.lon]) : ll;
    const bounds = L.latLngBounds(sel.length > 1 ? sel : ll);
    // fit now, and again once the layout has settled (the map may have been measured mid-layout)
    map.invalidateSize(); map.fitBounds(bounds, { padding: [36, 36] });
    setTimeout(() => { map.invalidateSize(); map.fitBounds(bounds, { padding: [36, 36] }); }, 250);
  }
}
// Direction arrows along the route, about every 110 px on screen (re-spaced on every zoom).
function drawArrows() {
  if (arrowLayer) { map.removeLayer(arrowLayer); arrowLayer = null; }
  if (!map || !mapPts || !mapLayer) return;
  arrowLayer = L.layerGroup().addTo(map);
  const pts = mapPts.stops, z = map.getZoom(), GAP = 110;
  let carry = GAP / 2;  // distance (px) to walk before the next arrow
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i], b = pts[i + 1]; if (a.lat == null || b.lat == null) continue;
    const pa = map.project([a.lat, a.lon], z), pb = map.project([b.lat, b.lon], z);
    const dx = pb.x - pa.x, dy = pb.y - pa.y, len = Math.hypot(dx, dy);
    if (len < 1) continue;
    const deg = Math.atan2(dy, dx) * 180 / Math.PI, faint = legFocus && !(i >= legFocus[0] && i < legFocus[1]);
    let d = carry;
    for (; d < len; d += GAP) {
      const ll = map.unproject(L.point(pa.x + dx * d / len, pa.y + dy * d / len), z);
      L.marker(ll, { interactive: false, keyboard: false, icon: L.divIcon({ className: "rarrow" + (faint ? " faint" : ""), iconSize: [20, 20],
        html: `<svg viewBox="0 0 14 14" style="transform:rotate(${deg}deg)"><path d="M3 2.5 L11 7 L3 11.5 L5.5 7 Z"/></svg>` }) }).addTo(arrowLayer);
    }
    carry = d - len;  // what's left of the gap carries into the next stretch
  }
}
function focusStop(i, pan = true) {
  $$("#line .stoprow").forEach((r, k) => r.classList.toggle("focus", k === i));
  $$("#line .stoprow")[i]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  const mk = stopMarkers[i]; if (mk) { if (pan) map.panTo(mk.getLatLng()); mk.openTooltip(); }
}

// ================================================================ OPERATORS
const getOday = seg("oday", () => { loadHot(); loadRebal(); });
for (let h = 5; h < 23; h++) $("#ohour").insertAdjacentHTML("beforeend", `<option value="${h}">${hh(h)}</option>`);
$("#ohour").onchange = () => loadHot();
let hotRows = [];
async function loadOps() {
  const s = await api("/api/operator/summary"), inf = s.info, hold = s.holdout;
  $("#kpis").innerHTML = [
    [(inf.tickets / 1e6).toFixed(2) + "M", "tickets analysed (Oct 2026)"],
    [s.routes.toLocaleString(), "route-directions modelled"],
    [hold.pearson_r_route_hour_peak_flow.toFixed(2), "week-4 check (correlation)"],
    [Math.round(hold.level_agreement_well_sampled * 100) + "%", "same crowding level next week"],
  ].map(([v, l]) => `<div class="kpi"><div class="v tnum">${v}</div><div class="l">${l}</div></div>`).join("");
  lineChart($("#chart-hourly"), [{ name: "Weekday", color: "var(--s1)", values: s.hourly_profile.weekday }, { name: "Weekend", color: "var(--s2)", values: s.hourly_profile.weekend }]);
  barChart($("#chart-crowded"), s.crowded_share_by_hour.slice(5, 23).map((v, i) => ({ x: i + 5, v })));
  $("#valid").innerHTML = `
    <div><b>${hold.pearson_r_route_hour_peak_flow.toFixed(2)}</b>Fit on weeks 1–3, tested on week 4 (Spearman ${hold.spearman_r_route_hour_peak_flow.toFixed(2)}).</div>
    <div><b>${Math.round(hold.level_agreement_well_sampled * 100)}%</b>of well-sampled route-hours get the same crowding level next week.</div>
    <div><b>${(s.daytype_totals.weekday / s.daytype_totals.weekend).toFixed(2)}×</b>weekday vs weekend riders; peaks 08–10 and 17–20.</div>
    <div><b>${(inf.implied_weekday_boardings / 1e6).toFixed(1)}M</b>weekday boardings after calibrating to bus capacity.</div>`;
  const lbl = { low: "Quiet (few tickets) · week 4", medium: "Medium · week 4", high: "Busy · week 4", all: "All · week 4",
    final_low: "Quiet · final check 29–31 Oct", final_all: "All · final check 29–31 Oct" };
  $("#mltable tbody").innerHTML = (s.ml || []).map((r) => `<tr><td>${lbl[r.bucket] || r.bucket}</td><td class="num">${r.cells.toLocaleString()}</td>
    <td class="num">${r.dev_current.toFixed(3)}</td><td class="num">${r.dev_blend.toFixed(3)}</td>
    <td class="num"><b style="color:${r.improvement_pct > 0.5 ? "var(--good)" : "var(--ink-2)"}">${r.improvement_pct > 0.05 ? "−" + r.improvement_pct.toFixed(0) + "%" : "same"}</b></td></tr>`).join("")
    || `<tr><td colspan="5" class="muted">ML smoothing not run.</td></tr>`;
  loadHot(); loadRebal();
}
async function loadHot() {
  const h = $("#ohour").value;
  hotRows = await api(`/api/operator/hotspots?day=${getOday()}${h ? "&hour=" + h : ""}&limit=15`);
  $("#hot tbody").innerHTML = hotRows.length ? hotRows.map((r) => `<tr class="click" data-r="${esc(r.route)}" data-h="${r.hour}">
    <td><span class="rt">${esc(r.route)}</span></td><td class="tnum">${hh(r.hour)}</td><td>${esc(r.from)} → ${esc(r.to)}</td>
    <td class="num">${Math.round(r.peak_load)}</td><td>${crowdPill(r.level)}</td><td class="num"><b>+${r.extra_buses_per_hour}</b></td></tr>`).join("")
    : `<tr><td colspan="6" class="empty">No overloaded routes at this hour.</td></tr>`;
  $$("#hot tr.click").forEach((tr) => (tr.onclick = () => openRoute(tr.dataset.r, null, null, +tr.dataset.h)));
}
$("#csv").onclick = () => {
  const cols = Object.keys(hotRows[0] || {});
  const csv = [cols.join(","), ...hotRows.map((r) => cols.map((c) => JSON.stringify(r[c] ?? "")).join(","))].join("\n");
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); a.download = "bheedbus-hotspots.csv"; a.click();
};
$("#scale").oninput = (e) => ($("#scaleout").textContent = "×" + (+e.target.value).toFixed(1));
$("#scale").onchange = async (e) => { await api(`/api/operator/scale?scale=${e.target.value}`, { method: "POST" }); loadHot(); loadRebal(); };

// ---- fleet rebalancer (drag a bus from a spare route onto a crowded one)
let rb = { donors: [], receivers: [], moves: [], eval: null, sel: null };
const rbWin = () => $("#rbwin").value.split("-").map(Number);
$("#rbwin").onchange = () => loadRebal();
$("#rbreset").onclick = () => { rb.moves = []; evalRebal(); };
$("#rbopt").onclick = async () => {
  const [start, end] = rbWin(); $("#rbopt").textContent = "Optimising…";
  const r = await post("/api/operator/optimise", { day: getOday(), start, end, max_moves: 40 });
  $("#rbopt").textContent = "✨ Auto-optimise"; rb.moves = r.moves; rb.eval = r; renderRebal();
};
async function loadRebal() {
  const [start, end] = rbWin();
  const c = await api(`/api/operator/rebalance/candidates?day=${getOday()}&start=${start}&end=${end}&n=10`);
  rb = { donors: c.donors, receivers: c.receivers, moves: [], eval: { ...c, routes: [], buses_moved: 0 }, sel: null }; renderRebal();
}
async function evalRebal() { const [start, end] = rbWin(); rb.eval = await post("/api/operator/rebalance", { day: getOday(), start, end, moves: rb.moves }); renderRebal(); }
function addMove(from, to) {
  if (!from || !to || from === to) return;
  const m = rb.moves.find((x) => x.from === from && x.to === to); if (m) m.n += 1; else rb.moves.push({ from, to, n: 1 });
  rb.sel = null; evalRebal();
}
function renderRebal() {
  const e = rb.eval || {}, per = Object.fromEntries((e.routes || []).map((r) => [r.base, r]));
  const saved = (e.crowded_pax_min_before || 0) - (e.crowded_pax_min_after || 0);
  $("#rbkpi").innerHTML = [[String(e.buses_moved || 0), "buses moved"], [(e.change_pct || 0) + "%", "change in crowded travel"],
    [Math.round(saved / 60).toLocaleString(), "crowded passenger-hours saved / day"]]
    .map(([v, l]) => `<div class="kpi"><div class="v tnum">${v}</div><div class="l">${l}</div></div>`).join("");
  for (const m of rb.moves) {
    if (!rb.donors.some((d) => d.base === m.from)) rb.donors.push({ base: m.from, fleet: per[m.from]?.fleet_before ?? "?" });
    if (!rb.receivers.some((d) => d.base === m.to)) rb.receivers.push({ base: m.to, fleet: per[m.to]?.fleet_before ?? "?" });
  }
  const occ = (r) => {
    const p = per[r.base], v = p ? p.peak_load_after / 70 * 100 : r.peak_occupancy_pct;
    if (v == null) return "";
    const col = v > 143 ? "var(--critical)" : v > 100 ? "var(--serious)" : v > 49 ? "var(--warn)" : "var(--good)";
    return `<span class="occ" data-tip="Busiest point vs comfortable capacity${p ? `: ${Math.round(p.peak_load_before / 0.7)}% → ${Math.round(v)}%` : ""}"><span class="occbar"><i style="width:${Math.min(v, 150) / 1.5}%;background:${col}"></i></span>${Math.round(v)}%</span>`;
  };
  const d = (b) => { const x = per[b]?.delta_buses || 0; return x ? ` <span class="delta">${x > 0 ? "+" : ""}${x}</span>` : ""; };
  $("#rbdonors").innerHTML = rb.donors.map((r) => `<div class="rbrow donor ${rb.sel === r.base ? "sel" : ""}" draggable="true" data-b="${esc(r.base)}"><span class="bus">🚌</span><span><span class="rt">${esc(r.base)}</span> <span class="muted">${r.fleet} buses${d(r.base)}</span></span>${occ(r)}</div>`).join("");
  $("#rbrecv").innerHTML = rb.receivers.map((r) => `<div class="rbrow recv" data-b="${esc(r.base)}"><span>🎯</span><span><span class="rt">${esc(r.base)}</span> <span class="muted">${r.fleet} buses${d(r.base)}</span></span>${occ(r)}</div>`).join("");
  const byTo = {}; rb.moves.forEach((m) => { (byTo[m.to] ||= { n: 0, from: [] }); byTo[m.to].n += m.n; byTo[m.to].from.push(m.from); });
  $("#rbmoves").innerHTML = Object.entries(byTo).sort((a, b) => b[1].n - a[1].n).map(([to, g]) =>
    `<span class="mv" data-tip="from ${esc(g.from.join(", "))}"><b>+${g.n} 🚌 → ${esc(to)}</b><button data-to="${esc(to)}" aria-label="Undo">✕</button></span>`).join("");
  $$("#rbmoves button").forEach((b) => (b.onclick = () => { rb.moves = rb.moves.filter((m) => m.to !== b.dataset.to); evalRebal(); }));
  $$("#rbdonors .rbrow").forEach((el) => {
    el.addEventListener("dragstart", (ev) => ev.dataTransfer.setData("text/bus", el.dataset.b));
    el.onclick = () => { rb.sel = rb.sel === el.dataset.b ? null : el.dataset.b; renderRebal(); };
  });
  $$("#rbrecv .rbrow").forEach((el) => {
    el.addEventListener("dragover", (ev) => { ev.preventDefault(); el.classList.add("over"); });
    el.addEventListener("dragleave", () => el.classList.remove("over"));
    el.addEventListener("drop", (ev) => { ev.preventDefault(); el.classList.remove("over"); addMove(ev.dataTransfer.getData("text/bus"), el.dataset.b); });
    el.onclick = () => rb.sel && addMove(rb.sel, el.dataset.b);
  });
}

// ---- small charts (inline SVG, hover tooltips)
function niceTicks(max, n) {
  const raw = max / n, mag = 10 ** Math.floor(Math.log10(raw)), step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  return [...Array(Math.floor(max / step) + 1)].map((_, i) => +(i * step).toFixed(6));
}
function lineChart(el, series) {
  const W = 520, H = 200, L = 40, R = 10, T = 10, B = 24, n = series[0].values.length, max = Math.max(...series.flatMap((s) => s.values)) * 1.08;
  const x = (i) => L + (i * (W - L - R)) / (n - 1), y = (v) => T + (H - T - B) * (1 - v / max);
  let g = niceTicks(max, 4).map((t) => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/><text x="${L - 6}" y="${y(t) + 4}" text-anchor="end">${t >= 1000 ? t / 1000 + "k" : t}</text>`).join("");
  for (let i = 0; i < n; i += 3) g += `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${String(i).padStart(2, "0")}</text>`;
  series.forEach((s) => {
    g += `<path d="${s.values.map((v, i) => (i ? "L" : "M") + x(i).toFixed(1) + "," + y(v).toFixed(1)).join("")}" fill="none" stroke="${s.color}" stroke-width="2"/>`;
    s.values.forEach((v, i) => (g += `<rect x="${x(i) - 8}" y="${T}" width="16" height="${H - T - B}" fill="transparent" data-tip="${hh(i)} · ${s.name}: ${Math.round(v).toLocaleString()} boardings/day"/>`));
  });
  el.innerHTML = `<div class="legend">${series.map((s) => `<span><i class="sw" style="background:${s.color}"></i>${s.name}</span>`).join("")}</div><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Boardings per hour">${g}</svg>`;
}
function barChart(el, data) {
  const W = 520, H = 200, L = 36, R = 6, T = 10, B = 24, max = Math.max(...data.map((d) => d.v), 0.01) * 1.1, bw = (W - L - R) / data.length;
  const y = (v) => T + (H - T - B) * (1 - v / max);
  let g = niceTicks(max * 100, 4).map((t) => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(t / 100)}" y2="${y(t / 100)}"/><text x="${L - 6}" y="${y(t / 100) + 4}" text-anchor="end">${t}%</text>`).join("");
  data.forEach((d, i) => {
    const x0 = L + i * bw + 1, hg = y(0) - y(d.v);
    if (hg >= 1) g += `<path d="M${x0},${y(0)}v${-Math.max(hg - 4, 0)}q0,-4 4,-4h${bw - 10}q4,0 4,4v${Math.max(hg - 4, 0)}z" fill="var(--critical)"/>`;
    g += `<rect x="${x0}" y="${T}" width="${bw - 2}" height="${H - T - B}" fill="transparent" data-tip="${hh(d.x)} · ${(d.v * 100).toFixed(1)}% crowded"/>`;
    if (d.x % 3 === 0) g += `<text x="${x0 + bw / 2}" y="${H - 6}" text-anchor="middle">${String(d.x).padStart(2, "0")}</text>`;
  });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Share crowded by hour">${g}</svg>`;
}

// ================================================================ CITY
// Generic small chart: lines and/or bars over x = 0..n-1, with reference lines and hover tips.
function xyChart(el, { series = [], bars = null, xs, yfmt = (v) => v, ymin = null, ymax = null, refs = [], xlab = (x) => x, every = 3, H = 200, W = 520, onBar }) {
  const L = 44, R = 10, T = 12, B = 24, n = xs.length;
  const vals = [...series.flatMap((s) => s.values), ...(bars ? bars.map((b) => b.v) : []), ...refs.map((r) => r.v)];
  const lo = ymin ?? Math.min(0, ...vals), hi = ymax ?? (Math.max(...vals) * 1.08 || 1);
  const bw = (W - L - R) / n, x = (i) => L + bw * (i + 0.5), y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
  let g = "";
  const step = (hi - lo) / 4;
  for (let k = 0; k <= 4; k++) { const v = lo + k * step; g += `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${yfmt(v)}</text>`; }
  xs.forEach((xv, i) => { if (i % every === 0) g += `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${xlab(xv)}</text>`; });
  let lastLab = -9, stack = 0;
  if (bars) bars.forEach((b, i) => {
    const y0 = y(Math.max(lo, 0)), y1 = y(b.v), top = Math.min(y0, y1), h = Math.abs(y1 - y0);
    g += `<rect x="${x(i) - bw / 2 + 1.5}" y="${top}" width="${bw - 3}" height="${Math.max(h, 1)}" rx="3" fill="${b.color}"/>`;
    if (b.label) {  // neighbouring labels step down so they don't overlap
      stack = i - lastLab === 1 ? stack + 1 : 0; lastLab = i;
      g += `<text x="${x(i)}" y="${b.v < 0 ? y1 + 13 + 12 * stack : y1 - 5 - 12 * stack}" text-anchor="middle" class="blabel">${esc(b.label)}</text>`;
    }
    g += `<rect class="bar-hit" data-i="${i}" x="${x(i) - bw / 2}" y="${T}" width="${bw}" height="${H - T - B}" fill="transparent" data-tip="${b.tip}"/>`;
  });
  refs.forEach((r) => (g += `<line class="ref" x1="${L}" x2="${W - R}" y1="${y(r.v)}" y2="${y(r.v)}"/><text x="${W - R}" y="${y(r.v) - 4}" text-anchor="end">${esc(r.label)}</text>`));
  series.forEach((s) => {
    const pts = s.values.map((v, i) => (v == null ? null : [x(i), y(v)]));
    let d = "", pen = false;
    pts.forEach((p) => { if (!p) { pen = false; return; } d += (pen ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1); pen = true; });
    g += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" ${s.dash ? 'stroke-dasharray="5 4"' : ""}/>`;
  });
  if (series.length && !bars) xs.forEach((xv, i) => {
    const tipTxt = series.map((s) => (s.values[i] == null ? "" : `${s.name}: ${s.fmt ? s.fmt(s.values[i]) : yfmt(s.values[i])}`)).filter(Boolean).join("<br>");
    g += `<rect x="${x(i) - bw / 2}" y="${T}" width="${bw}" height="${H - T - B}" fill="transparent" data-tip="<b>${xlab(xv)}</b><br>${tipTxt}"/>`;
  });
  const leg = series.length > 1 ? `<div class="legend">${series.map((s) => `<span><i class="sw" style="background:${s.color}"></i>${s.name}</span>`).join("")}</div>` : "";
  el.innerHTML = leg + `<svg viewBox="0 0 ${W} ${H}" role="img">${g}</svg>`;
  if (onBar) $$(".bar-hit", el).forEach((r) => (r.onclick = () => onBar(+r.dataset.i)));
}

// ---- time-lapse map: one canvas, redrawn per hour (28k road pieces would be too many SVG lines)
let cmap = null, ccity = null, cproj = null, cplay = null, cmarkers = null;
const getCday = seg("cday", () => loadCityLayer());
const CityLayer = window.L ? L.Layer.extend({
  onAdd(m) { this._c = L.DomUtil.create("canvas", "citycanvas"); m.getPanes().overlayPane.appendChild(this._c); m.on("moveend resize", this._reset, this); this._reset(); },
  onRemove(m) { this._c.remove(); m.off("moveend resize", this._reset, this); },
  _reset() {
    const m = this._map, s = m.getSize(), r = devicePixelRatio || 1;
    L.DomUtil.setPosition(this._c, m.containerPointToLayerPoint([0, 0]));
    Object.assign(this._c, { width: s.x * r, height: s.y * r }); this._c.style.width = s.x + "px"; this._c.style.height = s.y + "px";
    cproj = ccity ? ccity.pieces.map((p) => { const a = m.latLngToContainerPoint([p[0], p[1]]), b = m.latLngToContainerPoint([p[2], p[3]]); return [a.x, a.y, b.x, b.y]; }) : null;
    this.draw();
  },
  draw() {
    if (!this._c || !cproj) return;
    const ctx = this._c.getContext("2d"), r = devicePixelRatio || 1, h = +$("#chour").value, s = this._map.getSize();
    ctx.setTransform(r, 0, 0, r, 0, 0); ctx.clearRect(0, 0, s.x, s.y); ctx.lineCap = "round";
    const z = this._map.getZoom(), wmul = z >= 13 ? 1.6 : z >= 12 ? 1.25 : 1;
    const groups = Array.from({ length: 16 }, () => []);
    ccity.pieces.forEach((p, j) => { const c = p[4][h]; if (c !== "-") groups[parseInt(c, 16)].push(j); });
    groups.forEach((idx, code) => {  // draw seats first, packed last (on top)
      if (!idx.length) return;
      const lv = code >> 2, w = [0.8, 1.6, 2.6, 4][code & 3] * wmul;
      ctx.strokeStyle = LV[KEYS[lv]].color; ctx.globalAlpha = lv === 0 ? 0.45 : 0.9; ctx.lineWidth = w;
      ctx.beginPath();
      for (const j of idx) { const q = cproj[j]; if (q[0] < -50 && q[2] < -50) continue; ctx.moveTo(q[0], q[1]); ctx.lineTo(q[2], q[3]); }
      ctx.stroke();
    });
    ctx.globalAlpha = 1;
  },
}) : null;
let clayer = null;
function cityCaption() {
  const h = +$("#chour").value;
  $("#cclock").textContent = hh(h);
  if (ccity) $("#ccap").textContent = `${ccity.crowded_pieces[h].toLocaleString()} crowded or packed stretches`;
}
async function loadCityLayer() {
  $("#ccap").textContent = "Loading the city…";
  ccity = await api(`/api/city?day=${getCday()}`);
  if (clayer) clayer._reset(); cityCaption();
}
async function loadCity() {
  $("#citylegend").innerHTML = LEVELS.map((l) => `<span><i class="sw" style="background:${l.color}"></i>${l.label}</span>`).join("") + `<span class="muted">Medium/high-confidence stretches only</span>`;
  if (window.L) {
    cmap = L.map("citymap", { zoomSnap: 0.5, zoomAnimation: false, scrollWheelZoom: false }).setView([28.63, 77.15], 11);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' }).addTo(cmap);
    cmap.on("click", () => cmap.scrollWheelZoom.enable());
    clayer = new CityLayer().addTo(cmap);
    cmarkers = L.layerGroup().addTo(cmap);
  }
  $("#chour").oninput = () => { cityCaption(); clayer?.draw(); };
  $("#cplay").onclick = () => {
    if (cplay) { clearInterval(cplay); cplay = null; $("#cplay").textContent = "▶ Play"; return; }
    $("#cplay").textContent = "⏸ Pause";
    cplay = setInterval(() => { const el = $("#chour"); el.value = +el.value >= 23 ? 5 : +el.value + 1; cityCaption(); clayer?.draw(); }, 650);
  };
  loadCityLayer().catch((e) => ($("#ccap").textContent = e.message));
  loadEvents(); loadBunching(); loadNudge(); loadFeedback();
  ["#nshare", "#nmin", "#neve"].forEach((s) => ($(s).oninput = debounce(loadNudge, 150)));
}

// ---- unusual days and places
const MON = (d) => +d.slice(8, 10);
async function loadEvents() {
  const ev = await api("/api/insights/events");
  if (!ev.available) { $("#evchart").innerHTML = `<p class="muted">Rebuild the data to run the detector.</p>`; return; }
  const c = ev.check;
  $("#evcheck").innerHTML = `<span>Known events found: <b>${c.found} of ${c.known}</b></span><span>Days flagged: <b>${c.flagged}</b></span>` +
    (c.unexplained.length ? `<span data-tip="Flagged by the data but not in our calendar list: weather, a strike, or a data gap?">Unexplained: <b>${c.unexplained.map((d) => MON(d) + " Oct").join(", ")}</b></span>` : "");
  const short = (s) => s.replace(/\(.*\)/, "").replace(/Navratri: |Durga |Maha /g, "").trim();
  const bars = ev.days.map((d) => ({
    v: d.change_pct, color: d.flagged ? (d.known_event ? "var(--s2)" : "var(--critical)") : "var(--wait)",
    label: d.flagged ? (d.known_event ? short(d.known_event) : "?") : "",
    tip: `<b>${d.dow} ${MON(d.date)} Oct</b><br>${Math.round(d.observed).toLocaleString()} digital boardings vs ~${Math.round(d.expected).toLocaleString()} usual<br>${d.change_pct > 0 ? "+" : ""}${d.change_pct}%${d.known_event ? "<br>📅 " + esc(d.known_event) : ""}${d.flagged ? "<br><b>Flagged by the data</b>" : ""}`,
  }));
  const pick = (i) => {
    const d = ev.days[i], hrs = ev.hours[d.date], st = ev.stops.filter((s) => s.date === d.date);
    let html = `<div class="card-in"><b>${d.dow} ${MON(d.date)} Oct</b> · ${d.change_pct > 0 ? "+" : ""}${d.change_pct}% vs a usual ${d.dow}${d.known_event ? ` · 📅 ${esc(d.known_event)}` : d.flagged ? " · not in our calendar" : ""}`;
    if (hrs) html += `<div id="evhours" class="chart"></div>`;
    if (st.length) html += `<p class="muted">Stops with sudden crowds this day: ${st.slice(0, 6).map((s) => `<b>${esc(s.stop)}</b> (×${s.ratio})`).join(", ")}</p>`;
    $("#evday").innerHTML = html + "</div>";
    if (hrs) xyChart($("#evhours"), { xs: hrs.map((r) => r.hour), xlab: hh, every: 3, H: 170,
      series: [{ name: "Usual " + d.dow, values: hrs.map((r) => r.expected), color: "var(--ink-3)", dash: true, fmt: (v) => Math.round(v).toLocaleString() },
               { name: "This day", values: hrs.map((r) => r.observed), color: "var(--s2)", fmt: (v) => Math.round(v).toLocaleString() }],
      yfmt: (v) => (v >= 1000 ? Math.round(v / 1000) + "k" : Math.round(v)), W: 760 });
  };
  xyChart($("#evchart"), { xs: ev.days.map((d) => d.date), xlab: (d) => MON(d), every: 2, bars, yfmt: (v) => Math.round(v) + "%", H: 260, W: 760,
    ymin: Math.min(...ev.days.map((d) => d.change_pct)) * 1.3, ymax: Math.max(20, ...ev.days.map((d) => d.change_pct)) * 1.2, onBar: pick });
  pick(ev.days.findIndex((d) => d.date === "2026-10-20") >= 0 ? ev.days.findIndex((d) => d.date === "2026-10-20") : 0);
  // place clusters
  const byDate = {};
  ev.stops.forEach((s) => (byDate[s.date] = byDate[s.date] || []).push(s));
  const list = Object.entries(byDate).sort((a, b) => b[1].length - a[1].length).slice(0, 9);
  $("#evplaces").innerHTML = list.map(([d, st]) => {
    const day = ev.days.find((x) => x.date === d);
    return `<button data-d="${d}"><div class="d">${day.dow} ${MON(d)} Oct · ${st.length} stop${st.length > 1 ? "s" : ""}</div><div class="s">${st.slice(0, 3).map((s) => esc(s.stop) + " ×" + s.ratio).join(" · ")}</div></button>`;
  }).join("");
  $$("#evplaces button").forEach((b) => (b.onclick = () => {
    const st = byDate[b.dataset.d].filter((s) => s.lat != null);
    if (!cmap || !st.length) return;
    cmarkers.clearLayers();
    st.forEach((s) => L.circleMarker([s.lat, s.lon], { radius: 6 + Math.min(10, s.ratio * 1.5), color: "#111", weight: 2, fillColor: "#8b5cf6", fillOpacity: 0.75 })
      .bindTooltip(`<b>${esc(s.stop)}</b><br>${s.observed} boardings vs ~${Math.round(s.expected)} usual (×${s.ratio})`, { className: "stoptip" }).addTo(cmarkers));
    $("#city").scrollIntoView({ behavior: "smooth" });
    cmap.fitBounds(L.latLngBounds(st.map((s) => [s.lat, s.lon])).pad(0.6), { maxZoom: 14 });
    $("#ccap").textContent = `Sudden crowds on ${MON(b.dataset.d)} Oct (purple)`;
  }));
}

// ---- bunching
async function loadBunching() {
  const b = await api("/api/insights/bunching");
  const pk = b.by_hour.weekday.slice(8, 10).reduce((a, c) => a + c, 0) / 2, ev = b.by_hour.weekday.slice(19, 22).reduce((a, c) => a + c, 0) / 3;
  $("#bnkpi").innerHTML = `<span>Whole city: <b>${b.city_g}</b></span><span>5 am: <b>${b.by_hour.weekday[5].toFixed(2)}</b></span><span>Evening: <b>${ev.toFixed(2)}</b></span>`;
  const hrs = [...Array(18)].map((_, i) => i + 5);  // 05-22: too few buses after 23:00 to measure
  const top = Math.max(1.2, ...hrs.flatMap((h) => [b.by_hour.weekday[h], b.by_hour.weekend[h]]));
  xyChart($("#bnchart"), { xs: hrs, xlab: hh, every: 3, W: 400, H: 240, ymin: 0, ymax: Math.ceil(top * 5) / 5, yfmt: (v) => v.toFixed(1),
    refs: [{ v: 1, label: "random" }],
    series: [{ name: "Weekday", values: hrs.map((h) => b.by_hour.weekday[h]), color: "var(--s1)", fmt: (v) => v.toFixed(2) },
             { name: "Weekend", values: hrs.map((h) => b.by_hour.weekend[h]), color: "var(--s2)", fmt: (v) => v.toFixed(2) }] });
  $("#bntable tbody").innerHTML = b.worst_routes.slice(0, 8).map((r) => `<tr><td><button class="rt" data-r="${esc(r.route)}">${esc(r.route)}</button></td><td class="muted">${esc(r.from)} → ${esc(r.to)}</td><td class="num">${r.dep_per_hour}</td><td class="num"><b>${r.g_peak.toFixed(2)}</b></td></tr>`).join("");
  $$("#bntable .rt").forEach((x) => (x.onclick = () => openRoute(x.dataset.r, null, null, 18)));
}

// ---- nudge simulator
async function loadNudge() {
  const share = +$("#nshare").value, min = +$("#nmin").value;
  $("#nshareout").textContent = share + "%";
  $("#nminout").textContent = (min > 0 ? "+" : "") + min + " min" + (min > 0 ? " (later)" : min < 0 ? " (earlier)" : "");
  const r = await api(`/api/insights/nudge?share=${share / 100}&minutes=${min}&evening=${$("#neve").checked}`);
  const fmt = (n) => Math.round(n).toLocaleString();
  $("#nkpi").innerHTML = [
    [fmt(r.riders_shifted_per_day), "riders shift each weekday"],
    [`${r.crowded_change_pct > 0 ? "+" : ""}${r.crowded_change_pct}%`, "crowded passenger-hours", r.crowded_change_pct < 0],
    [fmt(r.seated_pax_hours_gained), "more passenger-hours seated", r.seated_pax_hours_gained > 0],
    [`${r.packed_route_hours_before} → ${r.packed_route_hours_after}`, "route-hours with a packed stretch", r.packed_route_hours_after < r.packed_route_hours_before],
  ].map(([v, l, good]) => `<div class="kpi"><div class="v ${good ? "good" : ""}">${v}</div><div class="l">${l}</div></div>`).join("");
  const hrs = [...Array(19)].map((_, i) => i + 5);
  xyChart($("#nchart"), { xs: hrs, xlab: hh, every: 3, W: 760, yfmt: (v) => (v >= 1000 ? Math.round(v / 1000) + "k" : Math.round(v)),
    series: [{ name: "Today", values: hrs.map((h) => r.by_hour_before[h]), color: "var(--ink-3)", dash: true, fmt },
             { name: "With the nudge", values: hrs.map((h) => r.by_hour_after[h]), color: "var(--good)", fmt }] });
}

// ---- rider feedback
async function loadFeedback() {
  const s = await api("/api/feedback");
  if (!s.n) { $("#fbbox").innerHTML = `<p class="muted">No rider reports yet. Plan a trip on WhatsApp or here, then tell us how full the bus was.</p>`; return; }
  const head = KEYS.map((k) => `<th>${LV[k].icon}</th>`).join("");
  const rows = KEYS.map((p) => `<tr><th>${LV[p].label}</th>${KEYS.map((r) => `<td class="${p === r ? "hit" : ""}">${s.confusion[p][r] || ""}</td>`).join("")}</tr>`).join("");
  $("#fbbox").innerHTML = `<div class="kpis">
      <div class="kpi"><div class="v">${s.n}</div><div class="l">rider reports</div></div>
      <div class="kpi"><div class="v">${s.exact_pct}%</div><div class="l">exactly the level we predicted</div></div>
      <div class="kpi"><div class="v">${s.within_one_pct}%</div><div class="l">within one level</div></div>
      <div class="kpi"><div class="v">${s.predicted_seat_pct}% → ${s.reported_seat_pct}%</div><div class="l">seat chance predicted → riders who got one</div></div></div>
    <div class="tablewrap"><table class="conf"><thead><tr><th>Model said ↓ · riders said →</th>${head}</tr></thead><tbody>${rows}</tbody></table></div>`;
}
async function rateRide(btn) {
  const o = lastPlan.options[+btn.closest(".trip").dataset.i], l = o.legs[0];
  const trip = { route: l.route, hour: Math.floor(l.board_min / 60) % 24, daytype: getDay(), from: l.from, to: l.to, predicted: l.worst_level, seat_pct: l.seat_board_pct };
  await post("/api/feedback", { trip, reported: btn.dataset.r, channel: "web" });
  btn.closest(".rate").innerHTML = `🙏 Thanks! ${btn.dataset.r === l.worst_level ? "That matches our estimate." : "Noted: this helps check the model."}`;
  if (loaded.city) loadFeedback();
}

// ================================================================ DATA
async function loadAbout() {
  const i = await api("/api/info"), inf = i.info;
  loadWaStatus();
  const eng = { claude: "Claude (Anthropic API)", local: `Local AI · ${i.local_model}`, rules: "Built-in parser (no AI running)" }[i.ai];
  $("#modelinfo").innerHTML = [["Data", inf.source_csv], ["Tickets", inf.tickets.toLocaleString()], ["Built", inf.built_at],
    ["Expansion", "×" + inf.expansion_factor.toFixed(1)], ["GTFS", i.gtfs ? "loaded" : "not loaded"], ["Assistant", eng]]
    .map(([k, v]) => `<span>${k}</span><span>${esc(v)}</span>`).join("");
  $("#note").innerHTML = mdToHtml(await (await fetch("/api/note")).text());
}
const drop = $("#drop");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, () => drop.classList.remove("over")));
drop.addEventListener("drop", (e) => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) upload(f); });
$("#pick").onclick = () => $("#file").click();
$("#file").onchange = (e) => e.target.files[0] && upload(e.target.files[0]);
async function upload(f) {
  const log = $("#joblog"); log.hidden = false; log.textContent = `Uploading ${f.name} (${(f.size / 1e6).toFixed(1)} MB)…`;
  const fd = new FormData(); fd.append("file", f);
  try {
    let j = await api("/api/upload", { method: "POST", body: fd });
    while (j.status === "queued" || j.status === "running") { log.textContent = j.log.join("\n"); log.scrollTop = 1e9; await new Promise((r) => setTimeout(r, 1200)); j = await api(`/api/jobs/${j.id}`); }
    log.textContent = j.log.join("\n") + `\n\n${j.status === "done" ? "✅ Model rebuilt." : "❌ Rebuild failed."}`;
  } catch (err) { log.textContent += "\n" + err.message; }
}
function mdToHtml(md) {
  let html = "", inT = false, inL = false;
  const inline = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  for (const ln of md.split("\n")) {
    if (/^\|/.test(ln)) { if (/^\|[\s:-]+\|/.test(ln)) continue; html += (inT ? "" : "<table>") + "<tr>" + ln.split("|").slice(1, -1).map((c) => `<td>${inline(c.trim())}</td>`).join("") + "</tr>"; inT = true; continue; }
    else if (inT) { html += "</table>"; inT = false; }
    if (/^\s*[-*] /.test(ln)) { html += (inL ? "" : "<ul>") + `<li>${inline(ln.replace(/^\s*[-*] /, ""))}</li>`; inL = true; continue; }
    else if (inL) { html += "</ul>"; inL = false; }
    const h = ln.match(/^(#{1,3}) (.*)/); if (h) html += `<h2>${inline(h[2])}</h2>`; else if (ln.trim() && !ln.startsWith("```")) html += `<p>${inline(ln)}</p>`;
  }
  return html + (inT ? "</table>" : "") + (inL ? "</ul>" : "");
}

// ================================================================ AI CHAT (live, streamed)
const chat = $("#chat"), msgs = $("#msgs"), getLang = seg("lang");
const convo = [];
const TOOL_LABEL = { search_places: "📍 Looking up places", plan_journey: "🚌 Planning your trip", route_crowding: "📊 Checking route crowding", operator_hotspots: "🔥 Finding overloaded routes" };
const ENGINE_LABEL = { claude: "Claude", local: "Local AI (on this Mac)", rules: "Built-in parser" };
$("#chatfab").onclick = () => { chat.hidden = false; $("#chatfab").hidden = true; $("#askq").focus(); };
$("#chatclose").onclick = () => { chat.hidden = true; $("#chatfab").hidden = false; stopSpeaking(); };
$$("#msgs .sugg button").forEach((b) => (b.onclick = () => ask(b.textContent)));
$("#chatform").onsubmit = (e) => { e.preventDefault(); ask($("#askq").value); };
function addMsg(cls, html) { const d = document.createElement("div"); d.className = "msg " + cls; d.innerHTML = html; msgs.appendChild(d); msgs.scrollTop = 1e9; return d; }

async function ask(q) {
  q = q.trim(); if (!q) return;
  stopSpeaking(); $("#askq").value = "";
  addMsg("user", esc(q));
  const steps = document.createElement("div"); steps.className = "steps"; msgs.appendChild(steps);
  const step = (text) => { $$(".st.run", steps).forEach((s) => s.classList.replace("run", "ok")); const s = document.createElement("span"); s.className = "st run"; s.textContent = text; steps.appendChild(s); msgs.scrollTop = 1e9; };
  step("Reading your question…");
  let engine = "rules", answer = "", note = "", plans = [];
  try {
    const res = await fetch("/api/ask/stream", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, day: getDay(), lang: getLang(), history: convo.slice(-6) }) });
    const reader = res.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl); buf = buf.slice(nl + 1); if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.type === "engine") { engine = ev.engine; $("#engine").textContent = " · " + ENGINE_LABEL[engine]; }
        else if (ev.type === "step") step(ev.text);
        else if (ev.type === "tool") step(TOOL_LABEL[ev.name] || ev.name);
        else if (ev.type === "plan") plans.push(ev.params);
        else if (ev.type === "answer") { answer = ev.text; engine = ev.engine; note = ev.note || ""; }
      }
    }
  } catch (err) { answer = "Sorry, something went wrong: " + err.message; }
  $$(".st.run", steps).forEach((s) => s.classList.replace("run", "ok"));
  const m = addMsg("bot", esc(answer) + `<span class="eng">${ENGINE_LABEL[engine] || engine}${note ? " · " + esc(note) : ""}</span>`);
  convo.push({ role: "user", content: q }, { role: "assistant", content: answer });
  if (plans.length) {
    const p = plans.at(-1);
    const box = document.createElement("div"); box.className = "minitrip";
    box.innerHTML = `<button>📍 Show these trips on the Trip page</button>`;
    box.firstChild.onclick = () => { setPlanForm(p); showTab("trip"); plan({ from: p.origin, to: p.destination, time: p.time, day: p.day, comfort: p.comfort }); };
    m.appendChild(box);
    setPlanForm(p); plan({ from: p.origin, to: p.destination, time: p.time, day: p.day, comfort: p.comfort });
  }
  speak(answer);
}

// voice in (Web Speech API) and out (speechSynthesis)
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null;
$("#mic").onclick = () => {
  if (!SR) { addMsg("bot", "Voice input needs Chrome, Edge or Safari."); return; }
  if (rec) { rec.stop(); return; }
  stopSpeaking();
  rec = new SR(); rec.lang = getLang() === "hi-IN" ? "hi-IN" : "en-IN"; rec.interimResults = true;
  rec.onstart = () => $("#mic").classList.add("live");
  rec.onresult = (e) => { $("#askq").value = [...e.results].map((r) => r[0].transcript).join(" "); };
  rec.onend = () => { $("#mic").classList.remove("live"); rec = null; if ($("#askq").value.trim()) ask($("#askq").value); };
  rec.start();
};
function stopSpeaking() { if (window.speechSynthesis) speechSynthesis.cancel(); $("#stopspeak").hidden = true; }
$("#stopspeak").onclick = stopSpeaking;
$("#speak").onchange = (e) => { if (!e.target.checked) stopSpeaking(); };
addEventListener("keydown", (e) => { if (e.key === "Escape") stopSpeaking(); });
function speak(text) {
  if (!$("#speak").checked || !window.speechSynthesis || !text) return;
  const u = new SpeechSynthesisUtterance(text.split("\n").slice(0, 3).join(". ").replace(/[•→]/g, ", "));
  u.lang = getLang() !== "en-IN" || /\b(sabse|chahiye|milega|baje|aap)\b/i.test(text) ? "hi-IN" : "en-IN";
  const v = speechSynthesis.getVoices().find((v) => v.lang === u.lang); if (v) u.voice = v;
  u.onstart = () => ($("#stopspeak").hidden = false);
  u.onend = u.onerror = () => ($("#stopspeak").hidden = true);
  speechSynthesis.speak(u);
}

// ================================================================ SHARE (QR codes)
const qr = (u) => "/api/qr.svg?scale=8&data=" + encodeURIComponent(u);
$("#sharebtn").onclick = async () => {
  const sh = await api("/api/share");
  $("#qrsite").src = qr(sh.site_url); $("#siteurl").textContent = sh.site_url;
  $("#qrwabox").hidden = !sh.whatsapp_url; if (sh.whatsapp_url) $("#qrwa").src = qr(sh.whatsapp_url);
  $("#qrtgbox").hidden = !sh.telegram_url; if (sh.telegram_url) $("#qrtg").src = qr(sh.telegram_url);
  $("#sharenote").textContent = sh.public_url ? "Anyone can open this link." :
    "This QR works for phones on the same Wi-Fi as this Mac (start the server with --host 0.0.0.0). For a public link, run a tunnel and set PUBLIC_URL — see docs/WHATSAPP_SETUP.md."
    + (sh.whatsapp_url || sh.telegram_url ? "" : " Add TELEGRAM_BOT_TOKEN (or WHATSAPP_NUMBER) to show a chat QR.");
  $("#sharemodal").hidden = false;
};
$("#shareclose").onclick = () => ($("#sharemodal").hidden = true);
$("#sharemodal").onclick = (e) => { if (e.target.id === "sharemodal") $("#sharemodal").hidden = true; };

// ================================================================ WHATSAPP tester (same code path as the real bot)
const waMsgs = $("#wamsgs");
const waAdd = (cls, html) => { const d = document.createElement("div"); d.className = "wa " + cls; d.innerHTML = html; waMsgs.appendChild(d); waMsgs.scrollTop = 1e9; return d; };
const waTime = () => new Date().toTimeString().slice(0, 5);
const waFmt = (t) => esc(t).replace(/\*([^*\n]+)\*/g, "<b>$1</b>").replace(/_([^_\n]+)_/g, "<i>$1</i>");
function waRender(m) {
  const t = `<span class="meta">${waTime()}</span>`;
  if (m.type === "text") return waAdd("b", waFmt(m.body) + t);
  if (m.type === "voice") return waAdd("b", `🔊 Voice reply<audio controls src="${m.url}"></audio>` + t);
  if (m.type === "image") return waAdd("b img", `<a href="${m.url}" target="_blank"><img src="${m.url}" alt="${esc(m.caption)}"></a>${m.caption ? waFmt(m.caption) : ""}` + t);
  if (m.type === "cta") return waAdd("b", waFmt(m.body) + t + `<a class="wa-btn" href="${esc(m.url)}" target="_blank">${esc(m.label)}</a>`);
  if (m.type === "buttons") {
    const d = waAdd("b", waFmt(m.body) + t + m.buttons.map(([id, title]) => `<button class="wa-btn" data-id="${esc(id)}">${esc(title)}</button>`).join(""));
    $$(".wa-btn", d).forEach((b) => (b.onclick = () => waChoose(b.dataset.id, b.textContent)));
    return d;
  }
  if (m.type === "list") {
    const d = waAdd("b", waFmt(m.body) + t + `<button class="wa-btn">☰ ${esc(m.button)}</button>`);
    $(".wa-btn", d).onclick = () => {
      const sheet = waAdd("sheet", `<b>${esc(m.button)}</b>` + m.rows.map(([id, title, desc]) =>
        `<button class="wa-row" data-id="${esc(id)}" data-title="${esc(title)}"><b>${esc(title)}</b><small>${esc(desc)}</small></button>`).join(""));
      $$(".wa-row", sheet).forEach((r) => (r.onclick = () => { sheet.remove(); waChoose(r.dataset.id, r.dataset.title); }));
    };
    return d;
  }
}
function waChoose(id, title) {
  const fd = new FormData(); fd.append("choice_id", id); fd.append("choice_title", title);
  waSend(fd, esc(title));
}
async function waSend(fd, userBubble) {
  waAdd("u", userBubble + `<span class="meta">${waTime()} ✓✓</span>`);
  const typing = waAdd("b", "<i>typing…</i>");
  try {
    const r = await api("/api/whatsapp/simulate", { method: "POST", body: fd });
    typing.remove();
    if (r.heard) waAdd("sys", `🎙️ You said: “${esc(r.heard)}”`);
    r.messages.forEach(waRender);
  } catch (err) { typing.remove(); waAdd("sys", "Error: " + esc(err.message)); }
}
$("#waform").onsubmit = (e) => {
  e.preventDefault(); const t = $("#watext").value.trim(); if (!t) return;
  $("#watext").value = ""; const fd = new FormData(); fd.append("text", t); waSend(fd, esc(t));
};
let waRec = null, waChunks = [];
$("#warec").onclick = async () => {
  if (waRec) { waRec.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    waChunks = []; waRec = new MediaRecorder(stream);
    waRec.ondataavailable = (e) => waChunks.push(e.data);
    waRec.onstop = () => {
      stream.getTracks().forEach((t) => t.stop()); $("#warec").classList.remove("live");
      const type = waRec.mimeType || "audio/webm"; const blob = new Blob(waChunks, { type }); waRec = null;
      const fd = new FormData(); fd.append("audio", blob, "note." + (type.includes("mp4") ? "m4a" : "webm"));
      waSend(fd, `🎙️ Voice note<audio controls src="${URL.createObjectURL(blob)}"></audio>`);
    };
    waRec.start(); $("#warec").classList.add("live");
  } catch (err) { waAdd("sys", "Microphone not available: " + esc(err.message)); }
};
async function loadWaStatus() {
  const st = await api("/api/whatsapp/status").catch(() => null);
  if (st) $("#wastat").textContent = st.configured ? `✅ Live on WhatsApp${st.number ? " · +" + st.number : ""}` : "Not connected to WhatsApp yet · see docs/WHATSAPP_SETUP.md";
}

// ================================================================ boot
(async function boot() {
  const i = await api("/api/info");
  i.levels.forEach((l) => (LV[l.key] = l)); LEVELS = i.levels;
  $("#legend").innerHTML = LEVELS.map((l) => `<span><i class="sw" style="background:${l.color}"></i>${l.label}</span>`).join("");
  $("#engine").textContent = " · " + ENGINE_LABEL[i.ai];
  if (i.readonly) {  // hosted demo: one shared copy, so no rebuilds or global sliders
    $("#drop").innerHTML = `<b>Rebuilding is off on the hosted demo</b><span class="muted">Run BheedBus on your own computer to load a new month of tickets or a GTFS feed.</span>`;
    $("#scale").disabled = true; $("#scaleout").textContent = "(off on the hosted demo)";
  }
  const tab = location.hash.slice(1), qs = new URLSearchParams(location.search);
  if (qs.get("route")) { $("#rq").value = qs.get("route"); setHour(+qs.get("h") || 9); }
  showTab(["trip", "routes", "city", "ops", "about"].includes(tab) ? tab : "trip");
  plan();
})();
