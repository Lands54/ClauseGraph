const byId = id => document.getElementById(id);
const project = byId("project");
const history = byId("history");
const selectAll = byId("select-all");
const snapshot = byId("snapshot");
const snapshotTopK = byId("snapshot-top-k");
const bm25TopK = byId("bm25-top-k");
const importForm = byId("import-form");
const createForm = byId("create-form");
const dialog = byId("create-dialog");
const selected = new Set();
let files = [];
let snapshots = [];
let busy = false;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

async function request(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail;
    throw new Error(typeof detail === "string" ? detail : `请求失败（HTTP ${response.status}）`);
  }
  return body;
}

function notice(message = "", error = false) {
  const element = byId("notice");
  element.hidden = !message;
  element.textContent = message;
  element.classList.toggle("error", error);
}

function syncControls() {
  const active = files.filter(file => file.active);
  const count = active.filter(file => selected.has(file.id)).length;
  byId("selection").textContent = `已选 ${count} 个版本`;
  byId("import-fields").disabled = busy || !project.value;
  byId("open-create").disabled = busy;
  project.disabled = busy;
  history.disabled = busy || !project.value;
  selectAll.disabled = busy || !active.length;
  selectAll.checked = Boolean(active.length && count === active.length);
  selectAll.indeterminate = count > 0 && count < active.length;
  snapshot.disabled = busy || !count;
  snapshotTopK.disabled = busy || !project.value;
  for (const input of byId("files").querySelectorAll("input")) {
    input.disabled = busy || input.dataset.active !== "true";
  }
}

function renderFiles() {
  const tbody = byId("files");
  tbody.replaceChildren();
  if (!files.length) {
    const cell = node("td", project.value ? "暂无文件，导入后可选择版本。" : "请选择或创建项目。", "empty");
    cell.colSpan = 5;
    tbody.append(node("tr"));
    tbody.lastChild.append(cell);
  }
  for (const file of files) {
    const row = node("tr");
    const checkbox = node("input");
    checkbox.type = "checkbox";
    checkbox.dataset.active = String(file.active);
    checkbox.checked = selected.has(file.id);
    checkbox.setAttribute("aria-label", `选择 ${file.document_id} ${file.id.slice(0, 12)}`);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selected.add(file.id);
      else selected.delete(file.id);
      syncControls();
    });
    const pick = node("td");
    pick.append(checkbox);
    const name = node("td", file.document_id);
    const version = node("small", file.id.slice(0, 12));
    version.title = file.id;
    name.append(version);
    const status = node("td");
    status.append(node("span", file.active ? "活动" : "历史", file.active ? "badge" : "badge old"));
    row.append(pick, name, node("td", file.document_type || "—"),
      node("td", { ready: "已完成", skipped: "已跳过" }[file.route3_status] || "—"), status);
    tbody.append(row);
  }
  syncControls();
}

function queryFor(id, kind, topK = null) {
  const snapshot = `MATCH (s:ClauseGraphSnapshot {id:${JSON.stringify(id)}})`;
  if (kind === "tree") {
    return `${snapshot}
MATCH (s)-[:USES_DOCUMENT]->(v:ClauseGraphDocumentVersion)
WITH DISTINCT v.id AS version
MATCH (c:VersionClause {version_id:version})
OPTIONAL MATCH (c)-[r:CONTAINS|HAS_BLOCK]->(n)
OPTIONAL MATCH (n:Table)-[rr:HAS_ROW]->(row:TableRow)
RETURN c,r,n,rr,row`;
  }
  const relationship = {
    route1: "SIMILAR_TO",
    route2: "CLASSIFIED_AS|TYPE_RELATION",
    route3: "MENTIONS|CONTENT_RELATION",
  }[kind];
  const rank = kind === "route1" && topK !== null ? ` AND r.rank <= ${topK}` : "";
  return `${snapshot}
MATCH (s)-[:HAS_NODE]->(a:ClauseGraphNode)-[r:${relationship}]->(b:ClauseGraphNode)
WHERE b.snapshot_id = s.id${rank}
RETURN a,r,b`;
}

function renderQuery(query, copy) {
  const { snapshotId, kind } = query.dataset;
  const topK = bm25TopK.value === "" ? null : bm25TopK.valueAsNumber;
  const invalid = kind === "route1" && (!bm25TopK.validity.valid ||
    (topK !== null && !Number.isSafeInteger(topK)));
  query.textContent = invalid ? "请输入非负整数，或留空显示全部 BM25 关系。" : queryFor(snapshotId, kind, topK);
  query.hidden = false;
  copy.disabled = invalid;
}

bm25TopK.addEventListener("input", () => {
  for (const query of byId("snapshots").querySelectorAll('pre[data-kind="route1"]:not([hidden])')) {
    renderQuery(query, query.nextElementSibling);
  }
});

function renderSnapshots() {
  const list = byId("snapshots");
  list.replaceChildren();
  if (!snapshots.length) list.append(node("p", "暂无快照。", "empty"));
  for (const item of snapshots) {
    const row = node("div", undefined, "snapshot-row");
    const description = node("div");
    description.append(node("strong", item.documents.join(" · ") || "图谱快照"), node("br"),
      node("code", item.id.slice(0, 16)), node("small", item.created_at ? `  ·  ${new Date(item.created_at).toLocaleString()}` : "", "subtle"));
    const actions = node("div", undefined, "snapshot-actions");
    const query = node("pre", undefined, "query");
    query.dataset.snapshotId = item.id;
    query.hidden = true;
    const copy = node("button", "复制查询", "secondary");
    for (const [label, kind] of [["条款树", "tree"], ["1 路 · BM25", "route1"], ["2 路 · 本体", "route2"], ["3 路 · 内容", "route3"]]) {
      const button = node("button", label, "secondary");
      button.type = "button";
      button.addEventListener("click", () => {
        query.dataset.kind = kind;
        renderQuery(query, copy);
      });
      actions.append(button);
    }
    copy.type = "button";
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(query.textContent);
        notice("已复制查询，可粘贴到 Neo4j Browser 运行。");
      } catch {
        notice("请手动选择并复制下方查询。");
      }
    });
    const style = node("a", "下载配色");
    style.href = `/api/snapshots/${encodeURIComponent(item.id)}/style.grass`;
    style.title = "在 Neo4j Browser 中运行 :style 后上传此文件";
    actions.append(style);
    const queryPanel = node("div", undefined, "query-panel");
    queryPanel.append(query, copy);
    row.append(description, actions);
    list.append(row, queryPanel);
  }
}

const projectPath = () => `/api/projects/${encodeURIComponent(project.value)}`;

async function loadProjects(preferred = project.value) {
  const projects = await request("/api/projects");
  project.replaceChildren(new Option("选择项目", ""), ...projects.map(id => new Option(id, id)));
  project.value = projects.includes(preferred) ? preferred : projects[0] || "";
}

async function refresh() {
  if (project.value) {
    [files, snapshots] = await Promise.all([
      request(`${projectPath()}/files?history=${history.checked}`),
      request(`${projectPath()}/snapshots`),
    ]);
  } else {
    files = [];
    snapshots = [];
  }
  const active = new Set(files.filter(file => file.active).map(file => file.id));
  for (const id of selected) if (!active.has(id)) selected.delete(id);
  renderFiles();
  renderSnapshots();
}

async function run(action, progress = "", reportError = error => notice(error.message, true)) {
  busy = true;
  syncControls();
  notice(progress);
  try {
    await action();
  } catch (error) {
    reportError(error);
  } finally {
    busy = false;
    syncControls();
  }
}

project.addEventListener("change", () => {
  selected.clear();
  files = [];
  snapshots = [];
  renderFiles();
  renderSnapshots();
  run(refresh, "正在加载项目…").then(() => {
    if (byId("notice").textContent === "正在加载项目…") notice();
  });
});
history.addEventListener("change", () => run(refresh));
selectAll.addEventListener("change", () => {
  selected.clear();
  if (selectAll.checked) for (const file of files) if (file.active) selected.add(file.id);
  renderFiles();
});
byId("open-create").addEventListener("click", () => {
  byId("create-error").hidden = true;
  dialog.showModal();
});
byId("close-create").addEventListener("click", () => dialog.close());

createForm.addEventListener("submit", event => {
  event.preventDefault();
  const body = new FormData(createForm);
  const button = createForm.querySelector("button[type=submit]");
  const errorBox = byId("create-error");
  errorBox.hidden = true;
  button.disabled = true;
  run(async () => {
    const result = await request("/api/projects", { method: "POST", body });
    dialog.close();
    createForm.reset();
    selected.clear();
    files = [];
    snapshots = [];
    renderFiles();
    renderSnapshots();
    await loadProjects(result.project);
    await refresh();
    notice(`项目 ${result.project} 已就绪。`);
  }, "", error => {
    if (!dialog.open) return notice(error.message, true);
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  }).finally(() => { button.disabled = false; });
});

importForm.addEventListener("submit", event => {
  event.preventDefault();
  const body = new FormData(importForm);
  if (!body.get("route3")) body.delete("route3");
  run(async () => {
    const result = await request(`${projectPath()}/files`, { method: "POST", body });
    importForm.reset();
    selected.add(result.version_id);
    await refresh();
    notice(result.status === "unchanged" ? "这个版本已经在项目中。" : "文件已入库，可以选择它生成快照。");
  }, "正在导入文件，开启三路时可能需要较长时间…");
});

snapshot.addEventListener("click", () => run(async () => {
  const topK = snapshotTopK.value === "" ? null : snapshotTopK.valueAsNumber;
  if (!snapshotTopK.validity.valid || (topK !== null && (!Number.isSafeInteger(topK) || topK < 1))) {
    throw new Error("快照 BM25 Top K 请输入正整数，或留空保留全部。");
  }
  const result = await request(`${projectPath()}/snapshots`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ version_ids: [...selected], top_k: topK }),
  });
  await refresh();
  notice(`快照 ${result.snapshot_id.slice(0, 16)} 已生成，包含 ${result.nodes} 个节点和 ${result.edges} 条边。`);
}, "正在生成图谱快照…"));

run(async () => {
  await loadProjects();
  await refresh();
});
