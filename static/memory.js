let projects = [];
let memories = [];
let claudeMd = "";  // current server content
let claudeMdExpanded = false;
let claudeMdDraft = null;  // null when not editing; string when textarea has been touched
let claudeMdStatus = "";  // inline saved/error message
let expandedMemoryIds = new Set();
let expandedProjectSlugs = new Set();
let openAction = null;  // One inline scope or routing form.

let topics = [];
let fetchErrors = {};
let refreshing = false;
let topicDraft = null;
let topicStatus = "";
let routingStatus = "";
let routingSaving = false;
let topicSaving = false;

// --- Fetch ---

async function dashboardFetch(path, opts = {}) {
    try {
        return await apiFetch(path, opts);
    } catch (error) {
        return new Response(JSON.stringify({ detail: `Network error: ${error.message}` }), {
            status: 503, headers: { "Content-Type": "application/json" },
        });
    }
}

async function fetchResource(key, path, read, assign, empty) {
    try {
        const res = await dashboardFetch(`${API}${path}`);
        if (!res.ok) throw new Error(await errorDetail(res));
        const value = await read(res);
        assign(value);
        delete fetchErrors[key];
    } catch (error) {
        assign(empty);
        fetchErrors[key] = error.message;
    }
}

async function readArray(res) {
    const value = await res.json();
    if (!Array.isArray(value)) throw new Error("Invalid response: expected a list.");
    return value;
}

async function refresh() {
    if (refreshing || routingSaving || topicSaving) return;
    topicDraft = null;
    refreshing = true;
    claudeMdStatus = "";
    topicStatus = "";
    routingStatus = "";
    const notice = document.getElementById("refresh-status");
    notice.hidden = false;
    notice.textContent = "Refreshing…";
    await Promise.all([
        fetchResource("Projects", "/projects", readArray, v => projects = v, []),
        fetchResource("Memories", "/memory", readArray, v => memories = v, []),
        fetchResource("Instructions", "/config/claude-md", r => r.text(), v => claudeMd = v, ""),
        fetchResource("Topic catalog", "/config/memory-topics", readArray, v => topics = v, []),
    ]);
    refreshing = false;
    if (openAction && !memories.some(m => m.id === openAction.memoryId)) openAction = null;
    render();
}

// --- Render ---

function render() {
    const notice = document.getElementById("refresh-status");
    const errors = Object.entries(fetchErrors);
    notice.hidden = errors.length === 0;
    notice.textContent = errors.map(([name, message]) => `${name} refresh failed: ${message}`).join(" · ");
    renderStats();
    renderTopics();
    renderPendingReview();
    renderClaudeMd();
    renderGlobals();
    renderProjects();
}

function pendingReviewItems() {
    // One row per (project, instance_id, path) that needs review. A project
    // with project-level auto_registered_at gets at least one row regardless
    // of path flags; per-path flags surface separately.
    const items = [];
    for (const p of projects) {
        const projectFlagged = !!p.auto_registered_at;
        for (const path of (p.paths || [])) {
            if (projectFlagged || path.auto_registered_at) {
                items.push({
                    slug: p.slug,
                    instance_id: path.instance_id,
                    path: path.path,
                    auto_registered_at: path.auto_registered_at || p.auto_registered_at,
                    projectFlagged,
                });
            }
        }
        if (projectFlagged && (p.paths || []).length === 0) {
            // Edge case: project flagged but no paths recorded
            items.push({
                slug: p.slug,
                instance_id: null,
                path: null,
                auto_registered_at: p.auto_registered_at,
                projectFlagged: true,
            });
        }
    }
    items.sort((a, b) => (a.auto_registered_at || "").localeCompare(b.auto_registered_at || ""));
    return items;
}

function renderPendingReview() {
    const section = document.getElementById("pending-review-section");
    const list = document.getElementById("pending-review-list");
    const count = document.getElementById("pending-count");
    const items = pendingReviewItems();
    if (items.length === 0) {
        section.hidden = true;
        return;
    }
    section.hidden = false;
    count.textContent = `(${items.length})`;
    list.innerHTML = items.map(renderPendingRow).join("");
}

function renderPendingRow(it) {
    const flagBadge = it.projectFlagged
        ? `<span class="badge badge-yellow">new project</span>`
        : `<span class="badge badge-gray">new path</span>`;
    const pathLabel = it.path
        ? `<span class="memory-description">${escHtml(it.instance_id)} · <code>${escHtml(it.path)}</code></span>`
        : "";
    return `
        <div class="memory-row">
            <div class="memory-head">
                <span class="memory-name">${escHtml(it.slug)}</span>
                ${flagBadge}
                <span class="memory-actions">
                    <span class="memory-action" onclick="confirmAutoRegistered('${escAttr(it.slug)}', ${it.instance_id ? `'${escAttr(it.instance_id)}'` : "null"}, ${it.projectFlagged})">Confirm</span>
                    <span class="memory-action memory-action-danger" onclick="deletePendingEntry('${escAttr(it.slug)}', ${it.instance_id ? `'${escAttr(it.instance_id)}'` : "null"}, ${it.projectFlagged})">Delete</span>
                </span>
            </div>
            ${pathLabel}
        </div>
    `;
}

async function confirmAutoRegistered(slug, instanceId, projectFlagged) {
    // Always clear the path-level flag if we have one. If the project itself
    // is flagged, also clear the project-level flag.
    if (instanceId) {
        const r = await dashboardFetch(`${API}/projects/${encodeURIComponent(slug)}/paths/${encodeURIComponent(instanceId)}/confirm`, { method: "POST" });
        if (!r.ok && r.status !== 404) {
            alert(`Confirm failed: HTTP ${r.status}`);
            return;
        }
    }
    if (projectFlagged) {
        const r = await dashboardFetch(`${API}/projects/${encodeURIComponent(slug)}/confirm`, { method: "POST" });
        if (!r.ok) {
            alert(`Confirm (project) failed: HTTP ${r.status}`);
            return;
        }
    }
    await refresh();
}

async function deletePendingEntry(slug, instanceId, projectFlagged) {
    // If only this machine's path is flagged on an otherwise-confirmed project,
    // delete just the path. Otherwise nuke the whole project.
    if (!projectFlagged && instanceId) {
        if (!confirm(`Detach ${instanceId}'s path from project '${slug}'?`)) return;
        const r = await dashboardFetch(`${API}/projects/${encodeURIComponent(slug)}/paths/${encodeURIComponent(instanceId)}`, { method: "DELETE" });
        if (r.status !== 204) {
            alert(`Delete path failed: HTTP ${r.status}`);
            return;
        }
    } else {
        if (!confirm(`Delete project '${slug}' and all its paths?`)) return;
        const r = await dashboardFetch(`${API}/projects/${encodeURIComponent(slug)}`, { method: "DELETE" });
        if (r.status !== 204) {
            alert(`Delete project failed: HTTP ${r.status}`);
            return;
        }
    }
    await refresh();
}

function renderClaudeMd() {
    const el = document.getElementById("claude-md-section");
    if (fetchErrors.Instructions) {
        el.innerHTML = '<p class="empty-state">Instructions unavailable. Refresh to retry.</p>';
        return;
    }
    const caret = claudeMdExpanded ? "▾" : "▸";
    const bytes = claudeMd.length;
    const status = claudeMdStatus
        ? `<span class="claude-md-status">${escHtml(claudeMdStatus)}</span>`
        : "";
    const header = `
        <div class="claude-md-header" onclick="toggleClaudeMd()">
            <span class="archive-caret">${caret}</span>
            <span class="claude-md-label">CLAUDE.md</span>
            <span class="claude-md-meta">${bytes} char${bytes !== 1 ? "s" : ""}</span>
            ${status}
        </div>
    `;
    if (!claudeMdExpanded) {
        el.innerHTML = header;
        return;
    }
    const current = claudeMdDraft != null ? claudeMdDraft : claudeMd;
    const dirty = claudeMdDraft != null && claudeMdDraft !== claudeMd;
    const saveClass = dirty ? "memory-action" : "memory-action memory-action-disabled";
    const revertClass = dirty ? "memory-action" : "memory-action memory-action-disabled";
    el.innerHTML = `
        ${header}
        <div class="claude-md-body">
            <textarea id="claude-md-textarea" class="claude-md-textarea" oninput="onClaudeMdInput(this.value)" spellcheck="false">${escHtml(current)}</textarea>
            <div class="claude-md-actions">
                <span class="${saveClass}" onclick="saveClaudeMd()">Save</span>
                <span class="${revertClass}" onclick="revertClaudeMd()">Revert</span>
            </div>
        </div>
    `;
}

function renderStats() {
    const el = document.getElementById("memory-stats");
    if (fetchErrors.Memories || fetchErrors.Projects) {
        el.textContent = "Counts unavailable until refresh succeeds.";
        document.getElementById("global-count").textContent = "";
        document.getElementById("project-count").textContent = "";
        return;
    }
    const globalCount = memories.filter((m) => m.project_slug == null).length;
    const projectCount = memories.length - globalCount;
    el.textContent = `${projects.length} project${projects.length !== 1 ? "s" : ""} · ${memories.length} memor${memories.length !== 1 ? "ies" : "y"} (${globalCount} global · ${projectCount} project-scoped)`;
    document.getElementById("global-count").textContent = globalCount ? `(${globalCount})` : "";
    document.getElementById("project-count").textContent = projects.length ? `(${projects.length})` : "";
}

function renderGlobals() {
    const el = document.getElementById("global-memories");
    if (fetchErrors.Memories) {
        el.innerHTML = '<p class="empty-state">Memories unavailable. Refresh to retry.</p>';
        return;
    }
    const globals = memories.filter((m) => m.project_slug == null);
    globals.sort((a, b) => a.name.localeCompare(b.name));
    if (globals.length === 0) {
        el.innerHTML = '<p class="empty-state">No global memories.</p>';
        return;
    }
    el.innerHTML = globals.map((m) => renderMemoryRow(m, /*isGlobal=*/true)).join("");
}

function renderProjects() {
    const el = document.getElementById("project-list");
    if (fetchErrors.Projects || fetchErrors.Memories) {
        el.innerHTML = '<p class="empty-state">Projects or memories unavailable. Refresh to retry.</p>';
        return;
    }
    if (projects.length === 0) {
        el.innerHTML = '<p class="empty-state">No projects registered.</p>';
        return;
    }
    const sorted = [...projects].sort((a, b) => a.slug.localeCompare(b.slug));
    el.innerHTML = sorted.map(renderProjectRow).join("");
}

function renderProjectRow(p) {
    const scoped = memories.filter((m) => m.project_slug === p.slug);
    const open = expandedProjectSlugs.has(p.slug);
    const caret = open ? "▾" : "▸";
    const rows = open && scoped.length > 0
        ? scoped.slice().sort((a, b) => a.name.localeCompare(b.name))
            .map((m) => renderMemoryRow(m, /*isGlobal=*/false)).join("")
        : "";
    const emptyNote = open && scoped.length === 0
        ? '<p class="empty-state">No memories in this project.</p>'
        : "";
    const desc = p.description ? ` <span class="project-desc">${escHtml(p.description)}</span>` : "";
    return `
        <div class="project-row">
            <div class="project-header" onclick="toggleProject('${escAttr(p.slug)}')">
                <span class="archive-caret">${caret}</span>
                <span class="project-slug">${escHtml(p.slug)}</span>${desc}
                <span class="project-memory-count">${scoped.length} memor${scoped.length !== 1 ? "ies" : "y"}</span>
            </div>
            ${open ? `<div class="project-memories">${rows}${emptyNote}</div>` : ""}
        </div>
    `;
}

function renderMemoryRow(m, isGlobal) {
    const expanded = expandedMemoryIds.has(m.id);
    const caret = expanded ? "▾" : "▸";
    const typeClass = memoryTypeBadge(m.type);
    const actions = [];
    if (!isGlobal) {
        actions.push(`<span class="memory-action" onclick="startMoveToProject(${m.id})">Move to project</span>`);
        actions.push(`<span class="memory-action" onclick="startMoveToGlobal(${m.id})">Move to Global</span>`);
    } else {
        actions.push(`<span class="memory-action" onclick="startDistribute(${m.id})">Move to projects</span>`);
    }
    if (!fetchErrors["Topic catalog"] && Object.hasOwn(m, "topics")) {
        actions.push(`<button class="memory-action-button" onclick="startRouting(${m.id})">Edit routing</button>`);
    }
    actions.push(`<span class="memory-action memory-action-danger" onclick="deleteMemory(${m.id})">Delete</span>`);
    const form = renderInlineForm(m);
    const authorParts = [m.author_harness, m.author_model, m.author_session_id?.slice(0, 8)]
        .filter(Boolean).map(escHtml);
    const author = m.author_harness
        ? `<div class="memory-description">by ${authorParts.join(" · ")}</div>`
        : "";
    return `
        <div class="memory-row">
            <div class="memory-head">
                <span class="memory-toggle" onclick="toggleMemory(${m.id})">
                    <span class="archive-caret">${caret}</span>
                    <span class="memory-name">${escHtml(m.name)}</span>
                </span>
                <span class="badge ${typeClass}">${escHtml(m.type)}</span>
                <span class="memory-actions">${actions.join("")}</span>
            </div>
            ${m.description ? `<div class="memory-description">${escHtml(m.description)}</div>` : ""}
            ${author}
            ${renderRouting(m)}
            ${form}
            ${expanded ? `<pre class="memory-body">${escHtml(m.body || "")}</pre>` : ""}
        </div>
    `;
}

function memoryTypeBadge(type) {
    switch (type) {
        case "user": return "badge-green";
        case "feedback": return "badge-yellow";
        case "project": return "badge-gray";
        case "reference": return "badge-red";
        default: return "badge-gray";
    }
}

function renderInlineForm(m) {
    if (!openAction || openAction.memoryId !== m.id) return "";
    if (openAction.kind === "routing") return renderRoutingForm(m);
    if (openAction.kind === "reproject") {
        const others = projects.filter((p) => p.slug !== m.project_slug);
        if (others.length === 0) {
            return `<div class="memory-inline-form">No other projects to move to. <span class="memory-action" onclick="cancelAction()">Cancel</span></div>`;
        }
        const opts = others.map((p) => `<option value="${escAttr(p.slug)}">${escHtml(p.slug)}</option>`).join("");
        return `
            <div class="memory-inline-form">
                <label>Move to project:
                    <select id="reproject-target-${m.id}">${opts}</select>
                </label>
                <span class="memory-action" onclick="confirmMoveToProject(${m.id})">Confirm</span>
                <span class="memory-action" onclick="cancelAction()">Cancel</span>
            </div>
        `;
    }
    if (openAction.kind === "move") {
        return `
            <div class="memory-inline-form">
                <label>Move to Global as:
                    <select id="move-type-${m.id}">
                        <option value="user">user</option>
                        <option value="feedback">feedback</option>
                    </select>
                </label>
                <span class="memory-action" onclick="confirmMove(${m.id})">Confirm</span>
                <span class="memory-action" onclick="cancelAction()">Cancel</span>
            </div>
        `;
    }
    if (openAction.kind === "distribute") {
        if (projects.length === 0) {
            return `<div class="memory-inline-form">No projects to move to. <span class="memory-action" onclick="cancelAction()">Cancel</span></div>`;
        }
        const sorted = [...projects].sort((a, b) => a.slug.localeCompare(b.slug));
        const boxes = sorted.map((p) => `
            <label><input type="checkbox" name="distribute-${m.id}" value="${escAttr(p.slug)}"> ${escHtml(p.slug)}</label>
        `).join("");
        return `
            <div class="memory-inline-form memory-inline-form-distribute">
                <div class="distribute-label">Move to projects:</div>
                <div class="distribute-hint">One project moves it in place. Several splits it into per-project memories named <code>${escHtml(m.name)}-&lt;slug&gt;</code> - names are globally unique.</div>
                <div class="distribute-checkboxes">${boxes}</div>
                <div class="distribute-actions">
                    <span class="memory-action" onclick="confirmDistribute(${m.id})">Confirm</span>
                    <span class="memory-action" onclick="cancelAction()">Cancel</span>
                </div>
            </div>
        `;
    }
    return "";
}

// --- Routing metadata ---

function renderRouting(m) {
    const scope = m.project_slug == null ? "Global" : `Project: ${m.project_slug}`;
    let routing = "Routing unavailable";
    if (Object.hasOwn(m, "topics")) {
        routing = m.topics === null ? "Unclassified" : m.topics.length === 0 ? "Catalog only" : "Topic indexes";
    }
    const memberships = (m.topics || []).map(slug => {
        const topic = topics.find(t => t.slug === slug);
        return escHtml(topic ? topic.title : `${slug} (unavailable topic)`);
    }).join(" · ");
    const placement = m.project_slug != null
        ? "Project startup index - takes precedence over routing"
        : m.topics === null ? "Startup index until classified"
        : Array.isArray(m.topics) && m.topics.length === 0 ? "Full catalog - available on demand"
        : "Topic indexes and full catalog - available on demand";
    return `<div class="memory-routing">
        <span><strong>Scope</strong> ${escHtml(scope)}</span>
        <span><strong>Routing</strong> ${routing}${memberships ? `: ${memberships}` : ""}</span>
        <span class="memory-placement">${Object.hasOwn(m, "topics") ? placement : "Refresh with a compatible server to inspect routing."}</span>
    </div>`;
}

function renderTopics() {
    const el = document.getElementById("topic-catalog");
    document.getElementById("topic-count").textContent = fetchErrors["Topic catalog"] ? "" : `(${topics.length})`;
    if (fetchErrors["Topic catalog"]) {
        el.innerHTML = '<p class="empty-state">Topic catalog unavailable. Refresh to retry.</p>';
    } else {
        el.innerHTML = topics.length ? topics.map(t => `<div class="topic-definition">
            <strong>${escHtml(t.title)}</strong> <code>${escHtml(t.slug)}</code>
            <p>${escHtml(t.description)}</p>
        </div>`).join("") : '<p class="empty-state">No topics defined. Unclassified memories stay in startup; catalog-only memories remain discoverable.</p>';
    }
    renderTopicEditor();
}

function toggleTopicEditor() {
    if (topicSaving) return;
    if (topicDraft !== null) topicDraft = null;
    else if (!fetchErrors["Topic catalog"]) topicDraft = topics.map(t => ({ ...t }));
    topicStatus = "";
    renderTopicEditor();
}

function renderTopicEditor() {
    const el = document.getElementById("topic-editor");
    el.hidden = topicDraft === null;
    if (topicDraft === null) return;
    el.innerHTML = `<p class="memory-help">Slugs are stable. To rename, add a topic, reassign its memories, then remove the old topic. Referenced topics cannot be removed.</p>
        ${topicDraft.map((t, i) => `<div class="topic-editor-row">
            <label>Slug<input value="${escAttr(t.slug)}" oninput="updateTopicDraft(${i}, 'slug', this.value)" placeholder="topic-slug" ${topicSaving ? "disabled" : ""}></label>
            <label>Title<input value="${escAttr(t.title)}" oninput="updateTopicDraft(${i}, 'title', this.value)" ${topicSaving ? "disabled" : ""}></label>
            <label class="topic-description-field">Activity description<textarea rows="2" oninput="updateTopicDraft(${i}, 'description', this.value)" ${topicSaving ? "disabled" : ""}>${escHtml(t.description)}</textarea></label>
            <button class="memory-action-button memory-action-danger" onclick="removeTopicDraft(${i})" ${topicSaving ? "disabled" : ""}>Remove</button>
        </div>`).join("")}
        <div class="topic-editor-actions">
            <button class="memory-action-button" onclick="addTopicDraft()" ${topicSaving ? "disabled" : ""}>Add topic</button>
            <button class="memory-action-button" onclick="saveTopics()" ${topicSaving || fetchErrors["Topic catalog"] ? "disabled" : ""}>${topicSaving ? "Saving…" : "Save catalog"}</button>
            <button class="memory-action-button" onclick="toggleTopicEditor()" ${topicSaving ? "disabled" : ""}>Cancel</button>
        </div>
        <p class="memory-notice" role="status">${escHtml(topicStatus)}</p>`;
}

function updateTopicDraft(index, field, value) {
    if (topicDraft !== null && !topicSaving) topicDraft[index][field] = value;
    topicStatus = "";
    const notice = document.querySelector("#topic-editor .memory-notice");
    if (notice) notice.textContent = "";
}

function addTopicDraft() {
    if (topicDraft === null || topicSaving) return;
    topicDraft.push({ slug: "", title: "", description: "" });
    topicStatus = "";
    renderTopicEditor();
}

function removeTopicDraft(index) {
    if (topicDraft === null || topicSaving) return;
    topicDraft.splice(index, 1);
    topicStatus = "";
    renderTopicEditor();
}

async function saveTopics() {
    if (topicDraft === null || topicSaving || fetchErrors["Topic catalog"]) return;
    const submitted = topicDraft.map(t => ({ ...t }));
    topicSaving = true;
    topicStatus = "";
    renderTopicEditor();
    let published = false;
    try {
        const res = await dashboardFetch(`${API}/config/memory-topics`, {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(submitted),
        });
        if (!res.ok) throw new Error(await errorDetail(res));
        published = true;
        const readback = await dashboardFetch(`${API}/config/memory-topics`);
        if (!readback.ok) throw new Error(await errorDetail(readback));
        const saved = await readArray(readback);
        const canonical = catalog => JSON.stringify(catalog.map(t => ({
            slug: t.slug, title: t.title, description: t.description,
        })).sort((a, b) => a.slug.localeCompare(b.slug)));
        if (canonical(saved) !== canonical(submitted)) {
            throw new Error("Returned catalog differs from the submitted catalog.");
        }
        topics = saved;
        topicDraft = saved.map(t => ({ ...t }));
        topicStatus = "Catalog saved.";
        routingStatus = "";
    } catch (error) {
        if (published) {
            topics = [];
            fetchErrors["Topic catalog"] = error.message;
            topicStatus = `Catalog saved on server, but verification failed: ${error.message} Refresh to inspect the saved catalog before retrying.`;
        } else {
            topicStatus = `Save failed: ${error.message}`;
        }
    } finally {
        topicSaving = false;
        render();
    }
}

function startRouting(id) {
    if (routingSaving) return;
    const m = memories.find(x => x.id === id);
    if (!m || !Object.hasOwn(m, "topics") || fetchErrors["Topic catalog"]) return;
    openAction = {
        memoryId: id, kind: "routing",
        mode: m.topics === null ? "unclassified" : m.topics.length ? "topics" : "catalog",
        topics: [...(m.topics || [])],
    };
    routingStatus = "";
    render();
}

function setRoutingMode(value) {
    if (!openAction || routingSaving) return;
    openAction.mode = value;
    routingStatus = "";
    render();
}

function setRoutingTopic(slug, checked) {
    if (!openAction || routingSaving) return;
    openAction.topics = openAction.topics.filter(t => t !== slug);
    if (checked) openAction.topics.push(slug);
    routingStatus = "";
    const notice = document.querySelector(".memory-routing-form .memory-notice");
    if (notice) notice.textContent = "";
}

function renderRoutingForm(m) {
    const mode = openAction.mode;
    const options = [["unclassified", "Unclassified - startup visible"], ["topics", "Topic indexes - selective reading"], ["catalog", "Catalog only - available on demand"]];
    return `<div class="memory-inline-form memory-routing-form">
        <label>Routing<select onchange="setRoutingMode(this.value)" ${routingSaving ? "disabled" : ""}>
            ${options.map(([value, label]) => `<option value="${value}" ${mode === value ? "selected" : ""}>${label}</option>`).join("")}
        </select></label>
        ${m.project_slug != null ? '<p class="memory-help">This project memory stays in its project startup index with any routing choice.</p>' : ""}
        ${mode === "topics" ? `<div class="routing-topic-options">${topics.length ? topics.map(t => `<label>
            <input type="checkbox" ${openAction.topics.includes(t.slug) ? "checked" : ""} ${routingSaving ? "disabled" : ""} onchange="setRoutingTopic('${escAttr(t.slug)}', this.checked)">
            <span><strong>${escHtml(t.title)}</strong><small>${escHtml(t.description)}</small></span>
        </label>`).join("") : '<p class="memory-help">Add a topic to the catalog before selecting topic routing.</p>'}</div>` : ""}
        <div class="topic-editor-actions">
            <button class="memory-action-button" onclick="saveRouting(${m.id})" ${routingSaving || fetchErrors["Topic catalog"] ? "disabled" : ""}>${routingSaving ? "Saving…" : "Save routing"}</button>
            <button class="memory-action-button" onclick="cancelAction()" ${routingSaving ? "disabled" : ""}>Cancel</button>
        </div>
        <p class="memory-notice" role="status">${escHtml(routingStatus)}</p>
    </div>`;
}

async function saveRouting(id) {
    if (!openAction || openAction.memoryId !== id || routingSaving || fetchErrors["Topic catalog"]) return;
    const next = openAction.mode === "unclassified" ? null : openAction.mode === "catalog" ? [] : openAction.topics;
    if (Array.isArray(next) && openAction.mode === "topics" && next.length === 0) {
        routingStatus = "Pick at least one topic, or choose Catalog only.";
        render();
        return;
    }
    routingSaving = true;
    routingStatus = "";
    render();
    try {
        const res = await dashboardFetch(`${API}/memory/${id}`, {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ topics: next }),
        });
        if (!res.ok) throw new Error(await errorDetail(res));
        const saved = await res.json();
        if (!Object.hasOwn(saved, "topics") || JSON.stringify(saved.topics) !== JSON.stringify(next)) {
            throw new Error("Server did not confirm routing. Refresh before retrying.");
        }
        const index = memories.findIndex(m => m.id === id);
        if (index >= 0) memories[index] = saved;
        routingStatus = "Routing saved.";
    } catch (error) {
        routingStatus = `Save failed: ${error.message}`;
    } finally {
        routingSaving = false;
        render();
    }
}

// --- CLAUDE.md actions ---

function toggleClaudeMd() {
    claudeMdExpanded = !claudeMdExpanded;
    if (!claudeMdExpanded) {
        claudeMdDraft = null;
        claudeMdStatus = "";
    }
    render();
}

function onClaudeMdInput(value) {
    claudeMdDraft = value;
    claudeMdStatus = "";
    const status = document.querySelector(".claude-md-status");
    if (status) status.textContent = "";
    // Re-render only the action buttons' dirty state without rebuilding the
    // textarea (which would lose caret position). The simplest path: toggle
    // a CSS class on the buttons via direct DOM rather than full render().
    const actions = document.querySelectorAll(".claude-md-actions .memory-action");
    const dirty = claudeMdDraft !== claudeMd;
    for (const a of actions) {
        a.classList.toggle("memory-action-disabled", !dirty);
    }
}

async function saveClaudeMd() {
    if (claudeMdDraft == null || claudeMdDraft === claudeMd) return;
    if (!claudeMdDraft.trim()) {
        alert("CLAUDE.md cannot be empty.");
        return;
    }
    const res = await dashboardFetch(`${API}/config/claude-md`, {
        method: "PUT",
        headers: { "Content-Type": "text/plain" },
        body: claudeMdDraft,
    });
    if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
            const data = await res.json();
            if (data.detail) detail = data.detail;
        } catch (_) { /* ignore */ }
        claudeMdStatus = `Save failed: ${detail}`;
        render();
        return;
    }
    const data = await res.json();
    claudeMd = claudeMdDraft;
    claudeMdDraft = null;
    claudeMdStatus = `Saved ${data.updated_at}`;
    render();
}

function revertClaudeMd() {
    if (claudeMdDraft == null || claudeMdDraft === claudeMd) return;
    claudeMdDraft = null;
    claudeMdStatus = "";
    render();
}

// --- Toggles ---

function toggleMemory(id) {
    if (expandedMemoryIds.has(id)) expandedMemoryIds.delete(id);
    else expandedMemoryIds.add(id);
    render();
}

function toggleProject(slug) {
    if (expandedProjectSlugs.has(slug)) expandedProjectSlugs.delete(slug);
    else expandedProjectSlugs.add(slug);
    render();
}

// --- Actions ---

function startMoveToProject(id) {
    if (routingSaving) return;
    openAction = { memoryId: id, kind: "reproject" };
    render();
}

function startMoveToGlobal(id) {
    if (routingSaving) return;
    openAction = { memoryId: id, kind: "move" };
    render();
}

function startDistribute(id) {
    if (routingSaving) return;
    openAction = { memoryId: id, kind: "distribute" };
    render();
}

function cancelAction() {
    if (routingSaving) return;
    openAction = null;
    routingStatus = "";
    render();
}

async function deleteMemory(id) {
    const m = memories.find((x) => x.id === id);
    if (!m) return;
    const scope = m.project_slug ? `project '${m.project_slug}'` : "global";
    if (!confirm(`Delete memory '${m.name}' (${scope})?`)) return;
    const res = await dashboardFetch(`${API}/memory/${id}`, { method: "DELETE" });
    if (res.status !== 204) {
        alert(`Delete failed: HTTP ${res.status}`);
        return;
    }
    memories = memories.filter((x) => x.id !== id);
    expandedMemoryIds.delete(id);
    render();
}

// Re-scoping is a PUT, never create-then-delete. A new row means a new id, and
// every mirror file still carrying the OLD id then looks server-deleted - which
// is exactly how a "move" used to resurrect itself as a duplicate.
async function rescopeMemory(id, projectSlug, extra = {}) {
    const res = await dashboardFetch(`${API}/memory/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_slug: projectSlug, ...extra }),
    });
    if (!res.ok) {
        alert(`Move failed: ${await errorDetail(res)}`);
        return null;
    }
    const saved = await res.json();
    const idx = memories.findIndex((x) => x.id === saved.id);
    if (idx >= 0) memories[idx] = saved; else memories.push(saved);
    openAction = null;
    render();
    return saved;
}

async function confirmMoveToProject(id) {
    const sel = document.getElementById(`reproject-target-${id}`);
    const target = sel && sel.value;
    if (!target) return;
    await rescopeMemory(id, target);
}

async function confirmMove(id) {
    const sel = document.getElementById(`move-type-${id}`);
    const newType = (sel && sel.value) || "user";
    await rescopeMemory(id, null, { type: newType });
}

async function confirmDistribute(id) {
    const m = memories.find((x) => x.id === id);
    if (!m) return;
    const checked = document.querySelectorAll(`input[name="distribute-${id}"]:checked`);
    const targets = [...checked].map((el) => el.value);
    if (targets.length === 0) {
        alert("Pick at least one project.");
        return;
    }
    // A single target is a plain re-scope: same row, same id, same name.
    if (targets.length === 1) {
        await rescopeMemory(id, targets[0]);
        expandedMemoryIds.delete(id);
        return;
    }

    // Several targets can't all keep one name (names are globally unique), so
    // each lands as '<name>-<slug>' and the global original is deleted.
    const named = targets.map((t) => ({ target: t, name: `${m.name}-${t}` }));
    const clashes = named.filter((n) => memories.some(
        (x) => x.name === n.name && x.project_slug !== n.target,
    ));
    if (clashes.length > 0) {
        // Deliberately NOT sent with rescope:true - that would drag the existing
        // memory out of the project it is pinned to. Let the server 409 instead.
        alert(`Cannot split: ${clashes.map((n) => `'${n.name}'`).join(", ")} already exists elsewhere. Rename that memory first.`);
        return;
    }
    if (!confirm(`Split '${m.name}' into ${named.map((n) => `'${n.name}'`).join(", ")} and delete the global original?`)) return;
    const payloadBase = {
        description: m.description || "",
        type: m.type,
        body: m.body || "",
        topics: m.topics,
    };
    const results = await Promise.all(named.map(async ({ target, name }) => {
        const res = await dashboardFetch(`${API}/memory`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ...payloadBase, name, project_slug: target }),
        });
        return {
            target,
            ok: res.ok,
            saved: res.ok ? await res.json() : null,
            detail: res.ok ? null : await errorDetail(res),
        };
    }));
    const failed = results.filter((r) => !r.ok);
    if (failed.length > 0) {
        const msg = failed.map((f) => `${f.target}: ${f.detail}`).join(", ");
        alert(`Move to projects partially failed: ${msg}. Global memory NOT deleted. Successful copies are saved; you may retry.`);
        await refresh();
        return;
    }
    const delRes = await dashboardFetch(`${API}/memory/${id}`, { method: "DELETE" });
    if (delRes.status !== 204) {
        alert(`Move to projects partially failed: copies created, but DELETE of original returned HTTP ${delRes.status}. Resolve manually.`);
        await refresh();
        return;
    }
    memories = memories.filter((x) => x.id !== id);
    for (const r of results) {
        const idx = memories.findIndex((x) => x.id === r.saved.id);
        if (idx >= 0) memories[idx] = r.saved; else memories.push(r.saved);
    }
    openAction = null;
    expandedMemoryIds.delete(id);
    render();
}

// --- Helpers ---

async function errorDetail(res) {
    // Surface the server's message (the 409 explains WHY a name is refused);
    // a bare status code leaves the user with nothing to act on.
    try {
        const body = await res.json();
        if (body && body.detail) return `${body.detail} (HTTP ${res.status})`;
    } catch { /* not JSON */ }
    return `HTTP ${res.status}`;
}

function escAttr(s) {
    return escHtml(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// --- Init ---

ensureToken();
refresh();
