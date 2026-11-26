"use strict";
const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem("adminToken") || "";

function td(...xs) { return xs.map(x => { const c = document.createElement("td"); if (x instanceof Node) c.append(x); else c.textContent = x ?? ""; return c; }); }
function tr(cells) { const r = document.createElement("tr"); r.append(...cells); return r; }

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { ...(opts.headers || {}), "X-Admin-Token": token } });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const err = new Error(data.detail || `Ошибка ${r.status}`); err.status = r.status; throw err; }
  return data;
}

function statusMark(d) {
  const el = document.createElement("span");
  if (d.status === "ok") { el.className = "plaque plaque-ok"; el.textContent = "ok"; }
  else { el.className = "plaque plaque-warn"; el.textContent = `ошибка: ${d.error || d.status}`; }
  return el;
}

async function load() {
  const [docs, stats, inbox] = await Promise.all([api("/api/admin/documents"), api("/api/admin/stats"), api("/api/operator/inbox")]);
  $("stats").textContent = `· документов ${stats.documents}, пунктов ${stats.clauses}, ответов ${stats.tokens.answers}, токенов ${stats.tokens.prompt + stats.tokens.completion}`;
  $("docs").replaceChildren(...docs.map(d => {
    const del = document.createElement("button");
    del.textContent = "Удалить"; del.type = "button"; del.className = "none";
    del.onclick = async () => { if (!confirm(`Удалить ${d.rel_path}?`)) return; await api(`/api/admin/documents/${d.doc_id}`, { method: "DELETE" }); load(); };
    return tr(td(d.rel_path, d.box, d.n_clauses, statusMark(d), d.indexed_at, del));
  }));
  $("practice").replaceChildren(...inbox.practice.map(p => {
    const sel = document.createElement("select");
    for (const s of ["new", "approved", "hidden"]) { const o = document.createElement("option"); o.value = s; o.textContent = { new: "новый", approved: "одобрен", hidden: "скрыт" }[s]; o.selected = s === p.status; sel.append(o); }
    sel.onchange = () => api(`/api/operator/practice/${p.id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: sel.value }) });
    return tr(td(p.question, [p.doc_title, p.clause_no].filter(Boolean).join(", п. "), p.text, sel));
  }));
  $("nf").replaceChildren(...inbox.not_found.map(a => tr(td(a.created_at, a.question, a.boxes))));
  $("neg").replaceChildren(...inbox.negative_feedback.map(a => tr(td(a.created_at, a.question, [a.doc_title, a.clause_no].filter(Boolean).join(", п. ")))));
}

async function login() {
  token = $("token").value.trim() || token;
  try {
    await load();
    sessionStorage.setItem("adminToken", token);
    $("auth-msg").textContent = "вход выполнен";
    $("auth-msg").className = "muted";
    $("secured").hidden = false;
    const boxes = await fetch("/api/boxes").then(r => r.json());
    $("box").replaceChildren(new Option("Общие документы", "general"), ...boxes.map(b => new Option(b.name, b.box)));
  } catch (e) {
    $("auth-msg").textContent = e.message; $("auth-msg").className = "err"; $("secured").hidden = true;
  }
}

function showFileNames() {
  const names = $("file-names");
  if (!names) return;
  const files = [...($("file").files || [])].map(f => f.name);
  names.textContent = files.join(", ");
}

function bindDropzone() {
  const dz = $("dropzone");
  const input = $("file");
  if (!dz || !input) return;
  ["dragenter", "dragover"].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add("is-over"); }));
  ["dragleave", "drop"].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove("is-over"); }));
  dz.addEventListener("drop", e => {
    if (!e.dataTransfer || !e.dataTransfer.files.length) return;
    const dt = new DataTransfer();
    for (const f of e.dataTransfer.files) dt.items.add(f);
    input.files = dt.files;
    showFileNames();
  });
  input.addEventListener("change", showFileNames);
}

$("login").onclick = login;
$("upload").onsubmit = async (e) => {
  e.preventDefault();
  const log = $("upload-log");
  for (const f of $("file").files) {
    const send = (replace) => {
      const fd = new FormData(); fd.append("file", f); fd.append("box", $("box").value);
      if (replace) fd.append("replace", "true");
      return api("/api/admin/upload", { method: "POST", body: fd });
    };
    log.textContent += `${f.name}: загружаю…\n`;
    try {
      let r;
      try { r = await send(false); }
      catch (err) {
        // файл с таким именем уже есть: заменяем только после явного согласия
        if (err.status !== 409 || !confirm(`${err.message}\n\nЗаменить прежнюю редакцию?`)) throw err;
        r = await send(true);
      }
      const s = r.stats;
      log.textContent += `  ${r.file}: добавлено пунктов ${s.clauses_added}, удалено ${s.clauses_removed}, без изменений ${s.clauses_kept}${s.errors.length ? "; " + s.errors.join("; ") : ""}\n`;
      if (r.warning) log.textContent += `  ${r.warning}\n`;
    } catch (err) { log.textContent += `  ошибка: ${err.message}\n`; }
  }
  load();
};
$("reindex").onclick = async () => {
  const log = $("upload-log");
  try { const s = await api("/api/admin/reindex", { method: "POST" });
    log.textContent += `Пересканировано: новых ${s.added}, изменённых ${s.updated}, удалённых ${s.removed}, без изменений ${s.unchanged}${s.errors.length ? "; ошибки: " + s.errors.join("; ") : ""}\n`; load(); }
  catch (err) { log.textContent += `ошибка: ${err.message}\n`; }
};
bindDropzone();
if (token) { $("token").value = ""; login(); }
