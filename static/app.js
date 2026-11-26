"use strict";
const chat = document.getElementById("chat");
const form = document.getElementById("ask");
const q = document.getElementById("q");
const boxSel = document.getElementById("box");

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
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
        wrap.querySelectorAll(".chip").forEach(c => c.classList.toggle("is-on", c.getAttribute("data-box") === opt.value));
      },
    }, opt.textContent)
  ));
}

function textWithRefs(text, citations) {
  const span = el("div", { class: "answer-text" });
  const byN = Object.fromEntries((citations || []).map(c => [c.n, c]));
  const parts = String(text || "").split(/(\[\d+\])/);
  for (const p of parts) {
    const m = p.match(/^\[(\d+)\]$/);
    if (m && byN[m[1]]) span.append(el("a", { href: "#", class: "ref", onclick: (e) => { e.preventDefault(); openClause(byN[m[1]].point_id); } }, `[${m[1]}]`));
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
    confident: { class: "plaque plaque-ok", text: "Уверенный ответ" },
    uncertain: { class: "plaque plaque-warn", text: "Варианты" },
    not_found: { class: "plaque plaque-muted", text: "Нет в документах" },
    refused: { class: "plaque plaque-muted", text: "Вопрос отклонён" },
  };
  const m = map[mode] || map.not_found;
  return el("div", { class: m.class }, m.text);
}

function sourceLine(c) {
  return el("div", { class: "source" },
    el("span", { class: "source-file" }, c.doc_title || c.ref),
    c.clause_no ? el("span", {}, "п. " + c.clause_no) : null,
    c.page ? el("span", {}, "с. " + c.page) : null,
    c.doc_date ? el("span", { class: "muted" }, "файл от " + c.doc_date) : null,
    el("a", { href: "#", class: "ref", onclick: (e) => { e.preventDefault(); openClause(c.point_id); } }, "открыть пункт"));
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
      el("div", { class: "cite" }, el("a", { href: "#", onclick: (e) => { e.preventDefault(); openClause(v.point_id); } }, `${i + 1}. ${v.ref}`)),
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

function warningsBlock(items) {
  if (!items || !items.length) return null;
  return el("div", { class: "warnings" }, items.map(w => el("div", { class: "warning" }, w)));
}

function renderAnswer(ans) {
  const box = el("div", { class: `msg bot mode-${ans.mode}` });
  box.append(modePlaque(ans.mode));
  if (ans.mode === "confident") {
    if (ans.answer_kind === "quote_only") box.append(el("div", { class: "answer-note" }, ans.text));
    for (const c of ans.citations) {
      box.append(el("blockquote", { class: "quote" }, `«${c.quote}»`));
      box.append(sourceLine(c));
    }
    if (ans.answer_kind !== "quote_only") box.append(textWithRefs(ans.text, ans.citations));
    const warn = warningsBlock(ans.warnings);
    if (warn) box.append(warn);
    if (ans.advice) box.append(el("div", { class: "advice" }, el("b", {}, "Что сделать: "), ans.advice));
    const practice = practiceBlock(ans.practice);
    if (practice) box.append(practice);
    const owner = ownerBlock(ans.owner);
    if (owner) box.append(owner);
    box.append(feedbackRow(ans, box));
  } else if (ans.mode === "uncertain") {
    box.append(el("div", { class: "answer-text" }, ans.text));
    box.append(variantsBlock(ans, box));
    const ownerU = ownerBlock(ans.owner);
    if (ownerU) box.append(ownerU);
  } else if (ans.mode === "refused") {
    box.append(el("div", { class: "answer-text" }, ans.text));
  } else {
    box.append(el("div", { class: "answer-text" }, ans.text));
    const practiceN = practiceBlock(ans.practice, "Похожий вопрос уже решали так");
    if (practiceN) box.append(practiceN);
    const ownerN = ownerBlock(ans.owner);
    if (ownerN) box.append(ownerN);
    practiceForm(ans.answer_id, null, box, "Знаете ответ? Напишите, как решается вопрос, — сохраню как пример из практики.");
  }
  const steps = stepsBlock(ans.steps, ans.iterations);
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
    body.replaceChildren(
      el("h2", {}, c.ref), c.section ? el("div", { class: "muted" }, c.section) : null,
      el("div", { class: "context" }, c.context.map(x => el("p", { class: x.point_id === pointId ? "hl" : "" },
        x.clause_no ? el("b", {}, x.clause_no + " ") : null, x.text))),
      practiceBlock(c.practice, "Практика по этому пункту"));
  } catch (err) { body.textContent = err.message; }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = q.value.trim();
  if (question.length < 2) return;
  chat.append(el("div", { class: "msg user" }, question));
  q.value = "";
  const wait = el("div", { class: "msg bot wait" }, "Ищу в документах…");
  chat.append(wait); scrollDown();
  form.querySelector("button").disabled = true;
  try {
    const ans = await api("/api/chat", { question, boxes: boxSel.value ? [boxSel.value] : null });
    wait.remove();
    renderAnswer(ans);
  } catch (err) {
    wait.textContent = "Не получилось: " + err.message;
  } finally {
    form.querySelector("button").disabled = false;
    q.focus();
  }
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
}).catch(() => {});

renderChips();
