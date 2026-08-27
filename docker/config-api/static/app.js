// Vanilla JS admin UI for the OP25 config-api. No build step, no framework
// -- fetch() against /api/*, plain DOM updates. Same convention as
// op25/gr-op25_repeater/www/www-static/main.js.

let tagSets = [];
let accessLists = [];
let categories = [];
let systemsList = [];

function toast(msg, isError) {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.className = isError ? "error" : "";
    el.style.display = "block";
    setTimeout(() => { el.style.display = "none"; }, 4000);
}

async function api(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
    }
    const resp = await fetch(path, opts);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
}

// Internal tab id -> URL path (kept distinct so the "lists" tab id can read
// nicer in the address bar without renaming it everywhere in this file).
const TAB_PATHS = { systems: "/systems", sites: "/sites", talkgroups: "/talkgroups", lists: "/access-lists", devices: "/devices" };
const PATH_TABS = Object.fromEntries(Object.entries(TAB_PATHS).map(([tab, path]) => [path, tab]));

function tabFromPath(pathname) {
    return PATH_TABS[pathname] || "sites";
}

function switchTab(tab, pushState = true) {
    if (!TAB_PATHS[tab]) tab = "sites";
    document.querySelectorAll("nav button").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
    document.querySelectorAll("section").forEach(s => s.classList.toggle("active", s.id === `tab-${tab}`));
    if (pushState && location.pathname !== TAB_PATHS[tab]) {
        history.pushState({ tab }, "", TAB_PATHS[tab]);
    }
    if (tab === "systems") loadSystems();
    if (tab === "sites") loadSites();
    if (tab === "talkgroups") loadTalkgroups();
    if (tab === "lists") loadListEntries();
    if (tab === "devices") loadDevices();
}

window.addEventListener("popstate", (e) => {
    switchTab((e.state && e.state.tab) || tabFromPath(location.pathname), false);
});

function toggleAdd(id) {
    document.getElementById(id).classList.toggle("open");
}

function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
}

function formControlText(el) {
    // For <select> (e.g. the Group column), the visible option text is what
    // a human searches/sorts by -- el.value there is just the category id.
    if (el.tagName === "SELECT") return el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : "";
    return el.value;
}

function rowSearchText(tr) {
    // Built per-cell rather than from tr.textContent: a <select>'s
    // textContent includes every <option> (all 51 category names), not just
    // the selected one, which would make every row match every group name.
    let text = "";
    tr.querySelectorAll("td").forEach(td => {
        const control = td.querySelector("input, select");
        text += " " + (control ? formControlText(control) : td.textContent);
    });
    return text.toLowerCase();
}

function filterTable(tableId, query) {
    const q = query.trim().toLowerCase();
    document.querySelectorAll(`#${tableId} tbody tr`).forEach(tr => {
        tr.style.display = (!q || rowSearchText(tr).includes(q)) ? "" : "none";
    });
}

function cellSortKey(tr, col) {
    const td = tr.children[col];
    const control = td.querySelector("input, select");
    return control ? formControlText(control) : td.textContent;
}

// Attaches click-to-sort once per table header (headers persist across
// re-renders -- only tbody is replaced -- so this must be called once at
// startup, not on every load, or listeners would stack up.
function initSortableHeaders(tableId) {
    const table = document.getElementById(tableId);
    let sortState = { col: null, dir: 1 };
    table.querySelectorAll("th.sortable").forEach(th => {
        th.addEventListener("click", () => {
            const col = parseInt(th.dataset.col);
            const type = th.dataset.type;
            sortState.dir = (sortState.col === col) ? -sortState.dir : 1;
            sortState.col = col;
            table.querySelectorAll("th.sortable").forEach(h => h.classList.remove("sort-asc", "sort-desc"));
            th.classList.add(sortState.dir === 1 ? "sort-asc" : "sort-desc");

            const tbody = table.querySelector("tbody");
            const rows = Array.from(tbody.querySelectorAll("tr"));
            rows.sort((a, b) => {
                let va = cellSortKey(a, col), vb = cellSortKey(b, col);
                if (type === "num") { va = parseFloat(va) || 0; vb = parseFloat(vb) || 0; return sortState.dir * (va - vb); }
                return sortState.dir * String(va).localeCompare(String(vb));
            });
            rows.forEach(r => tbody.appendChild(r));
        });
    });
}

// ----------------------------------------------------------------- sites --

async function loadSites() {
    const [sites, lookups] = await Promise.all([api("GET", "/api/sites"), loadLookups()]);
    const systemOptions = (selectedId) => `<option value="" ${selectedId ? "" : "selected"}>(none)</option>` + systemsList.map(sy =>
        `<option value="${sy.id}" ${sy.id === selectedId ? "selected" : ""}>${esc(sy.name)}</option>`
    ).join("");

    const tbody = document.querySelector("#sites-table tbody");
    tbody.innerHTML = "";
    for (const s of sites) {
        const tr = document.createElement("tr");
        tr.draggable = true;
        tr.dataset.id = s.id;
        tr.innerHTML = `
          <td class="drag-handle">&#8942;&#8942;</td>
          <td><input value="${esc(s.sysname)}" onchange="updateSite(${s.id}, {sysname: this.value})" style="width:95%"></td>
          <td><input value="${esc(s.nac)}" onchange="updateSite(${s.id}, {nac: this.value})" style="width:5em"></td>
          <td><input type="number" value="${s.rfid ?? ""}" onchange="updateSite(${s.id}, {rfid: this.value ? parseInt(this.value) : null})" style="width:4em"></td>
          <td><input type="number" value="${s.stid ?? ""}" onchange="updateSite(${s.id}, {stid: this.value ? parseInt(this.value) : null})" style="width:4em"></td>
          <td><input value="${esc(s.control_channel_list)}" onchange="updateSite(${s.id}, {control_channel_list: this.value})" style="width:95%"></td>
          <td><select onchange="updateSite(${s.id}, {system_id: this.value ? parseInt(this.value) : null})">${systemOptions(s.system_id)}</select></td>
          <td><span class="badge ${s.active ? "active" : "inactive"}">${s.active ? "active" : "inactive"}</span></td>
          <td><input value="${esc(s.notes) || ""}" onchange="updateSite(${s.id}, {notes: this.value})" style="width:95%"></td>
          <td>
            <button class="action" ${s.active ? "" : "disabled title=\"only the active site's reload can be applied live\""} onclick="applyReload(${s.id})">Apply</button>
            <button class="action" onclick="activateSite(${s.id})">Set Active</button>
            <button class="action danger" onclick="deleteSite(${s.id})">Delete</button>
          </td>`;
        tbody.appendChild(tr);
    }
    initSitesDragReorder(tbody);
}

async function updateSite(id, patch) {
    try { await api("PUT", `/api/sites/${id}`, patch); toast("Saved"); }
    catch (e) { toast(e.message, true); }
}

let dragSrcRow = null;

function initSitesDragReorder(tbody) {
    tbody.querySelectorAll("tr").forEach(tr => {
        tr.addEventListener("dragstart", () => { dragSrcRow = tr; });
        tr.addEventListener("dragover", (e) => {
            e.preventDefault();
            tr.classList.add("drag-over");
        });
        tr.addEventListener("dragleave", () => tr.classList.remove("drag-over"));
        tr.addEventListener("drop", (e) => {
            e.preventDefault();
            tr.classList.remove("drag-over");
            if (!dragSrcRow || dragSrcRow === tr) return;
            const rows = Array.from(tbody.querySelectorAll("tr"));
            const srcIdx = rows.indexOf(dragSrcRow);
            const dstIdx = rows.indexOf(tr);
            if (srcIdx < dstIdx) tr.after(dragSrcRow);
            else tr.before(dragSrcRow);
            persistSiteOrder(tbody);
        });
    });
}

async function persistSiteOrder(tbody) {
    const order = Array.from(tbody.querySelectorAll("tr")).map(tr => parseInt(tr.dataset.id));
    try { await api("POST", "/api/sites/reorder", { order }); }
    catch (e) { toast(e.message, true); }
}

async function loadLookups() {
    [tagSets, accessLists, systemsList] = await Promise.all([
        api("GET", "/api/tag_sets"), api("GET", "/api/access_lists"), api("GET", "/api/systems"),
    ]);
    const systemTagsetSel = document.getElementById("system-tagset");
    systemTagsetSel.innerHTML = '<option value="">(none)</option>' + tagSets.map(t => `<option value="${t.id}">${t.name}</option>`).join("");
    const systemWlSel = document.getElementById("system-whitelist");
    systemWlSel.innerHTML = '<option value="">(none)</option>' + accessLists.filter(l => l.type === "whitelist").map(l => `<option value="${l.id}">${l.name}</option>`).join("");
    const systemBlSel = document.getElementById("system-blacklist");
    systemBlSel.innerHTML = '<option value="">(none)</option>' + accessLists.filter(l => l.type === "blacklist").map(l => `<option value="${l.id}">${l.name}</option>`).join("");

    const siteSystemSel = document.getElementById("site-system");
    siteSystemSel.innerHTML = '<option value="">(none)</option>' + systemsList.map(s => `<option value="${s.id}">${s.name}</option>`).join("");

    const tgPicker = document.getElementById("tgset-picker");
    tgPicker.innerHTML = tagSets.map(t => `<option value="${t.id}">${t.name}</option>`).join("");
    const listPicker = document.getElementById("list-picker");
    listPicker.innerHTML = accessLists.map(l => `<option value="${l.id}">${l.name} (${l.type})</option>`).join("");
}

async function createSite() {
    try {
        await api("POST", "/api/sites", {
            sysname: document.getElementById("site-sysname").value,
            nac: document.getElementById("site-nac").value,
            control_channel_list: document.getElementById("site-ccl").value,
            system_id: document.getElementById("site-system").value || null,
            notes: document.getElementById("site-notes").value || null,
        });
        toast("Site added");
        loadSites();
    } catch (e) { toast(e.message, true); }
}

async function deleteSite(id) {
    if (!confirm("Delete this site?")) return;
    try { await api("DELETE", `/api/sites/${id}`); toast("Deleted"); loadSites(); }
    catch (e) { toast(e.message, true); }
}

async function applyReload(id) {
    try { const r = await api("POST", `/api/sites/${id}/apply_reload`); toast(r.status); }
    catch (e) { toast(e.message, true); }
}

async function activateSite(id) {
    if (!confirm("Switch the active receiver to this site and restart op25?")) return;
    try { const r = await api("POST", `/api/sites/${id}/activate`); toast(r.status); loadSites(); }
    catch (e) { toast(e.message, true); }
}

// --------------------------------------------------------------- systems --

async function loadSystems() {
    const [systems, lookups] = await Promise.all([api("GET", "/api/systems"), loadLookups()]);
    const tagsetOptions = (selectedId) => `<option value="" ${selectedId ? "" : "selected"}>(none)</option>` + tagSets.map(t =>
        `<option value="${t.id}" ${t.id === selectedId ? "selected" : ""}>${esc(t.name)}</option>`
    ).join("");
    const accessListOptions = (type, selectedId) => `<option value="" ${selectedId ? "" : "selected"}>(none)</option>` +
        accessLists.filter(l => l.type === type).map(l =>
            `<option value="${l.id}" ${l.id === selectedId ? "selected" : ""}>${esc(l.name)}</option>`
        ).join("");

    const tbody = document.querySelector("#systems-table tbody");
    tbody.innerHTML = "";
    for (const sy of systems) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td><input value="${esc(sy.name)}" onchange="updateSystem(${sy.id}, {name: this.value})" style="width:95%"></td>
          <td><select onchange="updateSystem(${sy.id}, {tag_set_id: this.value ? parseInt(this.value) : null})">${tagsetOptions(sy.tag_set_id)}</select></td>
          <td><select onchange="updateSystem(${sy.id}, {whitelist_id: this.value ? parseInt(this.value) : null})">${accessListOptions("whitelist", sy.whitelist_id)}</select></td>
          <td><select onchange="updateSystem(${sy.id}, {blacklist_id: this.value ? parseInt(this.value) : null})">${accessListOptions("blacklist", sy.blacklist_id)}</select></td>
          <td><input value="${esc(sy.notes) || ""}" onchange="updateSystem(${sy.id}, {notes: this.value})" style="width:95%"></td>
          <td style="text-align:center;" title="Automatic handoff to a neighbor site (within this system) when the active site's signal degrades -- needs a dedicated 'scout' channel configured on a second tuner.">
            <input type="checkbox" ${sy.roaming_enabled ? "checked" : ""} onchange="updateSystem(${sy.id}, {roaming_enabled: this.checked})">
          </td>
          <td><input type="number" min="1" placeholder="10" value="${sy.roaming_stale_seconds ?? ""}" style="width:60px"
                onchange="updateSystem(${sy.id}, {roaming_stale_seconds: this.value ? parseInt(this.value) : null})"></td>
          <td><button class="action danger" onclick="deleteSystem(${sy.id})">Delete</button></td>`;
        tbody.appendChild(tr);
    }
}

async function updateSystem(id, patch) {
    try { await api("PUT", `/api/systems/${id}`, patch); toast("Saved"); }
    catch (e) { toast(e.message, true); }
}

async function createSystem() {
    try {
        await api("POST", "/api/systems", {
            name: document.getElementById("system-name").value,
            tag_set_id: document.getElementById("system-tagset").value || null,
            whitelist_id: document.getElementById("system-whitelist").value || null,
            blacklist_id: document.getElementById("system-blacklist").value || null,
            notes: document.getElementById("system-notes").value || null,
        });
        toast("System added");
        loadSystems();
    } catch (e) { toast(e.message, true); }
}

async function deleteSystem(id) {
    if (!confirm("Delete this system? Any sites using it will become unassigned, not deleted.")) return;
    try { await api("DELETE", `/api/systems/${id}`); toast("Deleted"); loadSystems(); }
    catch (e) { toast(e.message, true); }
}

// -------------------------------------------------------- import/export --

async function importConfig(inputEl) {
    const file = inputEl.files[0];
    inputEl.value = ""; // reset so picking the same file again still fires onchange
    if (!file) return;
    if (!confirm(
        `Import ${file.name}? This REPLACES every system, talkgroup, group, and access ` +
        "list currently configured, and restarts op25 to apply it. The current config " +
        "is backed up on disk first, but there's no undo from this UI."
    )) return;

    toast("Importing...");
    try {
        const resp = await fetch("/api/import", { method: "POST", body: file });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        toast(data.status);
        setTimeout(() => location.reload(), 2000);
    } catch (e) {
        toast(e.message, true);
    }
}

// ----------------------------------------------------------- talkgroups --

const NEW_CATEGORY_SENTINEL = "__new__";

function categorySelectOptions(selectedId) {
    let opts = `<option value="">(none)</option>`;
    for (const c of categories) {
        opts += `<option value="${c.id}" ${String(c.id) === String(selectedId) ? "selected" : ""}>${esc(c.name)}</option>`;
    }
    opts += `<option value="${NEW_CATEGORY_SENTINEL}">+ New group...</option>`;
    return opts;
}

async function loadTalkgroups() {
    const tagSetId = document.getElementById("tgset-picker").value;
    if (!tagSetId) return;
    await loadCategories(tagSetId);
    const tgs = await api("GET", `/api/tag_sets/${tagSetId}/talkgroups`);
    const tbody = document.querySelector("#talkgroups-table tbody");
    tbody.innerHTML = "";
    for (const tg of tgs) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="mono">${tg.tgid}</td>
          <td><input value="${esc(tg.name)}" onchange="updateTalkgroup(${tg.id}, {name: this.value})" style="width:95%"></td>
          <td><select onchange="onCategorySelectChange(this, ${tg.id})">${categorySelectOptions(tg.category_id)}</select></td>
          <td><input value="${tg.priority ?? ""}" onchange="updateTalkgroup(${tg.id}, {priority: this.value ? parseInt(this.value) : null})" style="width:4em"></td>
          <td><button class="action danger" onclick="deleteTalkgroup(${tg.id})">Delete</button></td>`;
        tbody.appendChild(tr);
    }
}

async function onCategorySelectChange(selectEl, tgId) {
    if (selectEl.value === NEW_CATEGORY_SENTINEL) {
        const name = prompt("New group name:");
        if (!name) { selectEl.value = ""; return; }
        try {
            await updateTalkgroup(tgId, { category_name: name });
            await loadTalkgroups();
        } catch (e) { toast(e.message, true); }
        return;
    }
    updateTalkgroup(tgId, { category_id: selectEl.value ? parseInt(selectEl.value) : null });
}

async function createTalkgroup() {
    const tagSetId = document.getElementById("tgset-picker").value;
    if (!tagSetId) { toast("Pick a tag set first", true); return; }
    const catVal = document.getElementById("tg-category").value;
    try {
        await api("POST", `/api/tag_sets/${tagSetId}/talkgroups`, {
            tgid: parseInt(document.getElementById("tg-tgid").value),
            name: document.getElementById("tg-name").value,
            category_id: catVal ? parseInt(catVal) : null,
            priority: document.getElementById("tg-prio").value ? parseInt(document.getElementById("tg-prio").value) : null,
        });
        toast("Talkgroup added");
        loadTalkgroups();
    } catch (e) { toast(e.message, true); }
}

async function updateTalkgroup(id, patch) {
    try { await api("PUT", `/api/talkgroups/${id}`, patch); toast("Saved"); }
    catch (e) { toast(e.message, true); }
}

async function deleteTalkgroup(id) {
    if (!confirm("Delete this talkgroup?")) return;
    try { await api("DELETE", `/api/talkgroups/${id}`); toast("Deleted"); loadTalkgroups(); }
    catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------ categories --

async function loadCategories(tagSetId) {
    categories = await api("GET", `/api/tag_sets/${tagSetId}/categories`);
    document.getElementById("tg-category").innerHTML = categorySelectOptions(null);
    const tbody = document.querySelector("#categories-table tbody");
    tbody.innerHTML = "";
    for (const c of categories) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td><input value="${esc(c.name)}" onchange="renameCategory(${c.id}, this.value)" style="width:95%"></td>
          <td>${c.talkgroup_count}</td>
          <td><button class="action danger" onclick="deleteCategory(${c.id})">Delete</button></td>`;
        tbody.appendChild(tr);
    }
}

async function createCategory() {
    const tagSetId = document.getElementById("tgset-picker").value;
    const name = document.getElementById("new-cat-name").value;
    if (!name) return;
    try {
        await api("POST", `/api/tag_sets/${tagSetId}/categories`, { name });
        document.getElementById("new-cat-name").value = "";
        toast("Group added");
        await loadCategories(tagSetId);
    } catch (e) { toast(e.message, true); }
}

async function renameCategory(id, name) {
    try {
        await api("PUT", `/api/categories/${id}`, { name });
        toast("Group renamed -- all its talkgroups updated");
        loadTalkgroups();
    } catch (e) { toast(e.message, true); }
}

async function deleteCategory(id) {
    if (!confirm("Delete this group? Talkgroups using it will just lose their group, not be deleted.")) return;
    try {
        await api("DELETE", `/api/categories/${id}`);
        toast("Group deleted");
        loadTalkgroups();
    } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------- access lists --

async function loadListEntries() {
    const listId = document.getElementById("list-picker").value;
    if (!listId) return;
    const entries = await api("GET", `/api/access_lists/${listId}/entries`);
    const tbody = document.querySelector("#lists-table tbody");
    tbody.innerHTML = "";
    for (const e of entries) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="mono">${e.tgid}</td>
          <td class="mono">${e.tgid_end ?? "-"}</td>
          <td><button class="action danger" onclick="deleteListEntry(${e.id})">Delete</button></td>`;
        tbody.appendChild(tr);
    }
}

async function createListEntry() {
    const listId = document.getElementById("list-picker").value;
    if (!listId) { toast("Pick a list first", true); return; }
    try {
        const end = document.getElementById("entry-tgid-end").value;
        await api("POST", `/api/access_lists/${listId}/entries`, {
            tgid: parseInt(document.getElementById("entry-tgid").value),
            tgid_end: end ? parseInt(end) : null,
        });
        toast("Entry added");
        loadListEntries();
    } catch (e) { toast(e.message, true); }
}

async function deleteListEntry(id) {
    if (!confirm("Delete this entry?")) return;
    try { await api("DELETE", `/api/access_list_entries/${id}`); toast("Deleted"); loadListEntries(); }
    catch (e) { toast(e.message, true); }
}

async function createAccessList() {
    try {
        await api("POST", "/api/access_lists", {
            name: document.getElementById("new-list-name").value,
            type: document.getElementById("new-list-type").value,
        });
        toast("List added");
        await loadLookups();
    } catch (e) { toast(e.message, true); }
}

// -------------------------------------------------------------- devices --

async function loadDevices() {
    const [devices, channels, sites] = await Promise.all([
        api("GET", "/api/devices"), api("GET", "/api/channels"), api("GET", "/api/sites"),
    ]);

    const devTbody = document.querySelector("#devices-table tbody");
    devTbody.innerHTML = "";
    for (const d of devices) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td><input value="${esc(d.name)}" onchange="updateDevice(${d.id}, {name: this.value})" style="width:95%"></td>
          <td><input value="${esc(d.args)}" onchange="updateDevice(${d.id}, {args: this.value})" style="width:95%"></td>
          <td><input value="${esc(d.gains)}" onchange="updateDevice(${d.id}, {gains: this.value})" style="width:95%"></td>
          <td><input value="${d.ppm}" type="number" onchange="updateDevice(${d.id}, {ppm: parseInt(this.value)})" style="width:4em"></td>
          <td><input value="${d.rate}" type="number" onchange="updateDevice(${d.id}, {rate: parseInt(this.value)})" style="width:6em"></td>
          <td><input value="${d.usable_bw_pct}" type="number" step="0.01" onchange="updateDevice(${d.id}, {usable_bw_pct: parseFloat(this.value)})" style="width:4em"></td>
          <td><input type="checkbox" ${d.tunable ? "checked" : ""} onchange="updateDevice(${d.id}, {tunable: this.checked})"></td>
          <td><button class="action danger" onclick="deleteDevice(${d.id})">Delete</button></td>`;
        devTbody.appendChild(tr);
    }

    document.getElementById("chan-device").innerHTML = devices.map(d => `<option value="${d.id}">${esc(d.name)}</option>`).join("");
    document.getElementById("chan-site").innerHTML = `<option value="">(none)</option>` + sites.map(s => `<option value="${s.id}">${esc(s.sysname)}</option>`).join("");

    const deviceOptions = (selectedId) => devices.map(d =>
        `<option value="${d.id}" ${d.id === selectedId ? "selected" : ""}>${esc(d.name)}</option>`
    ).join("");
    const siteOptions = (selectedId) => `<option value="" ${selectedId ? "" : "selected"}>(none)</option>` + sites.map(s =>
        `<option value="${s.id}" ${s.id === selectedId ? "selected" : ""}>${esc(s.sysname)}</option>`
    ).join("");

    const roleOptions = (selected) => ["primary", "scout"].map(r =>
        `<option value="${r}" ${r === selected ? "selected" : ""}>${r}</option>`
    ).join("");

    const chanTbody = document.querySelector("#channels-table tbody");
    chanTbody.innerHTML = "";
    for (const c of channels) {
        const isScout = c.role === "scout";
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td><input value="${esc(c.name)}" onchange="updateChannel(${c.id}, {name: this.value})" style="width:95%"></td>
          <td><select onchange="updateChannel(${c.id}, {device_id: parseInt(this.value)})">${deviceOptions(c.device_id)}</select></td>
          <td title="'scout' is roaming's dedicated neighbor-evaluation receiver -- never voice-eligible, never has a fixed site (the roaming coordinator points it dynamically).">
            <select onchange="updateChannel(${c.id}, {role: this.value})">${roleOptions(c.role)}</select>
          </td>
          <td style="${isScout ? "opacity:0.5;" : ""}" title="${isScout ? "Ignored for a scout channel -- its target is set dynamically by the roaming coordinator." : ""}">
            <select onchange="updateChannel(${c.id}, {trunking_system_id: this.value ? parseInt(this.value) : null})">${siteOptions(c.trunking_system_id)}</select>
          </td>
          <td><input value="${esc(c.demod_type)}" onchange="updateChannel(${c.id}, {demod_type: this.value})" style="width:5em"></td>
          <td><input value="${esc(c.destination)}" onchange="updateChannel(${c.id}, {destination: this.value})" style="width:95%"></td>
          <td><input value="${esc(c.enable_analog)}" onchange="updateChannel(${c.id}, {enable_analog: this.value})" style="width:4em"></td>
          <td><button class="action danger" onclick="deleteChannel(${c.id})">Delete</button></td>`;
        chanTbody.appendChild(tr);
    }
}

async function createDevice() {
    try {
        await api("POST", "/api/devices", {
            name: document.getElementById("dev-name").value,
            args: document.getElementById("dev-args").value,
            gains: document.getElementById("dev-gains").value,
            ppm: parseInt(document.getElementById("dev-ppm").value) || 0,
            rate: parseInt(document.getElementById("dev-rate").value) || 1000000,
        });
        toast("Device added -- restart op25 to use it");
        loadDevices();
    } catch (e) { toast(e.message, true); }
}

async function updateDevice(id, patch) {
    try { await api("PUT", `/api/devices/${id}`, patch); toast("Saved -- restart op25 to apply"); }
    catch (e) { toast(e.message, true); }
}

async function deleteDevice(id) {
    if (!confirm("Delete this device? Any channel using it will need a new device assigned.")) return;
    try { await api("DELETE", `/api/devices/${id}`); toast("Deleted"); loadDevices(); }
    catch (e) { toast(e.message, true); }
}

async function createChannel() {
    const deviceId = document.getElementById("chan-device").value;
    if (!deviceId) { toast("Add a device first", true); return; }
    try {
        await api("POST", "/api/channels", {
            name: document.getElementById("chan-name").value,
            device_id: parseInt(deviceId),
            trunking_system_id: document.getElementById("chan-site").value ? parseInt(document.getElementById("chan-site").value) : null,
            destination: document.getElementById("chan-destination").value,
        });
        toast("Channel added -- restart op25 to use it");
        loadDevices();
    } catch (e) { toast(e.message, true); }
}

async function updateChannel(id, patch) {
    try { await api("PUT", `/api/channels/${id}`, patch); toast("Saved -- restart op25 to apply"); }
    catch (e) { toast(e.message, true); }
}

async function deleteChannel(id) {
    if (!confirm("Delete this channel?")) return;
    try { await api("DELETE", `/api/channels/${id}`); toast("Deleted"); loadDevices(); }
    catch (e) { toast(e.message, true); }
}

// --------------------------------------------------------------- startup --

document.querySelectorAll("nav button").forEach(b => {
    b.addEventListener("click", () => switchTab(b.dataset.tab));
});

initSortableHeaders("talkgroups-table");

const initialTab = tabFromPath(location.pathname);
if (initialTab === "systems") {
    switchTab(initialTab, false);
} else {
    // loadSystems() is what actually populates tagSets/accessLists and the
    // tgset-picker/list-picker <select>s (via loadLookups() inside it) --
    // needed before rendering any other tab, so a hard refresh landing
    // directly on /talkgroups or /access-lists doesn't show an empty picker.
    loadSystems().then(() => switchTab(initialTab, false));
}
