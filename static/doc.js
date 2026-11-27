"use strict";
(async () => {
  const docId = decodeURIComponent(location.pathname.split("/").pop());
  const target = new URLSearchParams(location.search).get("p");
  const body = document.getElementById("doc-body");
  try {
    const r = await fetch(`/api/documents/${encodeURIComponent(docId)}/clauses`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `Ошибка ${r.status}`);
    document.getElementById("doc-title").textContent = d.title;
    document.title = `Где написано? — ${d.title}`;
    document.getElementById("doc-meta").textContent = [d.box_name, d.doc_date ? `файл от ${d.doc_date}` : ""].filter(Boolean).join(" · ");
    let section = null;
    for (const c of d.clauses) {
      if (c.section && c.section !== section) {
        section = c.section;
        const h = document.createElement("h3"); h.textContent = section; body.append(h);
      }
      const p = document.createElement("p");
      p.id = `p-${c.point_id}`;
      p.className = "doc-clause" + (c.point_id === target ? " hl" : "");
      if (c.clause_no) { const b = document.createElement("b"); b.textContent = c.clause_no + " "; p.append(b); }
      p.append(document.createTextNode(c.text));
      body.append(p);
    }
    const el = target && document.getElementById(`p-${target}`);
    if (el) el.scrollIntoView({ block: "center" });
  } catch (err) { document.getElementById("doc-title").textContent = err.message; }
})();
