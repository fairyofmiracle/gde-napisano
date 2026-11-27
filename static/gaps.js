"use strict";

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else e.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}

function tickets(ids) {
  return (ids || []).map(id => "ГН-" + id).join(", ");
}

function clauseLink(c) {
  const label = c.ref || [c.doc_title, c.clause_no ? "п. " + c.clause_no : ""].filter(Boolean).join(", ");
  if (c.link) return el("a", { href: c.link }, label);
  return label;
}

function empty(root, text) {
  root.replaceChildren(el("p", { class: "muted gaps-empty" }, text));
}

function renderUnanswered(root, items) {
  root.replaceChildren();
  if (!items.length) return empty(root, "Пока нет заявок с причиной «в регламентах нет ответа».");
  for (const g of items) {
    const status = Object.entries(g.status || {}).map(([k, v]) => k + ": " + v).join(", ");
    root.append(el("div", { class: "gaps-item" },
      el("h3", {}, g.question),
      el("p", { class: "muted gaps-meta" },
        "Задавали: " + g.count + (g.division ? " · " + g.division : "")
        + (g.ticket_ids && g.ticket_ids.length ? " · заявки " + tickets(g.ticket_ids) : "")
        + (status ? " · " + status : "")
        + (g.last_at ? " · последняя " + g.last_at : ""))));
  }
}

function renderConflicts(root, items) {
  root.replaceChildren();
  if (!items.length) return empty(root, "Пока нет заявок из-за расхождения пунктов.");
  for (const c of items) {
    const list = el("ul", { class: "gaps-clauses" });
    for (const cl of c.clauses || []) {
      list.append(el("li", {}, clauseLink(cl), cl.quote ? " — «" + cl.quote + "»" : ""));
    }
    root.append(el("div", { class: "gaps-item" },
      el("h3", {}, "ГН-" + c.ticket_id + ": " + c.question),
      el("p", { class: "muted gaps-meta" },
        (c.division ? c.division + " · " : "") + (c.status || "") + (c.created_at ? " · " + c.created_at : "")),
      (c.clauses || []).length ? list : null));
  }
}

function renderHot(root, items) {
  root.replaceChildren();
  if (!items.length) return empty(root, "Пока нет пунктов, связанных с заявками.");
  const ol = el("ol", { class: "gaps-clauses" });
  for (const h of items) {
    const reasons = Object.entries(h.reasons || {}).map(([k, v]) => k + ": " + v).join(", ");
    ol.append(el("li", {},
      clauseLink(h),
      " — " + h.count + " заявок" + (reasons ? " (" + reasons + ")" : "")));
  }
  root.append(ol);
}

function renderMissing(root, items) {
  root.replaceChildren();
  if (!items.length) return empty(root, "В загруженных пунктах не нашлось ссылок на отсутствующие документы.");
  const kinds = { order: "приказ или распоряжение", regulation: "регламент", cancelled: "отмена или утрата силы" };
  for (const m of items) {
    const list = el("ul", { class: "gaps-sources" });
    for (const s of m.sources || []) {
      list.append(el("li", {},
        clauseLink({ ref: (s.doc_title || "") + (s.clause_no ? ", п. " + s.clause_no : ""), link: s.link }),
        s.snippet ? " — " + s.snippet : ""));
    }
    root.append(el("div", { class: "gaps-item" },
      el("h3", {}, m.mention),
      el("p", { class: "muted gaps-meta" }, kinds[m.kind] || m.kind || "ссылка вне базы"),
      list));
  }
}

async function main() {
  const r = await fetch("/api/gaps");
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    document.getElementById("gaps-totals").textContent = "Не получилось загрузить отчёт.";
    return;
  }
  const t = data.totals || {};
  document.getElementById("gaps-totals").textContent =
    "Вопросов без ответа: " + (t.unanswered_questions || 0)
    + " (" + (t.unanswered_groups || 0) + " групп). Противоречий: " + (t.conflicts || 0)
    + ". Ссылок на документы вне базы: " + (t.missing_refs || 0) + ".";
  renderUnanswered(document.getElementById("gaps-unanswered"), data.unanswered || []);
  renderConflicts(document.getElementById("gaps-conflicts"), data.conflicts || []);
  renderHot(document.getElementById("gaps-hot"), data.hot_clauses || []);
  renderMissing(document.getElementById("gaps-missing"), data.missing_refs || []);
  const scan = data.corpus_conflict_scan || {};
  const scanBox = document.getElementById("gaps-scan");
  scanBox.replaceChildren(el("p", { class: "muted gaps-scan" },
    scan.done ? "Полный проход по парам пунктов выполнен." : (scan.reason || "Полный проход не выполнялся.")));
}

main().catch(() => {
  document.getElementById("gaps-totals").textContent = "Не получилось загрузить отчёт.";
});
