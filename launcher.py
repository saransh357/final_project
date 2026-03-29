"""
CryptoAPI Launcher
==================
Double-click this file (or run: python launcher.py)

- Starts pendulum_key_gen.py (SARA) on port 5000
- Waits for SARA to emit its API token
- Injects the token into app.py automatically
- Starts app.py on port 8000
- Opens a browser control panel so you can monitor everything
- No terminal interaction needed
"""

import os
import sys
import re
import time
import signal
import threading
import subprocess
import webbrowser
from pathlib import Path
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS

# ── Paths ────────────────────────────────────────────────────────────────────

BASE_DIR        = Path(__file__).parent.resolve()
SARA_SCRIPT     = BASE_DIR / "pendulum_key_gen.py"
APP_SCRIPT      = BASE_DIR / "app.py"
LAUNCHER_PORT   = 9000
PYTHON          = sys.executable

# ── State ────────────────────────────────────────────────────────────────────

state = {
    "sara_proc":    None,
    "app_proc":     None,
    "sara_token":   None,
    "sara_status":  "stopped",   # stopped | starting | waiting_token | running | error
    "app_status":   "stopped",
    "sara_logs":    [],
    "app_logs":     [],
    "launcher_logs":[],
    "sara_port":    5000,
    "app_port":     8000,
    "started_at":   None,
}

TOKEN_RE = re.compile(r"Bearer token[:\s]+(\S+)", re.IGNORECASE)
ALT_TOKEN_RE = re.compile(r"API Token[^:]*:\s*(\S+)", re.IGNORECASE)

MAX_LOG_LINES = 300

def add_log(buf_key, line, level="info"):
    ts = datetime.now().strftime("%H:%M:%S")
    state[buf_key].append({"ts": ts, "msg": line.rstrip(), "level": level})
    if len(state[buf_key]) > MAX_LOG_LINES:
        state[buf_key].pop(0)

def launcher_log(msg, level="info"):
    add_log("launcher_logs", msg, level)
    print(f"[Launcher] {msg}")

# ── SARA process ─────────────────────────────────────────────────────────────

def start_sara():
    if state["sara_proc"] and state["sara_proc"].poll() is None:
        launcher_log("SARA already running", "warn")
        return

    if not SARA_SCRIPT.exists():
        launcher_log(f"pendulum_key_gen.py not found at {SARA_SCRIPT}", "error")
        state["sara_status"] = "error"
        return

    launcher_log("Starting SARA (pendulum_key_gen.py)…")
    state["sara_status"] = "starting"
    state["sara_token"]  = None

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [PYTHON, str(SARA_SCRIPT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=str(BASE_DIR),
        env=env,
    )
    state["sara_proc"] = proc
    state["started_at"] = datetime.now().isoformat()

    threading.Thread(target=_read_sara_output, args=(proc,), daemon=True).start()


def _read_sara_output(proc):
    """Read SARA stdout line by line, extract token, auto-answer prompts."""
    waiting_for_api_prompt = False

    for line in proc.stdout:
        line = line.rstrip()
        add_log("sara_logs", line)

        # Detect when key derivation is complete and SARA asks about the API
        if "Start REST API server" in line or "start rest api" in line.lower():
            waiting_for_api_prompt = True
            state["sara_status"] = "waiting_token"
            launcher_log("SARA ready — auto-answering API prompt…")
            try:
                proc.stdin.write("y\n")
                proc.stdin.flush()
            except Exception as e:
                launcher_log(f"Could not answer SARA prompt: {e}", "error")

        # Extract token from SARA output
        if state["sara_token"] is None:
            for pattern in (TOKEN_RE, ALT_TOKEN_RE):
                m = pattern.search(line)
                if m:
                    token = m.group(1).strip()
                    state["sara_token"] = token
                    state["sara_status"] = "running"
                    launcher_log(f"SARA token captured ✓", "ok")
                    # Now start the API app
                    threading.Thread(target=start_app, daemon=True).start()
                    break

        # Also handle "Save key to file?" prompt
        if "Save key to file" in line:
            try:
                proc.stdin.write("n\n")
                proc.stdin.flush()
            except Exception:
                pass

        if proc.poll() is not None:
            break

    code = proc.poll()
    if state["sara_status"] != "stopped":
        state["sara_status"] = "error" if code != 0 else "stopped"
        launcher_log(f"SARA exited (code {code})", "warn" if code == 0 else "error")


# ── App process ───────────────────────────────────────────────────────────────

def start_app():
    if state["app_proc"] and state["app_proc"].poll() is None:
        launcher_log("API app already running", "warn")
        return

    if not APP_SCRIPT.exists():
        launcher_log(f"app.py not found at {APP_SCRIPT}", "error")
        state["app_status"] = "error"
        return

    # Wait up to 10s for SARA token
    for _ in range(20):
        if state["sara_token"]:
            break
        time.sleep(0.5)

    if not state["sara_token"]:
        launcher_log("Timed out waiting for SARA token", "error")
        state["app_status"] = "error"
        return

    launcher_log(f"Starting app.py with SARA token…")
    state["app_status"] = "starting"

    env = os.environ.copy()
    env["SARA_TOKEN"]       = state["sara_token"]
    env["SARA_URL"]         = f"http://localhost:{state['sara_port']}"
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [PYTHON, str(APP_SCRIPT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(BASE_DIR),
        env=env,
    )
    state["app_proc"] = proc
    threading.Thread(target=_read_app_output, args=(proc,), daemon=True).start()


def _read_app_output(proc):
    for line in proc.stdout:
        line = line.rstrip()
        add_log("app_logs", line)
        if "Running on" in line and "8000" in line:
            state["app_status"] = "running"
            launcher_log("API portal running at http://localhost:8000 ✓", "ok")
    code = proc.poll()
    if state["app_status"] != "stopped":
        state["app_status"] = "error" if code != 0 else "stopped"
        launcher_log(f"app.py exited (code {code})", "warn" if code == 0 else "error")


# ── Stop ─────────────────────────────────────────────────────────────────────

def stop_all():
    for key in ("app_proc", "sara_proc"):
        proc = state[key]
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    state["sara_status"] = "stopped"
    state["app_status"]  = "stopped"
    state["sara_token"]  = None
    launcher_log("All services stopped")


# ── Launcher Flask API ────────────────────────────────────────────────────────

launcher_app = Flask("launcher", template_folder=str(BASE_DIR / "templates"))
CORS(launcher_app)


@launcher_app.route("/launcher/start", methods=["POST"])
def api_start():
    threading.Thread(target=start_sara, daemon=True).start()
    return jsonify({"message": "Starting…"})


@launcher_app.route("/launcher/stop", methods=["POST"])
def api_stop():
    threading.Thread(target=stop_all, daemon=True).start()
    return jsonify({"message": "Stopping…"})


@launcher_app.route("/launcher/status")
def api_status():
    def proc_alive(p):
        return p is not None and p.poll() is None

    return jsonify({
        "sara_status":  state["sara_status"],
        "app_status":   state["app_status"],
        "sara_alive":   proc_alive(state["sara_proc"]),
        "app_alive":    proc_alive(state["app_proc"]),
        "sara_token":   "***" if state["sara_token"] else None,
        "token_ready":  bool(state["sara_token"]),
        "started_at":   state["started_at"],
        "sara_port":    state["sara_port"],
        "app_port":     state["app_port"],
    })


@launcher_app.route("/launcher/logs")
def api_logs():
    since = int(request.args.get("since", 0))
    return jsonify({
        "launcher": state["launcher_logs"][since:],
        "sara":     state["sara_logs"][max(0, len(state["sara_logs"])-80):],
        "app":      state["app_logs"][max(0, len(state["app_logs"])-80):],
    })


@launcher_app.route("/")
def index():
    return LAUNCHER_HTML


# ── Launcher HTML ─────────────────────────────────────────────────────────────

LAUNCHER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CryptoAPI Launcher</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;1,300&family=DM+Mono:wght@300;400&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --ink:#080a0e;--ink2:#0f1116;--ink3:#161a22;
  --gold:#c9a84c;--gold-dim:#7a5f28;--gold-pale:#e8d5a3;
  --silver:#7a8190;--silver-lt:#b8c0cc;--white:#ededea;
  --green:#2d7a5a;--red:#b83228;--border:rgba(201,168,76,.12);
  --border2:rgba(201,168,76,.28);--serif:'Cormorant Garamond',serif;
  --mono:'DM Mono',monospace;--ease:220ms cubic-bezier(.4,0,.2,1);
}
body{background:var(--ink);color:var(--white);font-family:var(--mono);font-size:12px;font-weight:300;line-height:1.6;min-height:100vh;display:flex;flex-direction:column}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:999;opacity:.5;
  background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.035'/%3E%3C/svg%3E")}

header{background:var(--ink2);border-bottom:1px solid var(--border);padding:0 28px;height:54px;display:flex;align-items:center;gap:0;flex-shrink:0}
.wm{display:flex;flex-direction:column;line-height:1;margin-right:28px}
.wm-t{font-family:var(--serif);font-size:19px;font-weight:300;font-style:italic;color:var(--gold)}
.wm-s{font-size:8px;letter-spacing:.22em;text-transform:uppercase;color:var(--silver);margin-top:2px}
.hdr-sep{width:1px;height:22px;background:var(--border);margin:0 20px}
.hdr-r{margin-left:auto;display:flex;align-items:center;gap:12px}

.content{display:grid;grid-template-columns:320px 1fr;gap:0;flex:1;overflow:hidden}

/* sidebar */
.sidebar{background:var(--ink2);border-right:1px solid var(--border);padding:24px 20px;display:flex;flex-direction:column;gap:20px;overflow-y:auto}

.section-label{font-size:8px;letter-spacing:.24em;text-transform:uppercase;color:var(--gold-dim);margin-bottom:10px}

/* service card */
.svc-card{background:var(--ink3);border:1px solid var(--border);border-radius:2px;padding:16px}
.svc-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.svc-name{font-size:13px;color:var(--white);letter-spacing:.04em}
.svc-port{font-size:10px;color:var(--silver);letter-spacing:.06em}

.status-row{display:flex;align-items:center;gap:8px;margin-bottom:12px}
.beacon{width:7px;height:7px;border-radius:50%;background:var(--silver);flex-shrink:0;transition:all .3s}
.beacon.running{background:var(--gold);box-shadow:0 0 8px rgba(201,168,76,.5);animation:pulse 2s ease-in-out infinite}
.beacon.starting{background:#d4a843;animation:blink .7s ease-in-out infinite}
.beacon.error{background:var(--red)}
.status-text{font-size:11px;color:var(--silver);letter-spacing:.05em}
.status-text.running{color:var(--gold)}
.status-text.error{color:#e07060}

@keyframes pulse{0%,100%{box-shadow:0 0 8px rgba(201,168,76,.5)}50%{box-shadow:0 0 16px rgba(201,168,76,.8)}}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.4}}

.svc-detail{font-size:10px;color:var(--silver);line-height:1.8;letter-spacing:.03em}
.svc-detail span{color:var(--gold-pale)}

/* big launch button */
.launch-btn{
  width:100%;padding:14px;border:1px solid var(--gold-dim);border-radius:1px;
  background:rgba(201,168,76,.06);color:var(--gold);font-family:var(--mono);
  font-size:13px;letter-spacing:.08em;cursor:pointer;transition:all var(--ease);
  display:flex;align-items:center;justify-content:center;gap:10px;
}
.launch-btn:hover{background:rgba(201,168,76,.12);border-color:var(--gold)}
.launch-btn:active{transform:scale(.98)}
.launch-btn.danger{border-color:rgba(184,50,40,.4);color:#e07060;background:rgba(184,50,40,.05)}
.launch-btn.danger:hover{background:rgba(184,50,40,.1)}
.launch-btn:disabled{opacity:.4;cursor:not-allowed;transform:none}

/* open portal button */
.portal-btn{
  width:100%;padding:11px;border:1px solid var(--border2);border-radius:1px;
  background:transparent;color:var(--silver-lt);font-family:var(--mono);
  font-size:11px;letter-spacing:.07em;cursor:pointer;transition:all var(--ease);
  display:flex;align-items:center;justify-content:center;gap:8px;
}
.portal-btn:hover{color:var(--gold);border-color:var(--gold-dim);background:rgba(201,168,76,.04)}
.portal-btn:disabled{opacity:.35;cursor:not-allowed}

/* progress steps */
.steps{display:flex;flex-direction:column;gap:6px}
.step{display:flex;align-items:center;gap:10px;padding:8px 12px;border-radius:2px;font-size:11px;color:var(--silver);letter-spacing:.04em;transition:all .3s}
.step.done{color:#6abf8a;background:rgba(45,122,90,.06)}
.step.active{color:var(--gold);background:rgba(201,168,76,.05);animation:step-pulse 1.5s ease-in-out infinite}
.step.error{color:#e07060;background:rgba(184,50,40,.05)}
.step-icon{width:18px;text-align:center;font-size:12px;flex-shrink:0}
@keyframes step-pulse{0%,100%{opacity:1}50%{opacity:.6}}

/* log pane */
.log-area{flex:1;background:var(--ink);overflow:hidden;display:flex;flex-direction:column}
.log-tabs{display:flex;background:var(--ink2);border-bottom:1px solid var(--border);padding:0 20px;flex-shrink:0}
.log-tab{padding:10px 16px;font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--silver);cursor:pointer;border-bottom:2px solid transparent;transition:all var(--ease)}
.log-tab:hover{color:var(--white)}
.log-tab.active{color:var(--gold);border-bottom-color:var(--gold)}
.log-pane{display:none;flex:1;overflow-y:auto;padding:14px 20px;font-size:11px;line-height:2;flex-direction:column}
.log-pane.active{display:flex}
.log-line{display:flex;gap:12px;min-height:20px}
.log-ts{color:rgba(122,129,144,.4);flex-shrink:0;font-size:10px;padding-top:2px;min-width:56px}
.log-msg{color:var(--silver-lt);word-break:break-all}
.log-msg.ok{color:#6abf8a}
.log-msg.warn{color:#d4a843}
.log-msg.error{color:#e07060}
.log-msg.info{color:var(--silver-lt)}
.log-empty{color:var(--silver);font-style:italic;font-family:var(--serif);font-size:13px;margin:auto;text-align:center;padding:40px 0}

/* token badge */
.token-badge{background:rgba(201,168,76,.06);border:1px solid var(--border);border-radius:2px;padding:8px 12px;display:flex;align-items:center;gap:8px;font-size:10px}
.token-dot{width:6px;height:6px;border-radius:50%;background:var(--silver);flex-shrink:0}
.token-dot.ready{background:var(--gold);box-shadow:0 0 6px rgba(201,168,76,.5)}
.token-label{color:var(--silver);letter-spacing:.06em}
.token-val{color:var(--gold-pale);margin-left:auto;font-size:9px;letter-spacing:.06em}

::-webkit-scrollbar{width:3px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(201,168,76,.18);border-radius:2px}
</style>
</head>
<body>

<header>
  <div class="wm">
    <div class="wm-t">CryptoAPI</div>
    <div class="wm-s">System Launcher</div>
  </div>
  <div class="hdr-sep"></div>
  <div class="token-badge">
    <div class="token-dot" id="tok-dot"></div>
    <span class="token-label">SARA Token</span>
    <span class="token-val" id="tok-val">Not captured</span>
  </div>
  <div class="hdr-r">
    <span style="font-size:10px;color:var(--silver)" id="clock">—</span>
  </div>
</header>

<div class="content">

  <!-- Sidebar -->
  <aside class="sidebar">

    <div>
      <div class="section-label">Services</div>

      <div class="svc-card" style="margin-bottom:12px">
        <div class="svc-header">
          <span class="svc-name">SARA</span>
          <span class="svc-port">:5000</span>
        </div>
        <div class="status-row">
          <div class="beacon" id="sara-beacon"></div>
          <span class="status-text" id="sara-status-text">Stopped</span>
        </div>
        <div class="svc-detail">pendulum_key_gen.py<br>
          Entropy collector · Key derivation<br>
          AES-256-GCM encryption core
        </div>
      </div>

      <div class="svc-card">
        <div class="svc-header">
          <span class="svc-name">API Provider</span>
          <span class="svc-port">:8000</span>
        </div>
        <div class="status-row">
          <div class="beacon" id="app-beacon"></div>
          <span class="status-text" id="app-status-text">Stopped</span>
        </div>
        <div class="svc-detail">app.py<br>
          Customer keys · Usage tracking<br>
          REST API · Customer portal
        </div>
      </div>
    </div>

    <div>
      <div class="section-label">Startup sequence</div>
      <div class="steps">
        <div class="step" id="step-sara"><span class="step-icon">①</span> Start SARA server</div>
        <div class="step" id="step-entropy"><span class="step-icon">②</span> Collect entropy</div>
        <div class="step" id="step-key"><span class="step-icon">③</span> Derive key</div>
        <div class="step" id="step-token"><span class="step-icon">④</span> Capture API token</div>
        <div class="step" id="step-app"><span class="step-icon">⑤</span> Start API provider</div>
      </div>
    </div>

    <div style="display:flex;flex-direction:column;gap:8px;margin-top:auto">
      <button class="launch-btn" id="btn-start" onclick="startAll()">
        <span id="btn-icon">▶</span>
        <span id="btn-label">Launch Everything</span>
      </button>
      <button class="launch-btn danger" id="btn-stop" onclick="stopAll()" disabled>
        ■ &nbsp;Stop All
      </button>
      <button class="portal-btn" id="btn-portal" onclick="openPortal()" disabled>
        ↗ &nbsp;Open Customer Portal
      </button>
    </div>

  </aside>

  <!-- Log area -->
  <div class="log-area">
    <div class="log-tabs">
      <div class="log-tab active" onclick="switchTab('launcher')">Launcher</div>
      <div class="log-tab" onclick="switchTab('sara')">SARA</div>
      <div class="log-tab" onclick="switchTab('app')">API App</div>
    </div>
    <div class="log-pane active" id="pane-launcher">
      <div class="log-empty">Waiting to launch…</div>
    </div>
    <div class="log-pane" id="pane-sara">
      <div class="log-empty">SARA not started yet</div>
    </div>
    <div class="log-pane" id="pane-app">
      <div class="log-empty">API app not started yet</div>
    </div>
  </div>

</div>

<script>
let currentTab = 'launcher';
let logSince   = 0;
let running    = false;
let pollTimer  = null;

function switchTab(name) {
  currentTab = name;
  document.querySelectorAll('.log-tab').forEach((t,i) => {
    t.classList.toggle('active', ['launcher','sara','app'][i] === name);
  });
  document.querySelectorAll('.log-pane').forEach(p => p.classList.remove('active'));
  document.getElementById('pane-'+name).classList.add('active');
}

function tickClock() {
  document.getElementById('clock').textContent = new Date().toISOString().slice(11,19)+' UTC';
}
tickClock(); setInterval(tickClock, 1000);

function appendLogs(paneId, lines) {
  const pane = document.getElementById(paneId);
  const empty = pane.querySelector('.log-empty');
  if (empty && lines.length) empty.remove();
  const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 40;
  lines.forEach(l => {
    const div = document.createElement('div');
    div.className = 'log-line';
    const level = l.level === 'ok' ? 'ok' : l.level === 'error' ? 'error' : l.level === 'warn' ? 'warn' : 'info';
    div.innerHTML = `<span class="log-ts">${l.ts}</span><span class="log-msg ${level}">${escHtml(l.msg)}</span>`;
    pane.appendChild(div);
  });
  if (atBottom) pane.scrollTop = pane.scrollHeight;
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function setBeacon(id, statusClass) {
  const el = document.getElementById(id);
  el.className = 'beacon ' + (statusClass || '');
}

function setStatusText(id, text, cls) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'status-text ' + (cls || '');
}

function setStep(id, state) {
  const el = document.getElementById(id);
  el.className = 'step ' + (state || '');
}

function updateUI(status) {
  const ss = status.sara_status;
  const as = status.app_status;

  // Beacons
  setBeacon('sara-beacon', ss === 'running' ? 'running' : ss === 'starting' || ss === 'waiting_token' ? 'starting' : ss === 'error' ? 'error' : '');
  setBeacon('app-beacon',  as === 'running' ? 'running' : as === 'starting' ? 'starting' : as === 'error' ? 'error' : '');

  // Status text
  const saraLabel = {stopped:'Stopped', starting:'Starting…', waiting_token:'Waiting for key…', running:'Running', error:'Error'}[ss] || ss;
  const appLabel  = {stopped:'Stopped', starting:'Starting…', running:'Running', error:'Error'}[as] || as;
  setStatusText('sara-status-text', saraLabel, ss === 'running' ? 'running' : ss === 'error' ? 'error' : '');
  setStatusText('app-status-text',  appLabel,  as === 'running' ? 'running' : as === 'error' ? 'error' : '');

  // Token badge
  if (status.token_ready) {
    document.getElementById('tok-dot').className  = 'token-dot ready';
    document.getElementById('tok-val').textContent = 'Captured ✓';
  }

  // Steps
  const done='done', active='active', err='error';
  if (ss === 'stopped' && as === 'stopped') {
    ['step-sara','step-entropy','step-key','step-token','step-app'].forEach(id => setStep(id,''));
  } else {
    setStep('step-sara',    ss !== 'stopped' ? done : '');
    setStep('step-entropy', ss === 'waiting_token' || ss === 'running' ? done : ss === 'starting' ? active : '');
    setStep('step-key',     ss === 'waiting_token' || ss === 'running' ? done : '');
    setStep('step-token',   status.token_ready ? done : ss === 'waiting_token' ? active : '');
    setStep('step-app',     as === 'running' ? done : as === 'starting' ? active : as === 'error' ? err : '');
  }

  // Buttons
  const allRunning = ss === 'running' && as === 'running';
  const anyStopped = ss === 'stopped' && as === 'stopped';
  document.getElementById('btn-start').disabled  = !anyStopped;
  document.getElementById('btn-stop').disabled   = anyStopped;
  document.getElementById('btn-portal').disabled = as !== 'running';

  // Launch button label
  if (ss === 'starting' || ss === 'waiting_token' || as === 'starting') {
    document.getElementById('btn-icon').textContent  = '◉';
    document.getElementById('btn-label').textContent = 'Starting…';
  } else if (allRunning) {
    document.getElementById('btn-icon').textContent  = '✓';
    document.getElementById('btn-label').textContent = 'All Systems Running';
  } else {
    document.getElementById('btn-icon').textContent  = '▶';
    document.getElementById('btn-label').textContent = 'Launch Everything';
  }
}

async function poll() {
  try {
    const [status, logs] = await Promise.all([
      fetch('/launcher/status').then(r=>r.json()),
      fetch('/launcher/logs').then(r=>r.json()),
    ]);
    updateUI(status);
    appendLogs('pane-launcher', logs.launcher);
    appendLogs('pane-sara',     logs.sara);
    appendLogs('pane-app',      logs.app);
  } catch(e) {}
}

async function startAll() {
  document.getElementById('btn-start').disabled = true;
  document.getElementById('btn-icon').textContent  = '◉';
  document.getElementById('btn-label').textContent = 'Starting…';
  switchTab('launcher');
  await fetch('/launcher/start', {method:'POST'});
  if (!pollTimer) pollTimer = setInterval(poll, 1200);
}

async function stopAll() {
  await fetch('/launcher/stop', {method:'POST'});
  setTimeout(poll, 800);
}

function openPortal() {
  window.open('http://localhost:8000/portal', '_blank');
}

// Start polling immediately
poll();
pollTimer = setInterval(poll, 1200);
</script>
</body>
</html>
"""

# ── Run ───────────────────────────────────────────────────────────────────────

def open_browser():
    time.sleep(1.2)
    webbrowser.open(f"http://localhost:{LAUNCHER_PORT}")

if __name__ == "__main__":
    print("=" * 55)
    print("  CryptoAPI Launcher")
    print("=" * 55)
    print(f"  Opening control panel at http://localhost:{LAUNCHER_PORT}")
    print("  Press Ctrl+C to shut everything down.\n")

    threading.Thread(target=open_browser, daemon=True).start()

    def on_exit(sig, frame):
        print("\nShutting down…")
        stop_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, on_exit)
    signal.signal(signal.SIGTERM, on_exit)

    launcher_app.run(host="127.0.0.1", port=LAUNCHER_PORT, debug=False, use_reloader=False)