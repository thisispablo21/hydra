// API, authToken, ensureToken, clearToken, apiFetch, escHtml live in utils.js
// and are loaded as globals before this script.

const MAX_EVENTS = 100;

let sessions = {};
let archived = {};
let archiveOpen = false;
let eventLog = [];
let selectedSessionIds = new Set();
let editorConfig = { default: { editor: "vscode", type: "local" }, instances: {} };

// --- Fetch initial state ---

async function fetchSessions() {
    try {
        const res = await apiFetch(`${API}/sessions`);
        if (!res.ok) return;
        const data = await res.json();
        sessions = {};
        for (const s of data) {
            sessions[s.session_id] = s;
        }
        renderSessions();
    } catch (e) {
        console.error("Failed to fetch sessions:", e);
    }
}

// --- SSE connection ---

function connectSSE() {
    const statusEl = document.getElementById("connection-status");
    ensureToken();
    const url = authToken
        ? `${API}/events/stream?token=${encodeURIComponent(authToken)}`
        : `${API}/events/stream`;
    const source = new EventSource(url);

    source.onopen = () => {
        statusEl.textContent = "Connected";
        statusEl.className = "badge badge-green";
    };

    source.addEventListener("hook_event", (e) => {
        const data = JSON.parse(e.data);
        handleEvent(data);
    });

    source.onerror = () => {
        statusEl.textContent = "Disconnected";
        statusEl.className = "badge badge-red";
        source.close();
        // Reconnect after 3 seconds
        setTimeout(connectSSE, 3000);
    };
}

function handleEvent(data) {
    // Archive lifecycle events arrive over the same stream.
    if (data.event_name === "session_archived") {
        if (sessions[data.session_id]) {
            delete sessions[data.session_id];
            renderSessions();
        }
        if (archiveOpen) fetchArchived();
        return;
    }
    if (data.event_name === "session_archived_bulk") {
        for (const sid of data.session_ids || []) delete sessions[sid];
        renderSessions();
        if (archiveOpen) fetchArchived();
        return;
    }
    if (data.event_name === "session_unarchived") {
        if (archived[data.session_id]) {
            delete archived[data.session_id];
            renderArchive();
        }
        fetchSessions();
        return;
    }
    if (data.event_name === "session_url_updated") {
        if (sessions[data.session_id]) {
            sessions[data.session_id].remote_control_url = data.remote_control_url;
            renderSessions();
        }
        return;
    }
    if (data.event_name === "session_stale") {
        if (sessions[data.session_id]) {
            sessions[data.session_id].status = "ended";
            sessions[data.session_id].end_reason = "stale";
            renderSessions();
        }
        return;
    }

    // Update session state optimistically
    const sid = data.session_id;
    if (sessions[sid]) {
        const s = sessions[sid];
        s.last_event_at = data.received_at;
        if (data.tool_name) s.last_tool = data.tool_name;
        if (data.tool_input_summary) s.last_tool_input_summary = data.tool_input_summary;

        if (data.cwd) s.cwd = data.cwd;

        switch (data.event_name) {
            case "SessionStart":
            case "UserPromptSubmit":
            case "PostToolUse":
            case "SubagentStart":
                s.status = "active";
                break;
            case "SubagentStop":
                break;
            case "SessionEnd":
                s.status = "ended";
                break;
            case "Stop":
                s.status = "idle";
                break;
            case "Notification":
                s.status = "waiting_input";
                break;
        }
    } else if (data.event_name === "SessionStart") {
        // New session
        sessions[sid] = {
            session_id: sid,
            instance_id: data.instance_id,
            status: "active",
            cwd: data.cwd || "",
            last_event_at: data.received_at,
            last_tool: null,
            last_tool_input_summary: null,
            files_changed: [],
            started_at: data.received_at,
        };
    } else {
        // Unknown session - refetch all
        fetchSessions();
        return;
    }

    // Add to event log
    eventLog.unshift(data);
    if (eventLog.length > MAX_EVENTS) eventLog.pop();

    renderSessions();
    renderEventLog();
}

// --- Rendering ---

function renderSessions() {
    const grid = document.getElementById("sessions-grid");
    const list = Object.values(sessions);

    if (list.length === 0) {
        grid.innerHTML = '<p class="empty-state">No sessions yet. Start a Claude Code instance with hooks configured.</p>';
        updateCounts(list);
        renderEventFilter();
        return;
    }

    // Sort: active/waiting first, then idle, then ended
    const order = { active: 0, waiting_input: 1, idle: 2, ended: 3 };
    list.sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9));

    grid.innerHTML = list.map(renderCard).join("");
    updateCounts(list);
    renderEventFilter();
}

// --- Event filter chips ---

function renderEventFilter() {
    const eligible = Object.values(sessions).filter(
        (s) => s.status === "active" || s.status === "waiting_input" || s.status === "idle"
    );
    // Prune selection to the eligible set - auto-removes archived/ended sessions.
    const eligibleIds = new Set(eligible.map((s) => s.session_id));
    for (const sid of [...selectedSessionIds]) {
        if (!eligibleIds.has(sid)) selectedSessionIds.delete(sid);
    }

    const container = document.getElementById("event-filter");
    if (!container) return;
    if (eligible.length === 0) {
        container.innerHTML = "";
        return;
    }

    const chips = eligible.map((s) => {
        const base = s.cwd ? s.cwd.split(/[\/\\]/).filter(Boolean).pop() : "";
        const project = base || s.session_id.slice(0, 8);
        const selected = selectedSessionIds.has(s.session_id) ? " selected" : "";
        return `<span class="event-filter-chip${selected}" title="${escHtml(s.session_id)}" onclick="toggleSessionFilter('${s.session_id}')">${escHtml(s.instance_id)} · ${escHtml(project)}</span>`;
    }).join("");

    const clear = selectedSessionIds.size > 0
        ? `<span class="event-filter-clear" onclick="clearEventFilter()">Clear</span>`
        : "";

    container.innerHTML = `<span class="event-filter-label">Filter:</span>${chips}${clear}`;
}

function toggleSessionFilter(sid) {
    if (selectedSessionIds.has(sid)) {
        selectedSessionIds.delete(sid);
    } else {
        selectedSessionIds.add(sid);
    }
    renderEventFilter();
    renderEventLog();
}

function clearEventFilter() {
    selectedSessionIds.clear();
    renderEventFilter();
    renderEventLog();
}

function renderCard(s) {
    const shortCwd = s.cwd ? s.cwd.split("/").slice(-2).join("/") : "-";
    const statusLabel = {
        active: "Working",
        waiting_input: "Waiting for Input",
        idle: "Idle",
        ended: "Ended",
    }[s.status] || s.status;
    const statusBadge = {
        active: "badge-green",
        waiting_input: "badge-yellow",
        idle: "badge-gray",
        ended: "badge-red",
    }[s.status] || "badge-gray";

    const lastActivity = s.last_tool
        ? `<span class="tool-name">${escHtml(s.last_tool)}</span>${s.last_tool_input_summary ? " <code>" + escHtml(truncate(s.last_tool_input_summary, 60)) + "</code>" : ""}`
        : "-";

    const ago = timeAgo(s.last_event_at);
    const files = Array.isArray(s.files_changed) ? s.files_changed : [];
    const filesCount = files.length;

    const filesList = files.map((f) => {
        const uri = editorUri(s.instance_id, f);
        const name = fileName(f);
        if (uri) {
            return `<a class="file-link" href="${escHtml(uri)}" title="${escHtml(f)}">${escHtml(name)}</a>`;
        }
        return `<span class="file-link" title="${escHtml(f)}">${escHtml(name)}</span>`;
    }).join("");

    const filesToggle = filesCount > 0
        ? `<span class="files-count clickable" onclick="toggleFiles('${s.session_id}')">${filesCount} file${filesCount !== 1 ? "s" : ""} changed</span>`
        : `<span class="files-count">0 files changed</span>`;

    const archivable = s.status === "ended" || s.status === "idle";
    const archiveBtn = archivable
        ? `<span class="archive-btn" title="Archive" onclick="archiveSession('${s.session_id}')">×</span>`
        : "";

    return `
        <article class="session-card status-${s.status}">
            <div class="card-header">
                <span class="instance-name">${escHtml(s.instance_id)}</span>
                <span class="card-header-right">
                    <span class="badge ${statusBadge}">${statusLabel}</span>
                    ${archiveBtn}
                </span>
            </div>
            <div class="cwd" title="${escHtml(s.cwd)}">${escHtml(shortCwd)}</div>
            <div class="last-activity"><span class="last-activity-text">${lastActivity}</span><span class="time-ago">${ago}</span></div>
            ${filesCount > 0 ? `<div id="files-${s.session_id}" class="files-list hidden">${filesList}</div>` : ""}
            <div class="card-footer">
                ${filesToggle}
                ${renderRemoteControl(s)}
            </div>
        </article>
    `;
}

function renderArchivedCard(s) {
    const shortCwd = s.cwd ? s.cwd.split("/").slice(-2).join("/") : "-";
    const when = s.archived_at ? timeAgo(s.archived_at) : "";
    return `
        <article class="session-card archived-card status-${s.status}">
            <div class="card-header">
                <span class="instance-name">${escHtml(s.instance_id)}</span>
                <span class="archive-btn" title="Unarchive" onclick="unarchiveSession('${s.session_id}')">↺</span>
            </div>
            <div class="cwd" title="${escHtml(s.cwd)}">${escHtml(shortCwd)}</div>
            <div class="last-activity"><span class="last-activity-text">archived ${when}</span></div>
        </article>
    `;
}

function renderRemoteControl(s) {
    const url = s.remote_control_url;
    if (url) {
        return `
            <span class="remote-slot">
                <a class="remote-link" href="${escHtml(url)}" target="_blank" rel="noopener">Open Remote Control</a>
                <span class="remote-edit" title="Change URL" onclick="editRemoteControlUrl('${s.session_id}')">✎</span>
            </span>
        `;
    }
    return `<input type="url" class="remote-url-input" placeholder="Paste Remote Control URL"
                   onkeydown="if (event.key === 'Enter') { event.preventDefault(); setRemoteControlUrl('${s.session_id}', this.value.trim()); }"
                   onblur="if (this.value.trim()) setRemoteControlUrl('${s.session_id}', this.value.trim());">`;
}

async function setRemoteControlUrl(sessionId, url) {
    const res = await apiFetch(`${API}/sessions/${sessionId}/remote-control-url`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
    });
    if (res.ok) {
        const data = await res.json();
        if (sessions[sessionId]) {
            sessions[sessionId].remote_control_url = data.remote_control_url;
            renderSessions();
        }
    } else if (res.status === 400) {
        alert("Invalid URL - expected https://claude.ai/code/session_...");
    } else if (res.status === 404) {
        console.warn("Session not found when setting Remote Control URL");
    } else {
        console.error("Set remote URL failed:", res.status);
    }
}

function editRemoteControlUrl(sessionId) {
    const current = sessions[sessionId]?.remote_control_url || "";
    const next = window.prompt("Remote Control URL (clear to remove):", current);
    if (next === null) return;
    setRemoteControlUrl(sessionId, next.trim());
}

async function archiveSession(sessionId) {
    const res = await apiFetch(`${API}/sessions/${sessionId}/archive`, { method: "POST" });
    if (res.status === 204) {
        delete sessions[sessionId];
        renderSessions();
        if (archiveOpen) fetchArchived();
    } else if (res.status === 409) {
        console.warn("Cannot archive session in active state");
    } else {
        console.error("Archive failed:", res.status);
    }
}

async function unarchiveSession(sessionId) {
    const res = await apiFetch(`${API}/sessions/${sessionId}/unarchive`, { method: "POST" });
    if (res.status === 204) {
        delete archived[sessionId];
        renderArchive();
        fetchSessions();
    } else {
        console.error("Unarchive failed:", res.status);
    }
}

async function archiveAllEnded() {
    const count = Object.values(sessions).filter(
        (s) => s.status === "ended" || s.status === "idle"
    ).length;
    if (count === 0) return;
    if (!confirm(`Archive ${count} ended/idle session${count !== 1 ? "s" : ""}?`)) return;
    const res = await apiFetch(`${API}/sessions/archive-ended`, { method: "POST" });
    if (!res.ok) {
        console.error("Bulk archive failed:", res.status);
        return;
    }
    const data = await res.json();
    for (const sid of data.session_ids || []) delete sessions[sid];
    renderSessions();
    if (archiveOpen) fetchArchived();
}

async function fetchArchived() {
    try {
        const res = await apiFetch(`${API}/sessions?archived=true`);
        if (!res.ok) return;
        const data = await res.json();
        archived = {};
        for (const s of data) archived[s.session_id] = s;
        renderArchive();
    } catch (e) {
        console.error("Failed to fetch archive:", e);
    }
}

function toggleArchive() {
    archiveOpen = !archiveOpen;
    const grid = document.getElementById("archive-grid");
    const caret = document.getElementById("archive-caret");
    if (archiveOpen) {
        grid.classList.remove("hidden");
        caret.textContent = "▾";
        fetchArchived();
    } else {
        grid.classList.add("hidden");
        caret.textContent = "▸";
    }
}

function renderArchive() {
    const grid = document.getElementById("archive-grid");
    const countEl = document.getElementById("archive-count");
    const list = Object.values(archived);
    countEl.textContent = list.length ? `(${list.length})` : "";
    if (list.length === 0) {
        grid.innerHTML = '<p class="empty-state">No archived sessions.</p>';
        return;
    }
    list.sort((a, b) => (b.archived_at || "").localeCompare(a.archived_at || ""));
    grid.innerHTML = list.map(renderArchivedCard).join("");
}

function renderEventLog() {
    const log = document.getElementById("event-log");
    const filtered = selectedSessionIds.size === 0
        ? eventLog
        : eventLog.filter((e) => selectedSessionIds.has(e.session_id));
    if (filtered.length === 0) {
        const msg = selectedSessionIds.size === 0
            ? "Waiting for events..."
            : "No events match the filter.";
        log.innerHTML = `<p class="empty-state">${msg}</p>`;
        return;
    }
    log.innerHTML = filtered.map((e) => {
        const time = new Date(e.received_at).toLocaleTimeString();
        const detail = e.tool_input_summary ? escHtml(truncate(e.tool_input_summary, 80)) : "";
        const shortSession = e.session_id ? e.session_id.slice(0, 8) : "-";
        return `
            <div class="event-row">
                <span class="event-time">${time}</span>
                <span class="event-session">${shortSession}</span>
                <span class="event-instance">${escHtml(e.instance_id || "-")}</span>
                <span class="event-name">${escHtml(e.event_name)}</span>
                <span class="event-detail">${e.tool_name ? escHtml(e.tool_name) + " " : ""}${detail}</span>
            </div>
        `;
    }).join("");
}

function updateCounts(list) {
    const counts = { active: 0, waiting_input: 0, idle: 0, ended: 0 };
    for (const s of list) counts[s.status] = (counts[s.status] || 0) + 1;
    document.getElementById("count-active").textContent = `${counts.active} Active`;
    document.getElementById("count-waiting").textContent = `${counts.waiting_input} Waiting`;
    document.getElementById("count-idle").textContent = `${counts.idle} Idle`;
    document.getElementById("count-ended").textContent = `${counts.ended} Ended`;
}

// --- Helpers ---

function truncate(str, len) {
    return str.length > len ? str.slice(0, len) + "..." : str;
}

function timeAgo(iso) {
    if (!iso) return "";
    const diff = (Date.now() - new Date(iso).getTime()) / 1000;
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
}

// --- Editor deep-links ---

async function fetchEditorConfig() {
    try {
        const res = await apiFetch(`${API}/editors`);
        if (!res.ok) return;
        editorConfig = await res.json();
    } catch (e) {
        console.error("Failed to fetch editor config:", e);
    }
}

// Convert Git Bash path (/c/Users/...) to Windows path (C:/Users/...)
function toWindowsPath(p) {
    const m = p.match(/^\/([a-zA-Z])\/(.*)/);
    return m ? `${m[1].toUpperCase()}:/${m[2]}` : p;
}

function editorUri(instanceId, filePath) {
    const cfg = editorConfig.instances[instanceId] || editorConfig.default || {};
    const editor = cfg.editor || "vscode";
    const type = cfg.type || "local";
    const scheme = editor === "cursor" ? "cursor" : "vscode";

    if (type === "wsl" && cfg.distro) {
        return `${scheme}://vscode-remote/wsl+${cfg.distro}${filePath}`;
    }

    if (type === "ssh-remote" && cfg.host) {
        return `${scheme}://vscode-remote/ssh-remote+${cfg.host}${filePath}`;
    }

    if (type === "local") {
        return `${scheme}://file/${toWindowsPath(filePath)}`;
    }

    if (editor === "jetbrains") {
        return `jetbrains://open?file=${encodeURIComponent(filePath)}`;
    }

    return null;
}

function fileName(path) {
    return path.split("/").pop() || path;
}

function toggleFiles(sessionId) {
    const el = document.getElementById(`files-${sessionId}`);
    if (el) el.classList.toggle("hidden");
}

// --- Init ---

ensureToken();
fetchEditorConfig();
fetchSessions();
connectSSE();

setInterval(fetchSessions, 30000);
