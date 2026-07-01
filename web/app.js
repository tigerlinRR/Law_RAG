"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt != null) n.textContent = txt;
  return n;
};

/* ---------------- tabs ---------------- */
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    tab.classList.add("active");
    $("#view-" + tab.dataset.view).classList.add("active");
    if (tab.dataset.view === "library") loadLibrary();
    if (tab.dataset.view === "users") loadUsers();
  });
});

/* ---------------- stats + filters ---------------- */
async function loadStats() {
  try {
    const res = await fetch("/api/stats");
    if (res.status === 401) { showLogin(); return; }
    const s = await res.json();
    $("#stats").innerHTML =
      `<b>${s.documents.toLocaleString()}</b> documents · <b>${s.chunks.toLocaleString()}</b> passages`;
    fillSelect("#f-client", s.clients);
    fillSelect("#f-doc_type", s.doc_types);
    fillSelect("#f-author", s.authors);
  } catch (e) {
    $("#stats").textContent = "";
  }
}
function fillSelect(sel, values) {
  const node = $(sel);
  node.length = 1;   // keep the "Any" placeholder, drop stale options
  (values || []).forEach((v) => node.appendChild(new Option(v, v)));
}

/* ---------------- search ---------------- */
$("#searchForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const query = $("#q").value.trim();
  if (!query) return;
  const status = $("#searchStatus"), results = $("#results");
  results.innerHTML = "";
  status.className = "status";
  status.innerHTML = '<span class="spinner"></span>Searching…';

  const body = {
    query,
    client: $("#f-client").value,
    doc_type: $("#f-doc_type").value,
    author: $("#f-author").value,
    rerank: $("#f-rerank").checked,
  };
  try {
    const data = await postJSON("/api/search", body);
    if (!data.hits.length) {
      status.textContent = "No matching documents.";
      return;
    }
    status.textContent =
      `${data.hits.length} result${data.hits.length > 1 ? "s" : ""}` +
      (data.reranked ? " · AI-reranked" : "");
    data.hits.forEach((h) => results.appendChild(resultCard(h)));
  } catch (err) {
    status.className = "status err";
    status.textContent = "Search failed: " + err.message;
  }
});

function resultCard(h) {
  const card = el("div", "card");
  const head = el("div", "card-head");
  const name = el("div", "doc-name");
  name.textContent = h.filename;
  if (h.doc_type) name.appendChild(el("span", "badge", h.doc_type));
  head.appendChild(name);
  const scoreTxt = h.reranked
    ? `relevance ${h.score.toFixed(3)}`
    : `score ${h.score.toFixed(3)}`;
  head.appendChild(el("div", "score", scoreTxt));
  card.appendChild(head);

  const meta = el("div", "meta");
  [h.client, h.author, h.doc_date, h.page ? "p. " + h.page : null]
    .filter(Boolean)
    .forEach((m) => meta.appendChild(el("span", null, m)));
  card.appendChild(meta);

  card.appendChild(el("div", "snippet", h.content.trim()));
  return card;
}

/* ---------------- contract review ---------------- */
const dz = $("#dropzone"), fileInput = $("#fileInput");
dz.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => { if (fileInput.files.length) reviewFiles(fileInput.files); });
["dragover", "dragenter"].forEach((ev) =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => {
  if (e.dataTransfer.files.length) reviewFiles(e.dataTransfer.files);
});

let lastReviews = [];

async function reviewFiles(fileList) {
  const status = $("#reviewStatus"), report = $("#report");
  report.innerHTML = "";
  lastReviews = [];
  const files = Array.from(fileList);
  const errors = [];
  status.className = "status";

  for (let i = 0; i < files.length; i++) {
    status.innerHTML =
      `<span class="spinner"></span>Analyzing ${i + 1}/${files.length} — <b>${escapeHtml(files[i].name)}</b> (~30s each)…`;
    const fd = new FormData();
    fd.append("file", files[i]);
    try {
      const res = await fetch("/api/summarize", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "review failed");
      lastReviews.push(data);
    } catch (err) {
      errors.push(`${files[i].name}: ${err.message}`);
    }
  }

  if (!lastReviews.length) {
    status.className = "status err";
    status.textContent = "Review failed. " + errors.join(" · ");
    return;
  }
  status.className = "status";
  status.textContent = errors.length ? ("Skipped — " + errors.join(" · ")) : "";

  report.appendChild(exportBar());
  if (lastReviews.length === 1) {
    renderReportInto(lastReviews[0], report);
  } else {
    report.appendChild(batchTable(lastReviews));
    lastReviews.forEach((r) => {
      const det = el("details", "det");
      const sum = el("summary", null,
        (r._source || "Contract") + (r.doc_type ? "  ·  " + r.doc_type : ""));
      det.appendChild(sum);
      const holder = el("div");
      det.appendChild(holder);
      det.addEventListener("toggle", () => {
        if (det.open && !holder.dataset.done) {
          renderReportInto(r, holder);
          holder.dataset.done = "1";
        }
      });
      report.appendChild(det);
    });
  }
}

function exportBar() {
  const bar = el("div", "exportbar");
  bar.appendChild(el("span", "exp-label", "Export report:"));
  const xls = el("button", "btn-ghost", "Excel (.xlsx)");
  const doc = el("button", "btn-ghost", "Word (.docx)");
  xls.addEventListener("click", () => downloadExport("excel"));
  doc.addEventListener("click", () => downloadExport("word"));
  bar.appendChild(xls);
  bar.appendChild(doc);
  return bar;
}

async function downloadExport(fmt) {
  if (!lastReviews.length) return;
  try {
    const res = await fetch(`/api/export/${fmt}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reviews: lastReviews }),
    });
    if (!res.ok) throw new Error("export failed");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = el("a");
    a.href = url;
    a.download = fmt === "excel" ? "due_diligence.xlsx" : "due_diligence.docx";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    const s = $("#reviewStatus");
    s.className = "status err";
    s.textContent = "Export failed: " + err.message;
  }
}

function batchTable(reviews) {
  const panel = el("div", "panel");
  panel.appendChild(el("h3", null, `Comparison — ${reviews.length} contracts`));
  const table = el("table", "clauses");
  table.innerHTML =
    "<thead><tr><th>File</th><th>Type</th><th>Term</th><th>Termination</th><th>Governing law</th><th>Risks</th></tr></thead>";
  const tb = el("tbody");
  reviews.forEach((r) => {
    const cm = {};
    (r.clauses || []).forEach((c) => { cm[c.name] = c.value; });
    const tr = el("tr");
    tr.appendChild(el("td", "name", r._source || ""));
    tr.appendChild(el("td", null, r.doc_type || "—"));
    tr.appendChild(el("td", null, cm["Term / Duration"] || "—"));
    tr.appendChild(el("td", null, cm["Termination"] || "—"));
    tr.appendChild(el("td", null, cm["Governing Law"] || "—"));
    const n = (r.key_risks || []).length;
    const rt = el("td");
    rt.appendChild(el("span", "pill " + (n ? "err2" : "ok"), String(n)));
    tr.appendChild(rt);
    tb.appendChild(tr);
  });
  table.appendChild(tb);
  panel.appendChild(table);
  return panel;
}

function renderReportInto(r, report) {
  const head = el("div", "report-head");
  const h2 = el("h2", null, r._source || "Contract");
  if (r.doc_type) h2.appendChild(el("span", "badge", r.doc_type));
  head.appendChild(h2);
  report.appendChild(head);

  if (r.summary) {
    const p = el("div", "panel");
    p.appendChild(el("h3", null, "Summary"));
    p.appendChild(el("div", "summary", r.summary));
    report.appendChild(p);
  }

  if (r.parties && r.parties.length) {
    const p = el("div", "panel");
    p.appendChild(el("h3", null, "Parties"));
    const chips = el("div", "chips");
    r.parties.forEach((x) => chips.appendChild(el("span", "chip", x)));
    p.appendChild(chips);
    report.appendChild(p);
  }

  if (r.clauses && r.clauses.length) {
    const p = el("div", "panel");
    p.appendChild(el("h3", null, "Key clauses"));
    const table = el("table", "clauses");
    table.innerHTML =
      "<thead><tr><th>Clause</th><th>Value</th><th>Source quote</th></tr></thead>";
    const tb = el("tbody");
    r.clauses.forEach((c) => {
      const absent = !c.value || c.value.trim().toLowerCase() === "not found";
      const tr = el("tr", absent ? "absent" : "");
      tr.appendChild(el("td", "name", c.name));
      tr.appendChild(el("td", "val", c.value || "—"));
      tr.appendChild(el("td", "quote", c.quote || ""));
      tb.appendChild(tr);
    });
    table.appendChild(tb);
    p.appendChild(table);
    report.appendChild(p);
  }

  if (r.key_risks && r.key_risks.length) {
    const p = el("div", "panel risks");
    p.appendChild(el("h3", null, "Key risks to review"));
    r.key_risks.forEach((x) => {
      const row = el("div", "risk-item");
      row.appendChild(el("span", "flag", "⚑"));
      row.appendChild(el("span", null, x));
      p.appendChild(row);
    });
    report.appendChild(p);
  }

  const d = el("div", "disclaimer");
  d.textContent =
    "AI-assisted review generated locally. It may miss or misread terms — always verify against the source document before relying on it.";
  report.appendChild(d);
}

/* ---------------- add to library ---------------- */
const az = $("#addzone"), addInput = $("#addInput");
az.addEventListener("click", () => addInput.click());
addInput.addEventListener("change", () => { if (addInput.files.length) ingestFiles(addInput.files); });
["dragover", "dragenter"].forEach((ev) =>
  az.addEventListener(ev, (e) => { e.preventDefault(); az.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) =>
  az.addEventListener(ev, (e) => { e.preventDefault(); az.classList.remove("drag"); }));
az.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) ingestFiles(e.dataTransfer.files); });

async function ingestFiles(fileList) {
  const status = $("#addStatus"), out = $("#addResults");
  out.innerHTML = "";
  status.className = "status";
  const n = fileList.length;
  status.innerHTML =
    `<span class="spinner"></span>Reading & indexing ${n} file${n > 1 ? "s" : ""} — detecting type, parties & date…`;

  const fd = new FormData();
  Array.from(fileList).forEach((f) => fd.append("files", f));
  try {
    const res = await fetch("/api/ingest", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "ingest failed");
    status.textContent = "";
    out.appendChild(ingestTable(data.results));
    loadStats();           // refresh counts + filter dropdowns
  } catch (err) {
    status.className = "status err";
    status.textContent = "Ingest failed: " + err.message;
  }
}

const STATUS_LABEL = {
  ingested: ["Added", "ok"], skipped_duplicate: ["Already in library", "dim"],
  needs_ocr: ["Scanned — needs OCR", "warn"], error: ["Error", "err2"],
};

function ingestTable(results) {
  const panel = el("div", "panel");
  const table = el("table", "clauses");
  table.innerHTML =
    "<thead><tr><th>File</th><th>Status</th><th>Type</th><th>Client</th><th>Date</th><th>Parties</th></tr></thead>";
  const tb = el("tbody");
  results.forEach((r) => {
    const [label, cls] = STATUS_LABEL[r.status] || [r.status, ""];
    const tr = el("tr");
    tr.appendChild(el("td", "name", r.filename));
    const st = el("td"); st.appendChild(el("span", "pill " + cls, label));
    if (r.detail && r.status === "error") st.appendChild(el("div", "detail", r.detail));
    tr.appendChild(st);
    tr.appendChild(el("td", null, r.doc_type || "—"));
    tr.appendChild(el("td", null, r.client || "—"));
    tr.appendChild(el("td", null, r.doc_date || "—"));
    tr.appendChild(el("td", "quote", (r.parties || []).join(", ")));
    tb.appendChild(tr);
  });
  table.appendChild(tb);
  panel.appendChild(el("p", "auto-note",
    "Type, client, parties and date were auto-detected — review and correct if needed."));
  panel.appendChild(table);
  return panel;
}

/* ---------------- library ---------------- */
let CURRENT_USER = null;
let libDocs = [];

async function loadLibrary() {
  const status = $("#libStatus"), out = $("#libResults");
  status.className = "status";
  status.innerHTML = '<span class="spinner"></span>Loading…';
  out.innerHTML = "";
  try {
    const res = await fetch("/api/documents");
    if (res.status === 401) { showLogin(); return; }
    libDocs = (await res.json()).documents || [];
    status.textContent = `${libDocs.length} document${libDocs.length === 1 ? "" : "s"}`;
    renderLibrary(libDocs);
  } catch (err) {
    status.className = "status err";
    status.textContent = "Failed: " + err.message;
  }
}

$("#libFilter").addEventListener("input", (e) => {
  const q = e.target.value.toLowerCase();
  renderLibrary(libDocs.filter((d) =>
    [d.filename, d.client, d.doc_type, d.author, (d.parties || []).join(" ")]
      .join(" ").toLowerCase().includes(q)));
});

function renderLibrary(docs) {
  const out = $("#libResults");
  out.innerHTML = "";
  if (!docs.length) { out.appendChild(el("p", "auto-note", "No documents.")); return; }
  const isAdmin = CURRENT_USER && CURRENT_USER.role === "admin";
  const panel = el("div", "panel");
  const table = el("table", "clauses");
  table.innerHTML = "<thead><tr><th>File</th><th>Type</th><th>Client</th><th>Parties</th>"
    + "<th>Date</th><th>Chunks</th>" + (isAdmin ? "<th></th>" : "") + "</tr></thead>";
  const tb = el("tbody");
  docs.forEach((d) => {
    const tr = el("tr");
    const nameCell = el("td", "name");
    if (d.has_file) {
      const a = el("a", "doclink", d.filename);
      a.href = `/api/documents/${d.id}/file`;
      a.target = "_blank";
      a.rel = "noopener";
      nameCell.appendChild(a);
    } else {
      nameCell.appendChild(document.createTextNode(d.filename));
      nameCell.appendChild(el("span", "notstored", " (not stored)"));
    }
    tr.appendChild(nameCell);
    tr.appendChild(el("td", null, d.doc_type || "—"));
    tr.appendChild(el("td", null, d.client || "—"));
    tr.appendChild(el("td", "quote", (d.parties || []).join(", ")));
    tr.appendChild(el("td", null, d.doc_date || "—"));
    tr.appendChild(el("td", null, String(d.n_chunks || "")));
    if (isAdmin) {
      const td = el("td");
      const del = el("button", "linkbtn", "Delete");
      del.addEventListener("click", () => deleteDoc(d));
      td.appendChild(del);
      tr.appendChild(td);
    }
    tb.appendChild(tr);
  });
  table.appendChild(tb);
  panel.appendChild(table);
  out.appendChild(panel);
}

async function deleteDoc(d) {
  if (!confirm(`Remove "${d.filename}" from the library? This cannot be undone.`)) return;
  const res = await fetch(`/api/documents/${d.id}`, { method: "DELETE" });
  if (res.ok) { loadLibrary(); loadStats(); }
  else { alert("Delete failed"); }
}

/* ---------------- users (admin) ---------------- */
let allClients = [];

async function loadUsers() {
  const status = $("#usersStatus");
  status.className = "status"; status.textContent = "";
  try {
    const [uRes, cRes] = await Promise.all([fetch("/api/users"), fetch("/api/clients")]);
    if (uRes.status === 401) { showLogin(); return; }
    if (uRes.status === 403) { setUsersStatus("Admin only.", true); return; }
    allClients = (await cRes.json()).clients || [];
    renderClientChecks($("#nu-clients"), []);
    renderUsers((await uRes.json()).users || []);
  } catch (err) {
    setUsersStatus("Failed: " + err.message, true);
  }
}

function setUsersStatus(msg, err) {
  const s = $("#usersStatus");
  s.className = "status" + (err ? " err" : "");
  s.textContent = msg;
}

function renderClientChecks(container, selected) {
  container.innerHTML = "";
  if (!allClients.length) {
    container.appendChild(el("span", "auto-note", "(no clients in the library yet)"));
    return;
  }
  allClients.forEach((name) => {
    const lbl = el("label", "chk");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.value = name;
    if (selected.includes(name)) cb.checked = true;
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(" " + name));
    container.appendChild(lbl);
  });
}

function checkedClients(container) {
  return Array.from(container.querySelectorAll("input:checked")).map((cb) => cb.value);
}

$("#nu-create").addEventListener("click", async () => {
  const username = $("#nu-user").value.trim();
  const password = $("#nu-pass").value;
  if (!username || !password) { setUsersStatus("Username and password are required.", true); return; }
  try {
    await postJSON("/api/users", {
      username, password, role: $("#nu-role").value,
      clients: checkedClients($("#nu-clients")),
    });
    $("#nu-user").value = ""; $("#nu-pass").value = "";
    setUsersStatus(`Created ${username}.`, false);
    loadUsers();
  } catch (err) {
    setUsersStatus("Create failed: " + err.message, true);
  }
});

function renderUsers(users) {
  const out = $("#usersList");
  out.innerHTML = "";
  const panel = el("div", "panel");
  panel.appendChild(el("h3", null, "Users"));
  users.forEach((u) => panel.appendChild(userRow(u)));
  out.appendChild(panel);
}

function userRow(u) {
  const det = el("details", "det");
  const sum = el("summary");
  const scope = u.role === "admin" ? "all clients"
    : (u.clients.join(", ") || "no access yet");
  sum.innerHTML = `<b>${escapeHtml(u.username)}</b> `
    + `<span class="pill ${u.role === "admin" ? "warn" : "dim"}">${u.role}</span> `
    + `<span class="who">${escapeHtml(scope)}</span>`;
  det.appendChild(sum);

  const body = el("div", "user-editor");
  const roleSel = document.createElement("select");
  ["lawyer", "admin"].forEach((r) => {
    const o = new Option(r, r); if (r === u.role) o.selected = true; roleSel.appendChild(o);
  });
  const roleWrap = el("label", "fld");
  roleWrap.appendChild(el("span", null, "Role "));
  roleWrap.appendChild(roleSel);
  body.appendChild(roleWrap);

  body.appendChild(el("p", "auto-note", "Client access (ignored for admins):"));
  const checks = el("div", "client-checks");
  body.appendChild(checks);

  const bar = el("div", "exportbar");
  const save = el("button", "btn-ghost", "Save");
  const pwbtn = el("button", "btn-ghost", "Reset password");
  const del = el("button", "btn-ghost", "Delete");
  bar.appendChild(save); bar.appendChild(pwbtn); bar.appendChild(del);
  body.appendChild(bar);
  det.appendChild(body);

  det.addEventListener("toggle", () => {
    if (det.open && !checks.dataset.done) {
      renderClientChecks(checks, u.clients);
      checks.dataset.done = "1";
    }
  });
  save.addEventListener("click", async () => {
    try {
      await putJSON(`/api/users/${encodeURIComponent(u.username)}`,
        { role: roleSel.value, clients: checkedClients(checks) });
      setUsersStatus(`Saved ${u.username}.`, false);
      loadUsers();
    } catch (err) { setUsersStatus("Save failed: " + err.message, true); }
  });
  pwbtn.addEventListener("click", async () => {
    const pw = prompt(`New password for ${u.username}:`);
    if (!pw) return;
    try {
      await putJSON(`/api/users/${encodeURIComponent(u.username)}`, { password: pw });
      setUsersStatus(`Password updated for ${u.username}.`, false);
    } catch (err) { setUsersStatus("Failed: " + err.message, true); }
  });
  del.addEventListener("click", async () => {
    if (!confirm(`Delete user ${u.username}?`)) return;
    const res = await fetch(`/api/users/${encodeURIComponent(u.username)}`, { method: "DELETE" });
    if (res.ok) { setUsersStatus(`Deleted ${u.username}.`, false); loadUsers(); }
    else {
      const d = await res.json().catch(() => ({}));
      setUsersStatus("Delete failed: " + (d.detail || res.status), true);
    }
  });
  return det;
}

/* ---------------- helpers ---------------- */
async function putJSON(url, body) {
  const res = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------- auth ---------------- */
function showLogin() { $("#login").hidden = false; }

function onAuthed(u) {
  CURRENT_USER = u;
  $("#login").hidden = true;
  const role = u.role === "admin" ? " · admin" : "";
  $("#userbox").innerHTML =
    `<span class="who">${escapeHtml(u.username)}${role}</span>` +
    `<button id="logoutBtn" class="logout">Sign out</button>`;
  $("#logoutBtn").addEventListener("click", async () => {
    await fetch("/api/logout", { method: "POST" });
    location.reload();
  });
  document.querySelectorAll(".admin-only").forEach((e) => { e.hidden = u.role !== "admin"; });
  loadStats();
}

$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#loginErr").textContent = "";
  try {
    const data = await postJSON("/api/login",
      { username: $("#lg-user").value, password: $("#lg-pass").value });
    onAuthed(data);
  } catch (err) {
    $("#loginErr").textContent = "Sign-in failed: " + err.message;
  }
});

async function checkAuth() {
  try {
    const res = await fetch("/api/me");
    if (res.ok) { onAuthed(await res.json()); return; }
  } catch (_) {}
  showLogin();
}

checkAuth();
