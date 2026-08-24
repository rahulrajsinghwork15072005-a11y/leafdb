/* LeafDB Studio frontend - vanilla JS, no dependencies. */
"use strict";

const $ = (id) => document.getElementById(id);

// ---------- tabs ----------
document.querySelectorAll("#tabs button").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "tree") loadTree();
  };
});

async function api(path, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) }
    : {};
  const r = await fetch(path, opts);
  return r.json();
}

// ---------- schema sidebar ----------
let currentTables = [];

async function loadTables() {
  const data = await api("/api/tables");
  currentTables = data.tables;
  const box = $("tables");
  box.innerHTML = "";
  for (const t of data.tables) {
    const div = document.createElement("div");
    div.className = "tbl";
    const head = document.createElement("b");
    head.textContent = `${t.name} (${t.rows})`;
    head.onclick = () => {
      $("sql").value = `SELECT * FROM ${t.name}`;
      runQuery();
      switchTab("query");
    };
    div.appendChild(head);
    const cols = document.createElement("div");
    cols.className = "cols";
    for (const c of t.columns) {
      const line = document.createElement("div");
      line.innerHTML = `${c.name} <i>${c.type}</i>`;
      if (c.pk) line.innerHTML += '<span class="badge pk">PK</span>';
      if (c.indexed) line.innerHTML += '<span class="badge idx">idx</span>';
      line.onclick = () => { $("sql").value = `SELECT ${c.name} FROM ${t.name} WHERE ${c.name} = `;
                            $("sql").focus(); };
      cols.appendChild(line);
    }
    div.appendChild(cols);
    box.appendChild(div);
    const opt = document.createElement("option");
    opt.value = opt.textContent = t.name;
    $("tree-table").appendChild(opt);
  }
  if (!data.tables.length) box.innerHTML = '<div class="empty">no tables yet</div>';
}

function switchTab(name) {
  document.querySelectorAll("#tabs button").forEach(
    (b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach(
    (t) => t.classList.toggle("active", t.id === "tab-" + name));
}

// ---------- query runner ----------
async function runQuery() {
  const sql = $("sql").value.trim();
  if (!sql) return;
  $("timing").textContent = "running...";
  const data = await api("/api/query", { sql });
  const box = $("results");
  box.innerHTML = "";
  if (!data.ok) {
    box.innerHTML = `<div class="errbox">${escapeHtml(data.error)}</div>`;
    $("timing").textContent = "";
    return;
  }
  let total = 0;
  for (const r of data.results) {
    total += r.elapsed_ms || 0;
    const block = document.createElement("div");
    block.className = "resultblock";
    if (r.message) block.innerHTML += `<div class="msg">${escapeHtml(r.message)}</div>`;
    if (r.cols && r.rows.length) block.appendChild(grid(r.cols, r.rows));
    else if (r.cols) block.innerHTML += `<div class="muted">(0 rows)</div>`;
    box.appendChild(block);
  }
  $("timing").textContent = `${total.toFixed(1)} ms`;
  refreshChips();
  loadTables();
}

function grid(cols, rows) {
  const t = document.createElement("table");
  t.className = "grid";
  const numeric = cols.map((_, i) =>
    rows.every((r) => r[i] === null || typeof r[i] === "number"));
  const head = "<tr>" + cols.map((c, i) => `<th>${esc(c)}</th>`).join("") + "</tr>";
  const body = rows.map((r) =>
    "<tr>" + r.map((v, i) => {
      const cls = numeric[i] ? ' class="num"' : "";
      return `<td${cls}>${v === null ? '<span class="null">NULL</span>' : esc(String(v))}</td>`;
    }).join("") + "</tr>").join("");
  t.innerHTML = head + body;
  return t;
}

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
const esc = escapeHtml;

$("run").onclick = runQuery;
$("sql").addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") runQuery();
});

// ---------- stats chips ----------
async function refreshChips() {
  const st = await api("/api/stats");
  $("chip-pages").textContent = `pages ${st.pager.file_pages}`;
  $("chip-cache").textContent =
    `cache ${st.pager.hit_rate} (${st.pager.cache_hits}/${st.pager.cache_hits + st.pager.cache_misses})`;
  $("chip-wal").textContent = `wal ${st.wal_bytes}B`;
  $("chip-txn").textContent = st.in_transaction ? "in transaction" : "autocommit";
  $("chip-txn").style.color = st.in_transaction ? "var(--warn)" : "var(--dim)";
}

// ---------- B+ tree visualizer ----------
const NS = "http://www.w3.org/2000/svg";
let treeData = null;

$("tree-refresh").onclick = loadTree;
$("tree-table").onchange = loadTree;

async function loadTree() {
  const t = $("tree-table").value;
  if (!t) return;
  treeData = await api("/api/btree/" + encodeURIComponent(t));
  $("tree-table").value = treeData.table;
  const s = treeData.stats;
  $("tree-stats").textContent =
    `depth ${s.depth} | leaves ${s.leaves} | internal ${s.internal_nodes} | keys ${s.keys}`;
  drawTree();
}

function drawTree() {
  const svg = $("treesvg");
  svg.innerHTML = "";
  if (!treeData || !treeData.levels.length) return;
  const W = Math.max(900, svg.clientWidth || 900);
  const levels = treeData.levels;
  const H = 90 + levels.length * 130;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("height", H);

  // assign x positions: leaves spread evenly; parents centered on children
  const leafCount = levels[levels.length - 1].length;
  const slotW = W / Math.max(1, leafCount);
  const pos = {};   // page -> {x, y, node}
  levels.forEach((lvl, li) => {
    const y = 30 + li * 120;
    lvl.forEach((node, ni) => {
      let x;
      if (!node.children) {
        x = slotW * ni + slotW / 2;
      } else {
        const kids = node.children.map((c) => pos[c]).filter(Boolean);
        x = kids.length ? kids.reduce((a, k) => a + k.x, 0) / kids.length
                        : slotW * ni + slotW / 2;
      }
      pos[node.page] = { x, y, node };
    });
  });

  // edges parent->child
  levels.forEach((lvl, li) => {
    if (li + 1 >= levels.length) return;
    const py = 30 + li * 120;
    const cy = 30 + (li + 1) * 120;
    lvl.forEach((node) => {
      if (!node.children) return;
      node.children.forEach((c) => {
        const from = pos[node.page], to = pos[c];
        if (!from || !to) return;
        const path = document.createElementNS(NS, "path");
        path.setAttribute("class", "edge");
        path.setAttribute("d",
          `M${from.x},${py + 34} C${from.x},${(py + cy) / 2} ${to.x},${(py + cy) / 2} ${to.x},${cy}`);
        svg.appendChild(path);
      });
    });
  });

  // linked-leaf chain arrows
  const leaves = levels[levels.length - 1];
  const leafY = 30 + (levels.length - 1) * 120 + 17;
  for (let i = 0; i + 1 < leaves.length; ++i) {
    const a = pos[leaves[i].page], b = pos[leaves[i + 1].page];
    if (!a || !b) continue;
    const path = document.createElementNS(NS, "path");
    path.setAttribute("class", "chain");
    path.setAttribute("d",
      `M${a.x + boxW(a.node)},${leafY} C${a.x + 60},${leafY - 26} ${b.x - 60},${leafY - 26} ${b.x - boxW(b.node)},${leafY}`);
    svg.appendChild(path);
  }

  // nodes
  levels.forEach((lvl, li) => {
    const y = 30 + li * 120;
    lvl.forEach((node) => {
      const p = pos[node.page];
      const g = document.createElementNS(NS, "g");
      g.setAttribute("class", "gnode");
      g.appendChild(nodeBox(node, p.x, y));
      g.addEventListener("click", () => showDetail(node, g));
      svg.appendChild(g);
    });
  });

  function boxW(node) {
    if (node.kind === "internal") return Math.min(220, 24 + node.keys.length * 34);
    return Math.min(240, 40 + node.count * 22);
  }
}

function nodeBox(node, cx, y) {
  const w = node.kind === "internal"
    ? Math.min(220, 24 + node.keys.length * 34)
    : Math.min(240, 40 + node.count * 22);
  const h = 36;
  const g = document.createElementNS(NS, "g");
  const rect = document.createElementNS(NS, "rect");
  rect.setAttribute("x", cx - w / 2); rect.setAttribute("y", y);
  rect.setAttribute("width", w); rect.setAttribute("height", h);
  rect.setAttribute("rx", 5);
  rect.setAttribute("class", node.kind === "internal" ? "internalrect" : "leafrect");
  g.appendChild(rect);

  let label;
  if (node.kind === "internal") {
    label = node.keys.slice(0, 6).map((k) => String(k)).join(" | ")
          + (node.keys.length > 6 ? " ..." : "");
  } else {
    label = node.cells.length
      ? node.cells.slice(0, 5).map((c) => c.key).join(",")
            + (node.count > 5 ? ` +${node.count - 5}` : "")
      : "(empty)";
  }
  const text = document.createElementNS(NS, "text");
  text.setAttribute("x", cx); text.setAttribute("y", y + 22);
  text.setAttribute("text-anchor", "middle");
  text.textContent = label.length > 42 ? label.slice(0, 40) + ".." : label;
  g.appendChild(text);

  const tag = document.createElementNS(NS, "text");
  tag.setAttribute("x", cx - w / 2 + 4); tag.setAttribute("y", y + 12);
  tag.setAttribute("font-size", "9");
  tag.setAttribute("fill", "#7d8aa0");
  tag.textContent = `p${node.page}`;
  g.appendChild(tag);
  return g;
}

function showDetail(node, g) {
  document.querySelectorAll(".gnode.selected")
    .forEach((n) => n.classList.remove("selected"));
  g.classList.add("selected");
  const d = $("nodedetail");
  d.style.display = "block";
  let txt = `page ${node.page}\nkind: ${node.kind}\n`;
  if (node.kind === "internal") {
    txt += `separators: ${JSON.stringify(node.keys)}\nchildren: ${JSON.stringify(node.children)}`;
  } else {
    txt += `cells: ${node.count}\nnext leaf: ${node.next ?? "none"}\n`;
    txt += node.cells.slice(0, 20).map((c) => `  key ${c.key} (${c.size}B)`).join("\n");
    if (node.count > 20) txt += `\n  ... +${node.count - 20} more`;
  }
  d.textContent = txt;
}

// ---------- explain tab ----------
$("explain-run").onclick = async () => {
  const sql = $("explain-sql").value.trim();
  if (!sql.toLowerCase().startsWith("explain")) sql = "EXPLAIN " + sql;
  const data = await api("/api/query", { sql: sql.startsWith("EXPLAIN") ? sql : "EXPLAIN " + sql });
  const pipe = $("pipeline"), raw = $("plantext");
  pipe.innerHTML = ""; raw.textContent = "";
  if (!data.ok) { raw.textContent = data.error; return; }
  const lines = (data.results[0].rows || []).map((r) => r[0]);
  lines.forEach((line) => {
    const stage = document.createElement("div");
    stage.className = "pstage";
    stage.textContent = line.split("(")[0].trim();
    pipe.appendChild(stage);
    const arrow = document.createElement("span");
    arrow.className = "arrow"; arrow.textContent = "->";
    pipe.appendChild(arrow);
  });
  raw.textContent = lines.join("\n");
};

// ---------- boot ----------
loadTables();
refreshChips();
setInterval(refreshChips, 5000);
