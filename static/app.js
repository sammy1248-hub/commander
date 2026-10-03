const API = "";  // same origin - FastAPI serves this page
const GESTURES = ["space", "vol_up", "vol_down", "left_click", "right_click"];
const ACTION_LABELS = {
    space: "PLAY / PAUSE",
    vol_up: "VOLUME UP",
    vol_down: "VOLUME DOWN",
    left_click: "LEFT CLICK",
    right_click: "RIGHT CLICK",
    None: "-",
};
const GESTURE_ICONS = {
    space: "\u270B",
    vol_up: "\uD83D\uDC4D",
    vol_down: "\uD83D\uDC4E",
    left_click: "\uD83E\uDD1F",
    right_click: "\u270C\uFE0F",
    None: "-",
};

let lastGesture = null;
let lastLogSignature = null;

const el = (id) => document.getElementById(id);

function feedback(msg, ok = true) {
    const fb = el("fb");
    fb.textContent = msg;
    fb.className = "status-line " + (ok ? "ok" : "err");
}

function setDot(id, state) {
    // state: "off" | "on" | "warn" | "error"
    const dot = el(id);
    dot.className = "status-dot" + (state === "off" ? "" : ` ${state}`);
}

function renderLog(entries) {
    if (!entries) return;
    const sig = entries.length ? `${entries.length}:${entries[entries.length - 1].time}:${entries[entries.length - 1].value}` : "0";
    if (sig === lastLogSignature) return;  // nothing new - skip the DOM churn
    lastLogSignature = sig;

    const logEl = el("log");
    logEl.innerHTML = entries.map(e =>
        `<div class="log-entry"><span class="t">${e.time}</span><span class="k ${e.kind}">${e.kind}</span><span class="v">${e.value}</span></div>`
    ).join("");
}

function applyEngineState(data) {
    const { engine_state: state, error } = data;
    const btn = el("engine-btn");
    const stateEl = el("engine-state");
    const errEl = el("engine-error");
    const overlay = el("camera-overlay");

    btn.disabled = false;
    btn.classList.remove("running", "error");
    errEl.textContent = "";

    switch (state) {
        case "offline":
            btn.textContent = "START ENGINE";
            stateEl.textContent = "OFFLINE";
            overlay.classList.remove("hidden");
            overlay.textContent = "ENGINE OFFLINE";
            break;
        case "starting":
            btn.textContent = "STARTING...";
            btn.disabled = true;
            stateEl.textContent = "STARTING";
            overlay.classList.remove("hidden");
            overlay.textContent = "STARTING ENGINE...";
            break;
        case "active":
            btn.textContent = "STOP ENGINE";
            btn.classList.add("running");
            stateEl.textContent = "ACTIVE";
            overlay.classList.add("hidden");
            break;
        case "stopping":
            btn.textContent = "STOPPING...";
            btn.disabled = true;
            stateEl.textContent = "STOPPING";
            overlay.classList.remove("hidden");
            overlay.textContent = "STOPPING ENGINE...";
            break;
        case "error":
            btn.textContent = "RETRY";
            btn.classList.add("error");
            stateEl.textContent = "ERROR";
            errEl.textContent = error || "Unknown error";
            overlay.classList.remove("hidden");
            overlay.textContent = "CAMERA ERROR";
            break;
    }

    // System-status row
    setDot("dot-engine", state === "active" ? "on" : state === "starting" ? "warn" : state === "error" ? "error" : "off");
    el("val-engine").textContent = state.toUpperCase();

    setDot("dot-camera", state === "active" ? "on" : state === "error" ? "error" : "off");
    el("val-camera").textContent = state === "active" ? "ACTIVE" : state === "error" ? "ERROR" : "OFFLINE";

    const handOn = state === "active" && data.hand_detected;
    setDot("dot-hand", handOn ? "on" : state === "active" ? "warn" : "off");
    el("val-hand").textContent = handOn ? "ACTIVE" : state === "active" ? "IDLE" : "OFFLINE";
}

async function pollStatus() {
    try {
        const res = await fetch(`${API}/api/status`);
        if (!res.ok) throw new Error("bad status");
        const data = await res.json();

        setDot("dot-backend", "on");
        el("val-backend").textContent = "ONLINE";

        applyEngineState(data);

        const g = data.gesture || "None";
        if (g !== lastGesture) {
            const icon = el("gesture-icon");
            const label = el("gesture-label");
            icon.classList.add("changing");
            label.classList.add("changing");
            setTimeout(() => {
                icon.textContent = GESTURE_ICONS[g] || "-";
                label.textContent = g === "None" ? "NO GESTURE" : g.toUpperCase().replace("_", " ");
                el("action-label").textContent = ACTION_LABELS[g] || "-";
                icon.classList.remove("changing");
                label.classList.remove("changing");
            }, 90);
            lastGesture = g;
        }

        el("cursor-x").textContent = data.cursor_x.toFixed(3);
        el("cursor-y").textContent = data.cursor_y.toFixed(3);
        el("pointer-state").textContent = data.pointer_active ? "ACTIVE" : "LOCKED";

        GESTURES.forEach(name => {
            el(`card-${name}`).classList.toggle(`active-${name}`, g === name);
        });

        renderLog(data.log);
    } catch (err) {
        setDot("dot-backend", "error");
        el("val-backend").textContent = "OFFLINE";
        setDot("dot-engine", "off");
        setDot("dot-camera", "off");
        setDot("dot-hand", "off");
        feedback("Backend unreachable - is app.py running?", false);
        // No page refresh needed: the next poll (500ms later) will pick
        // the connection back up automatically once the backend returns.
    }
}

async function toggleEngine() {
    const btn = el("engine-btn");
    const starting = btn.textContent.includes("START") || btn.textContent.includes("RETRY");
    const endpoint = starting ? "start" : "stop";
    try {
        const res = await fetch(`${API}/api/engine/${endpoint}`, { method: "POST" });
        const data = await res.json();
        feedback(`Engine ${data.status.replace(/_/g, " ")}`);
    } catch {
        feedback("Could not reach engine control endpoint", false);
    }
}

async function runAction(key) {
    try {
        const res = await fetch(`${API}/api/action/${key}`, { method: "POST" });
        const data = await res.json();
        if (data.status === "success") {
            feedback(`ACTION: ${key.toUpperCase()}`);
        } else {
            feedback("Action failed", false);
        }
    } catch {
        feedback("Connection lost", false);
    }
}

async function loadSettings() {
    try {
        const res = await fetch(`${API}/api/settings`);
        const s = await res.json();
        el("smoothing").value = s.cursor_smoothing;
        el("smoothing-val").textContent = s.cursor_smoothing.toFixed(2);
        el("margin").value = s.cursor_margin;
        el("margin-val").textContent = s.cursor_margin.toFixed(2);
        el("confirm").value = s.confirm_frames;
        el("confirm-val").textContent = s.confirm_frames;
    } catch {
        // Non-fatal - sliders just keep their defaults until reachable.
    }
}

let settingsDebounce = null;
function pushSettings() {
    clearTimeout(settingsDebounce);
    settingsDebounce = setTimeout(async () => {
        const payload = {
            cursor_smoothing: parseFloat(el("smoothing").value),
            cursor_margin: parseFloat(el("margin").value),
            confirm_frames: parseInt(el("confirm").value, 10),
        };
        try {
            const res = await fetch(`${API}/api/settings`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!res.ok) throw new Error("save failed");
        } catch {
            feedback("Could not save settings - backend unreachable", false);
        }
    }, 250);
}

function wireSliders() {
    el("smoothing").addEventListener("input", (e) => {
        el("smoothing-val").textContent = parseFloat(e.target.value).toFixed(2);
        pushSettings();
    });
    el("margin").addEventListener("input", (e) => {
        el("margin-val").textContent = parseFloat(e.target.value).toFixed(2);
        pushSettings();
    });
    el("confirm").addEventListener("input", (e) => {
        el("confirm-val").textContent = e.target.value;
        pushSettings();
    });
}

window.addEventListener("DOMContentLoaded", () => {
    el("engine-btn").addEventListener("click", toggleEngine);
    GESTURES.forEach(name => {
        el(`card-${name}`).addEventListener("click", () => runAction(name));
    });
    wireSliders();
    loadSettings();
    pollStatus();
    setInterval(pollStatus, 500);

    // If the MJPEG stream errors (e.g. before the engine starts producing
    // frames), retry it periodically instead of leaving a broken image icon.
    const feedImg = el("feed");
    feedImg.addEventListener("error", () => {
        setTimeout(() => { feedImg.src = "/video_feed?_=" + Date.now(); }, 1500);
    });
});