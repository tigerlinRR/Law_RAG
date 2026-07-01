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
  });
});

/* ---------------- stats + filters ---------------- */
async function loadStats() {
  try {
    const s = await (await fetch("/api/stats")).json();
    $("#stats").innerHTML =
      `<b>${s.documents.toLocaleString()}</b> documents · <b>${s.chunks.toLocaleString()}</b> passages indexed`;
    fillSelect("#f-client", s.clients);
    fillSelect("#f-doc_type", s.doc_types);
    fillSelect("#f-author", s.authors);
  } catch (e) {
    $("#stats").textContent = "";
  }
}
function fillSelect(sel, values) {
  const node = $(sel);
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
fileInput.addEventListener("change", () => { if (fileInput.files[0]) reviewFile(fileInput.files[0]); });
["dragover", "dragenter"].forEach((ev) =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (f) reviewFile(f);
});

async function reviewFile(file) {
  const status = $("#reviewStatus"), report = $("#report");
  report.innerHTML = "";
  status.className = "status";
  status.innerHTML =
    `<span class="spinner"></span>Analyzing <b>${escapeHtml(file.name)}</b> with the local model — this can take ~30s…`;

  const fd = new FormData();
  fd.append("file", file);
  try {
    const res = await fetch("/api/summarize", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "review failed");
    status.textContent = "";
    renderReport(data);
  } catch (err) {
    status.className = "status err";
    status.textContent = "Review failed: " + err.message;
  }
}

function renderReport(r) {
  const report = $("#report");
  report.innerHTML = "";

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

/* ---------------- helpers ---------------- */
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

loadStats();
