"use strict";

let token = sessionStorage.getItem("adminToken") || "";
const $ = (id) => document.getElementById(id);
const REASON = { not_found: "нет ответа в регламентах", low_confidence: "неоднозначный ответ", conflict: "пункты противоречат", manual: "передал сотрудник" };
const STATUS = { open: "ждёт ответа", clarification: "эксперт уточняет", answered: "отвечено" };

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { ...(opts.headers || {}), "X-Admin-Token": token } });
  if (r.status === 401 || r.status === 403) throw Object.assign(new Error("Нужен токен администратора"), { auth: true });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `Ошибка ${r.status}`);
  return r.json();
}

let items = [];
let current = null;

function renderList() {
  const unread = items.filter(m => !m.read).length;
  $("mail-count").textContent = items.length ? `Писем: ${items.length}, непрочитанных: ${unread}` : "Писем пока нет";
  $("mail-list").replaceChildren(...items.map(m => el("li", { class: "mail-item" + (m.read ? "" : " unread") + (current === m.id ? " active" : "") },
    el("button", { type: "button", onclick: () => openMail(m.id) },
      el("span", { class: "mail-top" }, el("b", {}, m.ticket), el("span", { class: "muted" }, m.date)),
      el("span", { class: "mail-subject" }, m.subject.replace(/^\[ГН-\d+\]\s*/, "")),
      el("span", { class: "mail-meta muted" }, (m.read ? "прочитано" : "не прочитано") + " · " + (REASON[m.reason] || m.reason) + " · " + (STATUS[m.status] || m.status))))));
}

async function openMail(id) {
  current = id;
  const m = await api(`/api/operator/mail/${id}`);
  const it = items.find(x => x.id === id); if (it) it.read = true;
  renderList();
  const found = m.found.length
    ? el("ul", { class: "mail-found" }, m.found.map(f => el("li", {},
        f.link ? el("a", { href: f.link }, f.ref) : el("b", {}, f.ref),
        f.text ? el("div", { class: "muted" }, f.text.length > 300 ? f.text.slice(0, 300) + "…" : f.text) : null)))
    : el("p", { class: "muted" }, "Подходящих пунктов в регламентах не нашлось.");
  $("mail-view").replaceChildren(
    el("h3", {}, m.subject),
    el("dl", { class: "mail-head" },
      el("dt", {}, "От"), el("dd", {}, m.from),
      el("dt", {}, "Кому"), el("dd", {}, m.to),
      m.cc ? [el("dt", {}, "Копия"), el("dd", {}, m.cc)] : null,
      el("dt", {}, "Дата"), el("dd", {}, m.date),
      el("dt", {}, "Статус"), el("dd", {}, STATUS[m.status] || m.status)),
    el("p", {}, el("b", {}, "Вопрос: "), m.question),
    el("h4", {}, "Что нашлось в регламентах"), found,
    m.body ? el("details", { class: "mail-raw" }, el("summary", {}, "Текст письма целиком"), el("pre", {}, m.body)) : null,
    m.expert_answer ? el("p", {}, el("b", {}, "Ответ эксперта: "), m.expert_answer) : null,
    el("div", { class: "row mail-actions" },
      el("a", { class: "button-link", href: m.reply_url }, "Ответить / добавить разъяснение"),
      el("button", { type: "button", class: "none", onclick: async () => {
        await api(`/api/operator/mail/${id}/read?read=false`, { method: "POST" });
        const x = items.find(y => y.id === id); if (x) x.read = false; current = null; renderList();
        $("mail-view").replaceChildren(el("p", { class: "muted" }, "Письмо отмечено непрочитанным."));
      } }, "Отметить непрочитанным")));
}

async function load() {
  try {
    items = await api("/api/operator/mail");
    $("mail-auth").hidden = true; $("mail-box").hidden = false;
    renderList();
    const want = Number(new URLSearchParams(location.search).get("esc"));
    if (want && items.some(m => m.id === want)) openMail(want);
  } catch (e) {
    $("mail-auth").hidden = false; $("mail-box").hidden = true;
    $("mail-auth-msg").textContent = e.auth ? (token ? "Токен не подошёл" : "") : e.message;
  }
}

$("mail-auth").addEventListener("submit", (ev) => {
  ev.preventDefault();
  token = $("mail-token").value.trim();
  sessionStorage.setItem("adminToken", token);
  $("mail-token").value = "";
  load();
});

(function applyRole() {
  let role = "";
  try { role = (JSON.parse(localStorage.getItem("gnUser") || "null") || {}).role || ""; } catch (e) { role = ""; }
  if (role === "employee") {
    document.querySelector(".panel").replaceChildren(el("p", {}, "Этот раздел для службы качества и администратора. Войдите под другой ролью на главной странице."));
    return;
  }
  load();
})();
