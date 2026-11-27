"use strict";
const chat = document.getElementById("chat");
const form = document.getElementById("ask");
const q = document.getElementById("q");
const boxSel = document.getElementById("box");
// Диалог: идентификатор сессии (в ней запоминается подразделение) и выбор подразделения
const SESSION_KEY = "gnSession";
let sessionId = localStorage.getItem(SESSION_KEY);
if (!sessionId) {
  sessionId = "web-" + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
  localStorage.setItem(SESSION_KEY, sessionId);
}
let pendingDivision = null;

// Имитация корпоративного входа: имя и почта сотрудника уходят вместе с вопросом
const USER_KEY = "gnUser";
function currentUser() {
  try { return JSON.parse(localStorage.getItem(USER_KEY) || "null") || {}; } catch (e) { return {}; }
}
function refreshLogin() {
  const u = currentUser();
  const link = document.getElementById("login-link");
  const ROLES = { employee: "сотрудник", expert: "эксперт службы качества", admin: "администратор" };
  if (link) link.textContent = u.email ? `${u.name} (${ROLES[u.role] || ROLES.employee})` : "Войти";
  const qa = document.getElementById("qa-link"), adm = document.getElementById("admin-link");
  const gaps = document.getElementById("gaps-link");
  if (qa) qa.hidden = !(u.role === "expert" || u.role === "admin");
  if (adm) adm.hidden = u.role !== "admin";
  if (gaps) gaps.hidden = !(u.role === "expert" || u.role === "admin");
  const mail = document.getElementById("mail-link");
  if (mail) mail.hidden = !(u.role === "expert" || u.role === "admin");
}
(function initLogin() {
  const dlg = document.getElementById("login");
  const link = document.getElementById("login-link");
  if (!dlg || !link) return;
  link.addEventListener("click", (e) => {
    e.preventDefault();
    const u = currentUser();
    document.getElementById("login-name").value = u.name || "";
    document.getElementById("login-email").value = u.email || "";
    document.getElementById("login-role").value = u.role || "employee";
    dlg.showModal();
  });
  document.getElementById("login-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const name = document.getElementById("login-name").value.trim();
    const email = document.getElementById("login-email").value.trim();
    if (!name || !email) return;
    const role = document.getElementById("login-role").value;
    localStorage.setItem(USER_KEY, JSON.stringify({ name, email, role }));
    refreshLogin(); dlg.close();
  });
  document.getElementById("login-out").addEventListener("click", () => {
    localStorage.removeItem(USER_KEY); refreshLogin(); dlg.close();
  });
  refreshLogin();
})();

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

function nodes(...xs) {
  return xs.flat(Infinity).filter(c => c != null && c !== false);
}

function fill(node, ...children) {
  node.replaceChildren(...nodes(...children));
}

async function api(path, body) {
  const r = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `Ошибка ${r.status}`);
  return data;
}

function scrollDown() { chat.scrollTop = chat.scrollHeight; }

function svgIcon(paths) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "18");
  svg.setAttribute("height", "18");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.5");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("icon");
  for (const d of paths) {
    const node = document.createElementNS(NS, "path");
    node.setAttribute("d", d);
    svg.append(node);
  }
  return svg;
}

const ICON_UP = [
  "M7 11v10",
  "M7 21H4a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2h3",
  "M7 11l4.4-7.3A2.2 2.2 0 0 1 15.2 5L14 11h5.3a2 2 0 0 1 1.95 2.48l-1.7 6.7A2 2 0 0 1 17.6 22H7",
];
const ICON_DOWN = [
  "M17 13V3",
  "M17 3h3a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2h-3",
  "M17 13l-4.4 7.3A2.2 2.2 0 0 1 8.8 19L10 13H4.7a2 2 0 0 1-1.95-2.48l1.7-6.7A2 2 0 0 1 6.4 2H17",
];


function renderChips() {
  const wrap = document.getElementById("box-chips");
  if (!wrap) return;
  wrap.replaceChildren(...[...boxSel.options].map(opt =>
    el("button", {
      type: "button",
      class: "chip" + (boxSel.value === opt.value ? " is-on" : ""),
      "data-box": opt.value,
      onclick: () => {
        boxSel.value = opt.value;
        pendingDivision = opt.value || "auto";
        wrap.querySelectorAll(".chip").forEach(c => c.classList.toggle("is-on", c.getAttribute("data-box") === opt.value));
      },
    }, opt.textContent)
  ));
}

function clauseHref(c) {
  return (c && c.link) || (c && c.point_id ? `#cite-${c.point_id}` : "#");
}

function findCiteCard(origin, c) {
  const box = origin && origin.closest(".msg");
  if (!box || !c) return null;
  if (c.point_id) {
    const id = "cite-" + c.point_id;
    const sel = "#" + ((window.CSS && CSS.escape) ? CSS.escape(id) : id);
    const byId = box.querySelector(sel);
    if (byId) return byId;
  }
  if (c.n != null) return box.querySelector(`[data-cite-n="${c.n}"]`);
  return null;
}

function highlightCard(card) {
  if (!card) return;
  card.classList.add("is-hl");
  card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  setTimeout(() => card.classList.remove("is-hl"), 1800);
}

function onRefClick(e, c, mode) {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  const card = findCiteCard(e.currentTarget, c);
  if (mode === "cite" && card) { highlightCard(card); return; }
  if (card) highlightCard(card);
  if (c && c.point_id) openClause(c.point_id);
}

function refLink(c, label, mode) {
  return el("a", { href: clauseHref(c), class: "ref", onclick: (e) => onRefClick(e, c, mode) }, label);
}

function noteBlock(text) {
  if (!text) return null;
  const box = el("div", { class: "answer-note" });
  String(text).split("\n").forEach((line, i) => {
    if (i) box.append(document.createElement("br"));
    if (line) box.append(line);
  });
  return box;
}

function textWithRefs(text, citations) {
  const span = el("div", { class: "answer-text" });
  const byN = Object.fromEntries((citations || []).map(c => [c.n, c]));
  const parts = String(text || "").split(/(\[\d+\])/);
  for (const p of parts) {
    const m = p.match(/^\[(\d+)\]$/);
    if (m && byN[m[1]]) span.append(refLink(byN[m[1]], `[${m[1]}]`, "cite"));
    else if (!m) span.append(p);
  }
  return span;
}

function ownerBlock(owner) {
  if (!owner) return null;
  const who = [owner.full_name, owner.role].filter(Boolean).join(", ");
  return el("div", { class: "owner" }, `Ответственный (${owner.box_name}): ${who}`,
    owner.contact ? el("span", {}, " · ", owner.contact) : null,
    owner.is_example ? el("span", { class: "muted" }, " (пример)") : null);
}

function practiceBlock(items, title = "На практике") {
  if (!items || !items.length) return null;
  return el("div", { class: "practice" }, el("b", {}, title + ": "),
    el("ul", {}, items.map(p => el("li", {}, p.text))));
}

function practiceForm(answerId, pointId, container, prompt) {
  const ta = el("textarea", { rows: "2", maxlength: "4000", placeholder: "Как решили вопрос? Это поможет следующим коллегам" });
  const box = el("div", { class: "practice-form" }, el("div", {}, prompt), ta,
    el("button", { type: "button", onclick: async (e) => {
      if (ta.value.trim().length < 3) return;
      e.target.disabled = true;
      try { await api("/api/practice", { answer_id: answerId, text: ta.value, point_id: pointId || null });
        box.replaceWith(el("div", { class: "thanks" }, "Спасибо, сохранила как пример из практики.")); }
      catch (err) { e.target.disabled = false; alert(err.message); }
    } }, "Отправить"));
  container.append(box);
  scrollDown();
}

function modePlaque(mode) {
  const map = {
    confident: { class: "plaque plaque-ok", text: "Ответ по регламенту" },
    uncertain: { class: "plaque plaque-warn", text: "Варианты" },
    not_found: { class: "plaque plaque-muted", text: "Нет в документах" },
    refused: { class: "plaque plaque-muted", text: "Вопрос отклонён" },
    clarify: { class: "plaque plaque-warn", text: "Уточнение" },
    by_division: { class: "plaque plaque-warn", text: "По подразделениям" },
    faq: { class: "plaque plaque-ok", text: "Разъяснение службы качества" },
    offtopic: { class: "plaque plaque-muted", text: "Вне базы регламентов" },
  };
  const m = map[mode] || map.not_found;
  return el("div", { class: m.class }, m.text);
}

function sourceLine(c) {
  return el("div", { class: "source" },
    el("span", { class: "source-file" }, c.doc_title || c.ref),
    (c.label || c.clause_no) ? el("span", {}, "п. " + (c.label || c.clause_no)) : null,
    c.page ? el("span", {}, "с. " + c.page) : null,
    c.doc_date ? el("span", { class: "muted" }, "файл от " + c.doc_date) : null,
    c.link ? el("a", { href: c.link, class: "ref", target: "_blank", rel: "noopener" }, "открыть в документе") : null,
    c.point_id || c.link ? refLink(c, "показать пункт", "clause") : null);
}

function citationsBlock(citations) {
  if (!citations || !citations.length) return null;
  return el("div", { class: "sources" }, el("div", { class: "sources-title" }, "Где написано:"),
    citations.map(c => el("div", { class: "source-item", id: c.point_id ? `cite-${c.point_id}` : undefined, "data-cite-n": String(c.n), "data-point-id": c.point_id || "" },
      el("span", { class: "source-n" }, `[${c.n}]`),
      el("div", {}, sourceLine(c), c.quote ? el("blockquote", { class: "quote" }, `«${c.quote}»`) : null))));
}

function glossaryBlock(items) {
  if (!items || !items.length) return null;
  const parts = [];
  items.forEach((g, i) => {
    if (i) parts.push("; ");
    parts.push(el("abbr", { title: g.full }, g.abbr), " — ", g.full);
  });
  return el("div", { class: "glossary" }, el("b", {}, "Сокращения: "), ...parts);
}

function escalationBlock(esc) {
  if (!esc) return null;
  const how = esc.delivery === "smtp" ? "письмо отправлено" : "письмо сохранено в исходящих (почта не настроена)";
  return el("div", { class: "escalation" },
    el("b", {}, `Запрос в службу качества ГН-${esc.id}`),
    el("div", {}, `Кому: ${esc.to} · копия: ${esc.cc} · ${how}` + (esc.duplicate ? " · уже передан ранее" : "")));
}

function escalateButton(ans, container) {
  const btn = el("button", { type: "button", class: "none", onclick: async () => {
    btn.disabled = true;
    try {
      const r = await api("/api/escalate", { answer_id: ans.answer_id, session_id: sessionId });
      btn.replaceWith(...nodes(el("div", { class: "answer-note" }, r.text), escalationBlock(r.escalation)));
    } catch (err) { btn.disabled = false; alert(err.message); }
  } }, "Передать вопрос в службу качества");
  container.append(btn);
}

function clarifyBlock(ans) {
  const c = ans.clarify;
  const row = el("div", { class: "clarify" });
  c.options.forEach(o => row.append(el("button", { type: "button", class: o.id === "unknown" ? "none" : "pick", onclick: () => {
    row.querySelectorAll("button").forEach(b => b.disabled = true);
    ask(c.pending_question, o.id, o.label);
  } }, o.label)));
  return row;
}

function sectionBlock(sec) {
  const box = el("div", { class: `division-section mode-${sec.mode}` }, el("h4", {}, sec.division_name));
  if (sec.mode === "confident") {
    box.append(textWithRefs(sec.text, sec.citations));
    const cb = citationsBlock(sec.citations); if (cb) box.append(cb);
    if (sec.advice) box.append(el("div", { class: "advice" }, el("b", {}, "Что сделать: "), sec.advice));
  } else if (sec.mode === "uncertain") {
    box.append(el("div", { class: "answer-note" }, "Однозначного пункта нет, ближайшие по смыслу:"));
    sec.variants.forEach(v => box.append(el("div", { class: "variant" },
      el("div", { class: "cite" }, v.link ? el("a", { href: v.link, target: "_blank", rel: "noopener" }, v.ref) : v.ref),
      el("div", { class: "snippet" }, v.text.length > 300 ? v.text.slice(0, 300) + "…" : v.text))));
  } else {
    box.append(el("div", { class: "answer-note" }, "В регламенте этого подразделения ответа нет."));
  }
  const w = warningsBlock(sec.warnings); if (w) box.append(w);
  return box;
}

function feedbackRow(ans, container) {
  const row = el("div", { class: "feedback" }, el("span", {}, "Ответ помог?"));
  const up = el("button", { type: "button", class: "thumb", "aria-label": "Помогло", "data-helpful": "true" }, svgIcon(ICON_UP));
  const down = el("button", { type: "button", class: "thumb", "aria-label": "Не помогло", "data-helpful": "false" }, svgIcon(ICON_DOWN));
  const send = async (helpful) => {
    row.querySelectorAll("button").forEach(b => b.disabled = true);
    try {
      await api("/api/feedback", { answer_id: ans.answer_id, helpful });
      up.classList.toggle("is-selected", helpful);
      down.classList.toggle("is-selected", !helpful);
      if (!row.querySelector(".thanks")) row.append(el("span", { class: "thanks" }, helpful ? "Спасибо!" : "Понятно, учту."));
      if (!helpful) practiceForm(ans.answer_id, null, container, "Подскажите, как в итоге решили?");
    } catch (err) {
      row.querySelectorAll("button").forEach(b => b.disabled = false);
      alert(err.message);
    }
  };
  up.addEventListener("click", () => send(true));
  down.addEventListener("click", () => send(false));
  row.append(up, down);
  return row;
}

function variantsBlock(ans, container) {
  const list = el("div", { class: "variants" });
  ans.variants.forEach((v, i) => {
    const card = el("div", { class: "variant" },
      el("div", { class: "cite" }, refLink(v, `${i + 1}. ${v.ref}`, "clause")),
      el("div", { class: "snippet" }, v.text.length > 360 ? v.text.slice(0, 360) + "…" : v.text),
      practiceBlock(v.practice),
      el("button", { type: "button", class: "pick", onclick: async () => {
        list.querySelectorAll("button").forEach(b => b.disabled = true);
        try { await api("/api/feedback", { answer_id: ans.answer_id, helpful: true, point_id: v.point_id });
          card.classList.add("chosen");
          list.after(el("div", { class: "thanks" }, "Отлично, запомню, что для такого вопроса подходит этот пункт.")); }
        catch (err) { alert(err.message); }
      } }, "Да, это оно"));
    list.append(card);
  });
  list.append(el("button", { type: "button", class: "none", onclick: async () => {
    list.querySelectorAll("button").forEach(b => b.disabled = true);
    try {
      for (const v of ans.variants) await api("/api/feedback", { answer_id: ans.answer_id, helpful: false, point_id: v.point_id });
      practiceForm(ans.answer_id, null, container, "Ничего не подошло. Если знаете, как решается вопрос, напишите:");
    } catch (err) { alert(err.message); }
  } }, "Нет, ничего не подошло"));
  return list;
}

const STEP_MARK = { ok: "готово", warn: "внимание", fail: "сбой", skip: "пропущено" };

// Ход работы агента: компактный сворачиваемый список шагов
function stepsBlock(steps, iterations) {
  if (!steps || !steps.length) return null;
  const total = steps.reduce((a, s) => a + (s.ms || 0), 0);
  const summary = el("summary", { class: "steps-summary" },
    el("span", {}, "Ход работы"),
    el("span", { class: "steps-meta" }, `${steps.length} шагов` + (iterations ? `, повторный поиск: ${iterations}` : "") + (total ? `, ${(total / 1000).toFixed(1)} с` : "")));
  const list = el("ol", { class: "steps-list" }, steps.map(s => el("li", { class: `step step-${s.status}` },
    el("span", { class: "step-dot", "aria-hidden": "true" }),
    el("div", { class: "step-body" },
      el("div", { class: "step-head" }, el("b", {}, s.title),
        el("span", { class: "step-status" }, STEP_MARK[s.status] || s.status),
        s.ms != null ? el("span", { class: "step-ms" }, `${s.ms} мс`) : null),
      el("div", { class: "step-detail" }, s.detail)))));
  return el("details", { class: "steps" }, summary, list);
}

// Ход работы как расследование: улики, сверка, вердикт. Подробные шаги — внутри, свёрнуты.
const ROLE_LABEL = { cited: "в ответе", conflict: "расходится", variant: "вариант", candidate: "найден" };

function investigationBlock(ans) {
  const inv = ans.investigation;
  if (!inv) return stepsBlock(ans.steps, ans.iterations);
  const steps = ans.steps || [];
  const total = steps.reduce((a, s) => a + (s.ms || 0), 0);
  const v = inv.verdict || {};
  const summary = el("summary", { class: "steps-summary" },
    el("span", {}, "Ход работы"),
    el("span", { class: "steps-meta" }, "улики, сверка, вердикт" + (v.title ? `: ${v.title.toLowerCase()}` : "")));
  const ev = inv.evidence || { items: [] };
  const evList = el("ul", { class: "inv-list" }, (ev.items || []).map(i => el("li", { class: `inv-ev inv-${i.role}` },
    i.link ? refLink(i, i.ref, "clause") : el("span", {}, i.ref),
    el("span", { class: "inv-tag" }, ROLE_LABEL[i.role] || i.role),
    i.score != null ? el("span", { class: "step-ms" }, i.score.toFixed(2)) : null)));
  const ch = inv.check || { items: [] };
  const chList = el("ul", { class: "inv-list" }, (ch.items || []).map(i => el("li", { class: `step step-${i.status}` },
    el("span", { class: "step-dot", "aria-hidden": "true" }),
    el("div", { class: "step-body" },
      el("div", { class: "step-head" }, el("b", {}, i.label),
        el("span", { class: "step-status" }, STEP_MARK[i.status] || i.status)),
      el("div", { class: "step-detail" }, i.detail)))));
  const vd = el("div", { class: `inv-verdict inv-v-${v.kind || ""}` },
    el("div", { class: "step-head" }, el("b", {}, v.title || ""),
      v.confidence_label ? el("span", { class: "step-status" }, `уверенность: ${v.confidence_label}`) : null),
    v.why ? el("div", { class: "step-detail" }, v.why) : null);
  const stage = (n, title, note, body) => el("section", { class: "inv-stage" },
    el("div", { class: "inv-title" }, el("span", { class: "inv-n" }, n), el("b", {}, title),
      note ? el("span", { class: "steps-meta" }, note) : null), body);
  const parts = [
    stage("1", "Улики", ev.summary, ev.items && ev.items.length ? evList : null),
    stage("2", "Сверка", null, ch.items && ch.items.length ? chList : el("div", { class: "step-detail" }, ch.summary)),
    stage("3", "Вердикт", null, vd),
  ];
  const raw = stepsBlock(steps, ans.iterations);
  if (raw) {
    raw.classList.add("steps-raw");
    raw.querySelector(".steps-summary > span").textContent = "Все шаги";
  }
  return el("details", { class: "steps investigation" }, summary, el("div", { class: "inv-body" }, ...parts, raw));
}

function canSimplify(ans) {
  return !!(ans && (ans.can_simplify || (ans.mode === "confident" && !ans.escalation
    && (ans.citations || []).length && ans.answer_kind !== "quote_only")));
}

function simplifyRow(ans) {
  if (!canSimplify(ans)) return null;
  const original = ans.text;
  const row = el("div", { class: "simplify-row" });
  const note = el("div", { class: "answer-note simplify-note", hidden: true }, "Тот же ответ, простыми словами.");
  const btn = el("button", { type: "button", class: "none" }, "Не понял");
  let state = "orig";
  let simpleText = null;
  function setText(text) {
    const box = row.closest(".msg");
    const current = box && box.querySelector(".answer-text");
    if (current) current.replaceWith(textWithRefs(text, ans.citations));
  }
  btn.addEventListener("click", async () => {
    if (state === "simple") {
      setText(original);
      note.hidden = true;
      btn.textContent = "Не понял";
      state = "orig";
      return;
    }
    if (simpleText == null) {
      btn.disabled = true;
      const prev = btn.textContent;
      btn.textContent = "Пересказываю…";
      try {
        const r = await api("/api/simplify", {
          answer_id: ans.answer_id,
          question: ans.question,
          text: ans.text,
          citations: ans.citations,
          glossary: ans.glossary,
          advice: ans.advice,
          mode: ans.mode,
        });
        simpleText = r.text || ans.text;
      } catch (err) {
        btn.textContent = prev;
        btn.disabled = false;
        alert(err.message);
        return;
      }
      btn.disabled = false;
    }
    setText(simpleText);
    note.hidden = false;
    btn.textContent = "Исходная формулировка";
    state = "simple";
  });
  row.append(note, btn);
  return row;
}

function warningsBlock(items) {
  if (!items || !items.length) return null;
  return el("div", { class: "warnings" }, items.map(w => el("div", { class: "warning" }, w)));
}

function renderAnswer(ans) {
  const box = el("div", { class: `msg bot mode-${ans.mode}` });
  box.append(modePlaque(ans.mode));
  if (ans.division && ans.division.name && ans.mode !== "by_division") box.append(el("div", { class: "muted division-line" }, "По регламенту: " + ans.division.name));
  if (ans.mode === "confident") {
    if (ans.notice) box.append(noteBlock(ans.notice));
    if (ans.conflict && ans.answer_check === "internal_conflict") box.append(el("div", { class: "conflict-flag" }, "Расхождение внутри регламента. Ниже оба пункта."));
    if (ans.answer_kind === "quote_only") box.append(el("div", { class: "answer-note" }, ans.text));
    else box.append(textWithRefs(ans.text, ans.citations));
    const cb = citationsBlock(ans.citations); if (cb) box.append(cb);
    const warn = warningsBlock(ans.warnings); if (warn) box.append(warn);
    if (ans.advice) box.append(el("div", { class: "advice" }, el("b", {}, "Что сделать: "), ans.advice));
    const practice = practiceBlock(ans.practice); if (practice) box.append(practice);
    const owner = ownerBlock(ans.owner); if (owner) box.append(owner);
    const g = glossaryBlock(ans.glossary); if (g) box.append(g);
    const e = escalationBlock(ans.escalation); if (e) box.append(e);
    const simp = simplifyRow(ans); if (simp) box.append(simp);
    box.append(feedbackRow(ans, box));
  } else if (ans.mode === "uncertain") {
    box.append(el("div", { class: "answer-text" }, ans.text));
    box.append(variantsBlock(ans, box));
    const warn = warningsBlock(ans.warnings); if (warn) box.append(warn);
    const ownerU = ownerBlock(ans.owner); if (ownerU) box.append(ownerU);
    const e = escalationBlock(ans.escalation); if (e) box.append(e); else escalateButton(ans, box);
  } else if (ans.mode === "clarify") {
    box.append(el("div", { class: "answer-text" }, ans.text));
    box.append(clarifyBlock(ans));
  } else if (ans.mode === "by_division") {
    if (ans.notice) box.append(noteBlock(ans.notice));
    box.append(el("div", { class: "answer-text" }, ans.text));
    ans.sections.forEach(sec => box.append(sectionBlock(sec)));
    const g = glossaryBlock(ans.glossary); if (g) box.append(g);
    const e = escalationBlock(ans.escalation); if (e) box.append(e); else escalateButton(ans, box);
    box.append(feedbackRow(ans, box));
  } else if (ans.mode === "faq") {
    box.append(el("div", { class: "answer-text" }, ans.text));
    const f = ans.faq;
    box.append(el("div", { class: "faq-meta muted" }, `${f.label} от ${f.created_at}` + (f.escalation_id ? ` по запросу ГН-${f.escalation_id}` : "")
      + (f.doc_title ? ` · связано с: ${f.doc_title}${f.clause_no ? ", п. " + f.clause_no : ""}` : "") + ". В самих регламентах этого ответа нет."));
    const warn = warningsBlock(ans.warnings); if (warn) box.append(warn);
    box.append(feedbackRow(ans, box));
  } else if (ans.mode === "refused" || ans.mode === "offtopic") {
    box.append(el("div", { class: "answer-text" }, ans.text));
  } else {
    box.append(el("div", { class: "answer-text" }, ans.text));
    const warn = warningsBlock(ans.warnings); if (warn) box.append(warn);
    const e = escalationBlock(ans.escalation); if (e) box.append(e);
    const practiceN = practiceBlock(ans.practice, "Похожий вопрос уже решали так"); if (practiceN) box.append(practiceN);
    const ownerN = ownerBlock(ans.owner); if (ownerN) box.append(ownerN);
    if (!ans.escalation) practiceForm(ans.answer_id, null, box, "Знаете ответ? Напишите, как решается вопрос, — сохраню как пример из практики.");
  }
  const steps = investigationBlock(ans);
  if (steps) box.append(steps);
  chat.append(box);
  scrollDown();
}

async function openClause(pointId) {
  const dlg = document.getElementById("clause");
  const body = document.getElementById("clause-body");
  body.textContent = "Загружаю…";
  dlg.showModal();
  try {
    const c = await api(`/api/clauses/${encodeURIComponent(pointId)}`);
    fill(body,
      el("h2", {}, c.ref), c.section ? el("div", { class: "muted" }, c.section) : null,
      el("div", { class: "context" }, (c.context || []).map(x => el("p", { class: x.point_id === pointId ? "hl" : "" },
        (x.label || x.clause_no) ? el("b", {}, (x.label || x.clause_no) + " ") : null, x.text))),
      practiceBlock(c.practice, "Практика по этому пункту"));
  } catch (err) { body.textContent = err.message; }
}

// Живой ответ: сразу показываем шаг, найденные пункты и черновик текста, потом заменяем итоговым ответом
function liveAnswer(wait) {
  const status = el("div", { class: "live-status" }, "Ищу в документах");
  const evidence = el("div", { class: "live-evidence" });
  const draft = el("div", { class: "answer-text live-draft" });
  const groups = {};
  const drafts = {};
  wait.classList.remove("wait");
  wait.classList.add("live");
  fill(wait, status, evidence, draft);
  function group(scope) {
    const key = scope || "";
    if (!groups[key]) {
      const g = el("div", { class: "live-group" });
      if (key) g.append(el("div", { class: "live-scope" }, key));
      const list = el("div", { class: "live-items" });
      const text = el("div", { class: "answer-text live-draft" });
      g.append(list, text);
      evidence.append(g);
      groups[key] = { list, text };
    }
    return groups[key];
  }
  return {
    status(text) { status.textContent = text; },
    evidence(ev) {
      const g = group(ev.scope);
      fill(g.list, el("div", { class: "sources-title" }, ev.items && ev.items.length ? "Нашла пункты:" : "Подходящих пунктов пока нет"),
        (ev.items || []).map(c => el("div", { class: "source-item live-item" },
          el("div", {}, sourceLine(c), c.quote ? el("blockquote", { class: "quote" }, `«${c.quote}»`) : null))));
      scrollDown();
    },
    draft(ev) {
      const target = ev.scope ? group(ev.scope).text : draft;
      drafts[ev.scope || ""] = ev.text;
      target.textContent = ev.text;
      scrollDown();
    },
  };
}

async function askStream(body, wait) {
  const r = await fetch("/api/chat/stream", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!r.ok || !r.body || !r.body.getReader) {
    const data = await r.json().catch(() => ({}));
    const err = new Error(data.detail || `Ошибка ${r.status}`);
    err.fallback = r.status === 404 || r.status === 405 || !r.body;
    if (r.status === 429) err.fallback = false;
    throw err;
  }
  const live = liveAnswer(wait);
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let final = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (!line) continue;
      let ev;
      try { ev = JSON.parse(line); } catch (_) { continue; }
      if (ev.type === "status") live.status(ev.scope ? `${ev.scope}: ${ev.text}` : ev.text);
      else if (ev.type === "evidence") live.evidence(ev);
      else if (ev.type === "draft") live.draft(ev);
      else if (ev.type === "final") final = ev.answer;
      else if (ev.type === "error") throw new Error(ev.detail || "Не получилось собрать ответ");
    }
  }
  if (!final) throw new Error("Ответ прервался, попробуйте ещё раз");
  return final;
}

async function ask(question, division = null, divisionLabel = null) {
  chat.append(el("div", { class: "msg user" }, divisionLabel ? `${divisionLabel}` : question));
  const wait = el("div", { class: "msg bot wait" }, "Ищу в документах…");
  chat.append(wait); scrollDown();
  form.querySelector("button").disabled = true;
  const div = division || pendingDivision;
  pendingDivision = null;
  try {
    const u = currentUser();
    const body = { question, session_id: sessionId, division: div };
    if (u.email) { body.user_email = u.email; body.user_name = u.name || ""; }
    let ans;
    try {
      ans = await askStream(body, wait);
    } catch (err) {
      if (!err.fallback) throw err;
      ans = await api("/api/chat", body);
    }
    wait.remove();
    renderAnswer(ans);
  } catch (err) {
    fill(wait, "Не получилось: " + err.message);
    wait.className = "msg bot wait";
  } finally {
    form.querySelector("button").disabled = false;
    q.focus();
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = q.value.trim();
  if (question.length < 2) return;
  q.value = "";
  ask(question);
});

q.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});

api("/api/boxes").then(list => {
  list.forEach(b => boxSel.append(el("option", { value: b.box }, b.name)));
  renderChips();
}).catch(() => { renderChips(); });

api("/api/health").then(h => {
  if (h.demo_ui_only) {
    const slot = document.querySelector(".top-badges") || document.querySelector(".top");
    slot.append(el("span", { class: "demo-badge" }, "демо-режим: ответы-заглушки"));
  }
  const tg = document.getElementById("tg-link");
  if (tg && h.telegram_url) {
    tg.href = h.telegram_url;
    tg.hidden = false;
  }
}).catch(() => {});

renderChips();
