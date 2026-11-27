"use strict";

function fill(node, ...children) {
  node.replaceChildren(...children.flat(Infinity).filter(c => c != null && c !== false));
}

(async () => {
  const docId = decodeURIComponent(location.pathname.split("/").pop());
  const target = new URLSearchParams(location.search).get("p");
  const body = document.getElementById("doc-body");
  const tocNav = document.getElementById("doc-toc-nav");
  try {
    const r = await fetch(`/api/documents/${encodeURIComponent(docId)}/clauses`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || `Ошибка ${r.status}`);
    document.getElementById("doc-title").textContent = d.title;
    document.title = `Где написано? — ${d.title}`;
    document.getElementById("doc-meta").textContent = [d.box_name, d.doc_date ? `файл от ${d.doc_date}` : ""].filter(Boolean).join(" · ");

    const toc = [];
    let section = null;
    for (const c of d.clauses || []) {
      if (c.section && c.section !== section) {
        section = c.section;
        const hid = "sec-" + String(section).toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-|-$/g, "");
        const h = document.createElement("h3");
        h.id = hid;
        h.textContent = section;
        body.append(h);
        const a = document.createElement("a");
        a.href = "#" + hid;
        a.className = "toc-section";
        a.textContent = section;
        toc.push(a);
      }
      const p = document.createElement("p");
      p.id = "p-" + c.point_id;
      p.className = "doc-clause" + (c.point_id === target ? " hl" : "");
      if (c.clause_no) {
        const b = document.createElement("b");
        b.textContent = c.clause_no + " ";
        p.append(b);
      }
      p.append(document.createTextNode(c.text || ""));
      body.append(p);
      if (c.clause_no || c.text) {
        const a = document.createElement("a");
        a.href = "#p-" + c.point_id;
        a.className = "toc-clause";
        a.textContent = (c.clause_no ? c.clause_no + " " : "") + String(c.text || "").slice(0, 72);
        if ((c.text || "").length > 72) a.textContent = a.textContent.replace(/\s+\S*$/, "") + "…";
        toc.push(a);
      }
    }
    if (tocNav) fill(tocNav, ...toc);

    const markOn = (id) => {
      if (!tocNav) return;
      tocNav.querySelectorAll("a").forEach(a => a.classList.toggle("is-on", a.getAttribute("href") === "#" + id));
    };
    tocNav && tocNav.addEventListener("click", (e) => {
      const a = e.target.closest("a");
      if (!a) return;
      const id = (a.getAttribute("href") || "").slice(1);
      const el = id && document.getElementById(id);
      if (!el) return;
      e.preventDefault();
      el.scrollIntoView({ block: "start" });
      history.replaceState(null, "", "#" + id);
      markOn(id);
    });

    const start = (target && document.getElementById("p-" + target)) || (location.hash && document.getElementById(location.hash.slice(1)));
    if (start) {
      start.scrollIntoView({ block: "center" });
      markOn(start.id);
    }
  } catch (err) {
    document.getElementById("doc-title").textContent = err.message;
  }
})();
