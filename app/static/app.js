const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { files: [], job: null, pieces: [], extra: [], colors: [], colorById: {}, status: null, mode: "color", ideasLoaded: false };

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  let body = null;
  try { body = await r.json(); } catch {}
  if (!r.ok) throw new Error(body?.error || `Request failed (${r.status})`);
  return body;
}

function banner(msg, kind = "") {
  const b = $("#banner");
  if (!msg) { b.hidden = true; return; }
  b.className = "banner " + kind; b.textContent = msg; b.hidden = false;
}

function show(view) {
  $$(".view").forEach(v => v.hidden = v.id !== "view-" + view);
  const step = view === "progress" ? "upload" : view;
  $$("#steps span").forEach(s => s.classList.toggle("on", s.dataset.step === step));
  window.scrollTo({ top: 0, behavior: "smooth" });
}

const hex = rgb => rgb ? "#" + rgb : "transparent";

// ---------------------------------------------------------------- status
async function loadStatus() {
  try {
    state.status = await api("/api/status");
    $("#drop-limit").textContent = `Up to ${state.status.max_images} photos for a big pile`;
    const c = state.status.catalog;
    if (c.status !== "ready") {
      banner(c.status === "error" ? `Parts catalog failed to load: ${c.detail}` : `Loading the parts catalog (${c.detail || "starting"})… this takes a minute on first start.`, c.status === "error" ? "" : "warn");
      setTimeout(loadStatus, 4000);
    } else {
      banner("");
      if (!state.colors.length) await loadColors();
    }
    $("#tab-ideas").hidden = !state.status.ideas;
    updateGo();
  } catch (e) { banner(e.message); setTimeout(loadStatus, 6000); }
}

async function loadColors() {
  state.colors = await api("/api/colors");
  state.colorById = Object.fromEntries(state.colors.map(c => [c.id, c]));
  $("#add-color").innerHTML = colorOptions(null);
}

function colorOptions(selected, suggested = []) {
  const sug = suggested.map(c => c.id);
  const opt = c => `<option value="${c.id}" ${c.id === selected ? "selected" : ""}>${esc(c.name)}</option>`;
  let html = "";
  if (sug.length) html += `<optgroup label="Best guesses">${suggested.map(s => opt(state.colorById[s.id] || s)).join("")}</optgroup>`;
  html += `<optgroup label="All colours">${state.colors.filter(c => !sug.includes(c.id)).map(opt).join("")}</optgroup>`;
  return html;
}

// ---------------------------------------------------------------- upload
function updateGo() {
  $("#go").disabled = !state.files.length || state.status?.catalog?.status !== "ready";
}

function addFiles(list) {
  const max = state.status?.max_images || 6;
  for (const f of list) {
    if (!f.type.startsWith("image/")) continue;
    if (state.files.length >= max) { banner(`Max ${max} photos.`, "warn"); break; }
    state.files.push(f);
  }
  renderThumbs(); updateGo();
}

function renderThumbs() {
  $("#thumbs").innerHTML = "";
  state.files.forEach((f, i) => {
    const d = document.createElement("div");
    d.className = "thumb";
    d.innerHTML = `<img alt=""><button title="Remove">×</button>`;
    $("img", d).src = URL.createObjectURL(f);
    $("button", d).onclick = e => { e.preventDefault(); state.files.splice(i, 1); renderThumbs(); updateGo(); };
    $("#thumbs").append(d);
  });
}

const drop = $("#drop");
$("#file").onchange = e => { addFiles(e.target.files); e.target.value = ""; };
["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", e => addFiles(e.dataTransfer.files));

$("#go").onclick = async () => {
  const fd = new FormData();
  state.files.forEach(f => fd.append("images", f));
  show("progress");
  $("#prog-title").textContent = "Finding pieces…"; $("#prog-text").textContent = "Uploading"; $("#prog-bar").style.width = "4%";
  try {
    const { id } = await api("/api/jobs", { method: "POST", body: fd });
    state.job = id;
    poll();
  } catch (e) { banner(e.message); show("upload"); }
};

async function poll() {
  try {
    const j = await api(`/api/jobs/${state.job}`);
    if (j.state === "segmenting") { $("#prog-text").textContent = "Looking for pieces in your photo"; $("#prog-bar").style.width = "8%"; }
    if (j.state === "recognising") {
      $("#prog-title").textContent = `Identifying ${j.total} pieces…`;
      $("#prog-text").textContent = `${j.done} of ${j.total} done`;
      $("#prog-bar").style.width = (8 + 92 * j.done / Math.max(1, j.total)) + "%";
    }
    if (j.state === "error") { banner(j.error); show("upload"); return; }
    if (j.state === "done") { onJobDone(j); return; }
    setTimeout(poll, 900);
  } catch (e) { banner(e.message); show("upload"); }
}

// ---------------------------------------------------------------- review
const UNSURE = 0.6;

function onJobDone(j) {
  state.pieces = j.pieces;
  state.extra = [];
  state.ideasLoaded = false;
  $("#ideas").innerHTML = "";
  const w = $("#warn");
  w.hidden = !j.warnings.length; w.textContent = j.warnings.join(" ");
  if (!j.pieces.length) { banner("No pieces found. Try a plainer, more contrasting background with a gap around each piece."); show("upload"); return; }
  renderPieces(); renderInventory(); show("review");
}

function renderPieces() {
  const box = $("#pieces"); box.innerHTML = "";
  const onlyUnsure = $("#only-unsure").checked;
  const tpl = $("#t-piece");
  let shown = 0;
  for (const p of state.pieces) {
    const unsure = !p.part_num || p.score < UNSURE || (p.colors[0]?.distance ?? 99) > 18;
    if (onlyUnsure && !unsure) continue;
    shown++;
    const el = tpl.content.firstElementChild.cloneNode(true);
    el.classList.toggle("unsure", unsure);
    el.classList.toggle("off", !p.include);
    $("img", el).src = `/api/jobs/${state.job}/crops/${p.idx}.jpg`;
    const cand = $(".pcand", el);
    cand.innerHTML = (p.candidates.length ? p.candidates : [{ part_num: "", name: "Not recognised" }]).map(c =>
      `<option value="${esc(c.part_num || "")}" ${c.part_num === p.part_num ? "selected" : ""}>${esc(c.part_num || "?")} · ${esc(c.catalog_name || c.name)}${c.score ? ` (${Math.round(c.score * 100)}%)` : ""}</option>`).join("");
    cand.onchange = () => {
      const c = p.candidates.find(c => c.part_num === cand.value);
      p.part_num = c?.part_num || null; p.part_name = c?.catalog_name || c?.name; p.score = 1;
      el.classList.remove("unsure"); renderInventory();
    };
    const col = $(".pcolor", el), sw = $(".swatch", el);
    col.innerHTML = colorOptions(p.color_id, p.colors);
    sw.style.background = hex(state.colorById[p.color_id]?.rgb);
    col.onchange = () => {
      p.color_id = +col.value; p.color_name = state.colorById[p.color_id]?.name;
      p.colors = p.colors.map(c => ({ ...c, distance: 0 }));
      sw.style.background = hex(state.colorById[p.color_id]?.rgb); renderInventory();
    };
    const inc = $(".pinc", el);
    inc.checked = p.include;
    inc.onchange = () => { p.include = inc.checked; el.classList.toggle("off", !p.include); renderInventory(); };
    box.append(el);
  }
  $("#piece-count").textContent = `${shown} of ${state.pieces.length} pieces shown`;
}
$("#only-unsure").onchange = renderPieces;

function inventory() {
  const m = new Map();
  for (const p of [...state.pieces.filter(p => p.include && p.part_num), ...state.extra]) {
    const k = p.part_num + "|" + p.color_id;
    const cur = m.get(k) || { part_num: p.part_num, color_id: p.color_id, qty: 0, part_name: p.part_name, color_name: state.colorById[p.color_id]?.name || p.color_name };
    cur.qty += p.qty || 1;
    m.set(k, cur);
  }
  return [...m.values()].sort((a, b) => b.qty - a.qty);
}

function renderInventory() {
  const inv = inventory();
  $("#inv-total").textContent = `· ${inv.reduce((s, p) => s + p.qty, 0)} pieces, ${inv.length} kinds`;
  $("#inv-list").innerHTML = inv.map(p => `
    <div class="inv-row" title="${esc(p.part_num)}">
      <span class="swatch" style="background:${hex(state.colorById[p.color_id]?.rgb)}"></span>
      <span>${esc(p.part_name || p.part_num)} <span class="muted">${esc(p.color_name || "")}</span></span>
      <b>×${p.qty}</b>
    </div>`).join("") || `<p class="muted small">Nothing included yet.</p>`;
  $("#to-results").disabled = !inv.length;
}

$("#add-btn").onclick = () => {
  const part = $("#add-part").value.trim();
  if (!part) return;
  const color_id = +$("#add-color").value, qty = Math.max(1, +$("#add-qty").value || 1);
  state.extra.push({ part_num: part, color_id, qty, part_name: part });
  $("#add-part").value = ""; $("#add-qty").value = 1;
  renderInventory();
};

$("#restart").onclick = () => { state.files = []; renderThumbs(); updateGo(); show("upload"); };
$("#to-results").onclick = () => { show("results"); state.ideasLoaded = false; $("#ideas").innerHTML = ""; loadSets(); };
$("#back-review").onclick = () => show("review");

// ---------------------------------------------------------------- results
$$(".tab").forEach(t => t.onclick = () => {
  $$(".tab").forEach(x => x.classList.toggle("on", x === t));
  $("#tab-sets").hidden = t.dataset.tab !== "sets";
  $("#tab-ideasp").hidden = t.dataset.tab !== "ideas";
});
$$("#mode button").forEach(b => b.onclick = () => {
  state.mode = b.dataset.mode;
  $$("#mode button").forEach(x => x.classList.toggle("on", x === b));
  loadSets();
});

async function loadSets() {
  const inv = inventory();
  $("#res-lead").textContent = `From your ${inv.reduce((s, p) => s + p.qty, 0)} pieces. Sets are ranked by how much of each set your pile covers.`;
  const box = $("#sets");
  box.innerHTML = `<div class="empty"><span class="spin"></span> Searching ${state.mode === "color" ? "" : "all colours of "}every set…</div>`;
  try {
    const r = await api("/api/match", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ parts: inv, mode: state.mode }) });
    if (!r.sets.length) { box.innerHTML = `<div class="empty">No sets share enough parts with this pile. Try "Any colour", or the Ideas tab.</div>`; return; }
    box.innerHTML = "";
    for (const s of r.sets) box.append(setCard(s));
  } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

function setCard(s) {
  const pct = Math.round(s.coverage * 100);
  const cls = pct >= 90 ? "" : pct >= 60 ? "mid" : "low";
  const el = document.createElement("div");
  el.className = "set";
  el.innerHTML = `
    ${s.img_url ? `<div class="img"><img loading="lazy" src="${esc(s.img_url)}" alt=""></div>` : `<div class="img none">No picture</div>`}
    <div class="body">
      <h3>${esc(s.name)}</h3>
      <div class="muted small">${esc(s.set_num)} · ${esc(s.theme || "")} · ${s.year || ""}</div>
      <div class="cov ${cls}"><div class="bar"><div style="width:${pct}%"></div></div>${pct}%</div>
      <div class="small">You have <b>${s.parts_matched}</b> of ${s.parts_total} parts${s.missing_count ? `, missing ${s.missing_count}` : " — complete!"}</div>
      ${s.missing.length ? `<details class="missing"><summary>Show missing parts</summary><ul>${s.missing.map(m =>
        `<li><span class="swatch" style="background:${hex(m.rgb)}"></span>${m.qty}× ${esc(m.part_name || m.part_num)} <span class="muted">${esc(m.color_name || "")}</span></li>`).join("")}</ul></details>` : ""}
      <div class="mocbox"></div>
      <div class="links">
        <a href="${esc(s.instructions_url)}" target="_blank" rel="noopener">Instructions</a>
        <a href="${esc(s.rebrickable_url)}" target="_blank" rel="noopener">Rebrickable</a>
        ${state.status?.alternates ? `<a href="#" class="alt">Alternate builds</a>` : ""}
      </div>
    </div>`;
  const alt = $(".alt", el);
  if (alt) alt.onclick = async e => {
    e.preventDefault(); alt.remove();
    const mb = $(".mocbox", el); mb.innerHTML = `<span class="small muted"><span class="spin"></span> Loading…</span>`;
    try {
      const r = await api(`/api/sets/${encodeURIComponent(s.set_num)}/alternates`);
      mb.innerHTML = r.mocs.length ? `<div class="small muted">Fan builds from this set's parts:</div><div class="mocs">${r.mocs.map(m =>
        `<a href="${esc(m.url)}" target="_blank" rel="noopener"><img loading="lazy" src="${esc(m.img_url)}" alt="">${esc(m.name)}</a>`).join("")}</div>` : `<span class="small muted">No alternate builds listed.</span>`;
    } catch (err) { mb.innerHTML = `<span class="small muted">${esc(err.message)}</span>`; }
  };
  return el;
}

$("#ideas-go").onclick = async () => {
  const btn = $("#ideas-go"), box = $("#ideas");
  btn.disabled = true;
  box.innerHTML = `<div class="empty"><span class="spin"></span> Thinking up builds for your pile…</div>`;
  try {
    const r = await api("/api/ideas", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ parts: inventory(), job_id: state.job, audience: $("#audience").value }) });
    box.innerHTML = r.ideas.map(i => `
      <div class="card idea">
        <h3>${esc(i.title)} <span class="pill ${esc(i.difficulty)}">${esc(i.difficulty)}</span></h3>
        <p>${esc(i.description)}</p>
        <div class="uses">Uses: ${(i.uses || []).map(u => `${u.qty}× ${esc(u.part)}${u.color ? " " + esc(u.color) : ""}`).join(", ")}</div>
        <ol>${(i.steps || []).map(s => `<li>${esc(s)}</li>`).join("")}</ol>
      </div>`).join("") || `<div class="empty">No ideas came back. Try again.</div>`;
  } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  btn.disabled = false;
};

loadStatus();
