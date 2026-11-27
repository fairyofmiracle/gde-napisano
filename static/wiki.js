"use strict";

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c != null) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}

function displayAnswer(s) {
  s = String(s || "").replace(/^\s*(?:финальный\s+)?ответ\s*[.:]\s*/i, "").trim();
  const m = s.match(/^([\s«"'()]*)([\s\S]*)$/);
  if (m && m[2]) s = m[1] + m[2].charAt(0).toUpperCase() + m[2].slice(1);
  return s;
}

function formatDate(s) {
  if (!s) return "";
  const m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${m[3]}.${m[2]}.${m[1]}` : s;
}

function renderDocs(root, docs) {
  root.replaceChildren();
  if (!docs.length) {
    root.append(el("p", { class: "muted" }, "Пока нет проиндексированных регламентов."));
    return;
  }
  const tb = el("tbody");
  for (const d of docs) {
    tb.append(el("tr", {},
      el("td", {}, el("a", { href: `/doc/${encodeURIComponent(d.doc_id)}` }, d.title)),
      el("td", {}, d.box_name || "—"),
      el("td", {}, d.doc_date || "—"),
      el("td", {}, String(d.n_clauses))));
  }
  root.append(el("div", { class: "table-wrap" },
    el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Регламент"),
        el("th", {}, "Подразделение"),
        el("th", {}, "Дата"),
        el("th", {}, "Пунктов"))),
      tb)));
}

function renderFaq(root, items) {
  root.replaceChildren();
  if (!items.length) {
    root.append(el("p", { class: "muted" }, "Пока нет разъяснений службы качества."));
    return;
  }
  for (const f of items) {
    const id = "faq-" + f.id;
    const meta = [];
    if (f.escalation_id) meta.push("заявка ГН-" + f.escalation_id);
    const date = formatDate(f.created_at);
    if (date) meta.push(date);
    root.append(el("article", { class: "wiki-faq", id },
      el("h3", {}, el("a", { href: "#" + id, class: "wiki-anchor" }, f.question)),
      el("div", { class: "wiki-a" }, el("b", {}, "Ответ. "), displayAnswer(f.answer)),
      meta.length ? el("div", { class: "muted faq-meta" }, meta.join(" · ")) : null));
  }
}

(async () => {
  const docsEl = document.getElementById("wiki-docs");
  const faqEl = document.getElementById("wiki-faq");
  try {
    const r = await fetch("/api/wiki");
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `Ошибка ${r.status}`);
    renderDocs(docsEl, d.documents || []);
    renderFaq(faqEl, d.faq || []);
    const hash = location.hash;
    if (hash) {
      const t = document.getElementById(hash.slice(1));
      if (t) t.scrollIntoView({ block: "start" });
    }
  } catch (err) {
    docsEl.textContent = "Не получилось загрузить хранилище: " + err.message;
  }
})();
