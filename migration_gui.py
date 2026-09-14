#!/usr/bin/env python3
"""
migration_gui.py
-----------------
A local, browser-based GUI wrapper around new_central_migrate.py.

This does NOT reimplement the migration logic -- it imports
new_central_migrate.py as a module and drives its real main() function
exactly the way the command line does, just via a web form instead of
environment variables and flags. This is deliberate: the migration logic
has been battle-tested against a real tenant pair through many rounds of
real bugs and fixes, and duplicating it here would risk drifting out of
sync with that hard-won behavior. This file is only a thin shell: a local
HTTP server, an HTML form, and a way to stream main()'s own print() output
back to your browser in real time.

USAGE
=====
    python3 migration_gui.py

Then open the printed URL (http://127.0.0.1:8765 by default) in your
browser. Both new_central_migrate.py and migration_gui.py must be in the
same folder.

SECURITY NOTES
==============
- This server only listens on 127.0.0.1 (localhost) -- nothing on your
  network can reach it, only your own browser on the same machine.
- Tokens you type into the form are held in memory only for the duration
  of one run and are never written to disk by this GUI, never logged, and
  are cleared from the process environment as soon as that run finishes.
  (The underlying script's own export files, of course, still get written
  to disk exactly as they do from the command line -- that part is
  unchanged and is not a secret, it's your tenant's config data.)
- Only one migration run is allowed at a time (a lock enforces this),
  because the underlying script is driven via process environment
  variables, which are shared, global state -- letting two runs overlap
  would let their credentials cross-contaminate each other.
- This is meant for one person, running it on their own machine, against
  tenants they already have valid credentials for. It has no login screen
  and no authentication of its own -- don't expose port 8765 beyond
  localhost (e.g. don't port-forward it, don't run it on a shared server).
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import queue
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import new_central_migrate as migrate

HOST = "127.0.0.1"
PORT = 8765

RUN_LOCK = threading.Lock()


class LineQueueWriter(io.TextIOBase):
    """A file-like object that main()'s print() calls write into. Buffers
    partial lines and pushes each completed line onto a queue so the HTTP
    handler can stream it out to the browser as soon as it's produced."""

    def __init__(self, q):
        self.q = q
        self.buf = ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.q.put(line + "\n")
        return len(s)

    def flush(self):
        pass


def run_migration(params, q):
    """Runs new_central_migrate.main() with the given form params, exactly
    the way the CLI would with equivalent env vars and flags. Everything
    printed by main() (including its own error handling) is captured line
    by line onto q. Puts None onto q when done, as an end-of-stream marker."""
    old_argv = sys.argv
    old_environ = {k: os.environ.get(k) for k in
                   ("SRC_BASE_URL", "SRC_TOKEN", "DST_BASE_URL", "DST_TOKEN")}
    writer = LineQueueWriter(q)
    secrets_tmp_path = None
    try:
        os.environ["SRC_BASE_URL"] = params.get("src_base") or ""
        os.environ["SRC_TOKEN"] = params.get("src_token") or ""
        os.environ["DST_BASE_URL"] = params.get("dst_base") or ""
        os.environ["DST_TOKEN"] = params.get("dst_token") or ""

        argv = ["new_central_migrate.py", "--export-dir", params.get("export_dir") or "./export"]
        if params.get("from_export"):
            argv.append("--from-export")
        if params.get("apply"):
            argv.append("--apply")
        if params.get("skip_role_repatch"):
            argv.append("--skip-role-repatch")
        only = (params.get("only") or "").strip()
        if only:
            argv += ["--only", only]

        # Secret values (e.g. the real RADIUS shared secret for an Alias
        # like NAM_RADIUS_KEY) typed into the "Secret values" box: this
        # never gets written into the export folder or anywhere permanent.
        # It's parsed here, dropped into a private temp file that only
        # new_central_migrate.py's --secrets-file reads for the length of
        # this one run, then deleted in the `finally` below regardless of
        # how the run ends.
        secrets_json = (params.get("secrets_json") or "").strip()
        if secrets_json:
            try:
                parsed = json.loads(secrets_json)
                if not isinstance(parsed, dict):
                    raise ValueError("must be a JSON object, e.g. "
                                      '{"alias:NAME": "real value"}')
            except (json.JSONDecodeError, ValueError) as e:
                with contextlib.redirect_stdout(writer):
                    print(f"[FAILED] \"Secret values\" field is not valid JSON: {e}")
                return
            fd, secrets_tmp_path = tempfile.mkstemp(prefix="ncmig_secrets_", suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(parsed, f)
            argv += ["--secrets-file", secrets_tmp_path]

        sys.argv = argv

        with contextlib.redirect_stdout(writer):
            try:
                migrate.main()
            except SystemExit as e:
                print(f"[FAILED] {e}")
            except Exception as e:
                print(f"[FATAL] {type(e).__name__}: {e}")
    finally:
        sys.argv = old_argv
        for k, v in old_environ.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if secrets_tmp_path:
            try:
                os.remove(secrets_tmp_path)
            except OSError:
                pass
        # best-effort: drop our local references to the token/secret strings
        params["src_token"] = None
        params["dst_token"] = None
        params["secrets_json"] = None
        q.put(None)


def test_connection(base_url, token):
    if not base_url or not token:
        return {"ok": False, "message": "Base URL and token are both required."}
    try:
        # Aliases is one of the smallest/cheapest object types to fetch,
        # used here purely as a lightweight "can we authenticate" probe.
        status, body = migrate.api_call(base_url, token, "GET", "/network-config/v1alpha1/aliases")
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}
    if status == 200:
        return {"ok": True, "message": f"Connected (HTTP {status})."}
    return {"ok": False, "message": f"HTTP {status}: {body}"}


def list_object_types():
    """The full catalog of object types this migration covers, straight
    from new_central_migrate.OBJECT_TYPES -- the GUI's "what's being
    migrated" list is generated from this, not hand-maintained separately,
    so it can never drift out of sync with what the script actually does."""
    return [
        {
            "phase": t["phase"],
            "label": t["label"],
            "slug": migrate.export_slug(t),
        }
        for t in migrate.OBJECT_TYPES
    ]


def export_summary(export_dir):
    """For each known object type, how many items are sitting in that
    type's saved export file right now (or None if it hasn't been
    exported yet) -- lets the GUI show real counts after Step 1 runs."""
    summary = {}
    for t in migrate.OBJECT_TYPES:
        slug = migrate.export_slug(t)
        try:
            items, _ = migrate.load_export_file(export_dir, slug)
            summary[slug] = len(items)
        except (FileNotFoundError, json.JSONDecodeError):
            summary[slug] = None
    return summary


INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>New Central Migration Tool</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f5f6f8; --card: #ffffff; --border: #d7dbe0; --text: #1c2126;
    --muted: #5b6570; --accent: #2f6fed; --accent-dark: #1d4fbb;
    --danger: #c0392b; --danger-dark: #962d22; --ok: #1c8a4b; --warn: #b7791f;
    --log-bg: #10141a; --log-text: #d7e0ea;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14171c; --card: #1b1f26; --border: #333a44; --text: #e7ebef;
      --muted: #97a1ab; --log-bg: #0b0d10; --log-text: #cfd8e2;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding-block: 24px; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  .wrap { max-width: 880px; margin: 0 auto; padding-inline: 16px; }
  h1 { font-size: 1.4rem; margin: 0 0 4px; }
  .subtitle { color: var(--muted); margin: 0 0 20px; font-size: 0.92rem; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 18px 20px; margin-bottom: 16px;
  }
  .card h2 { font-size: 1.05rem; margin: 0 0 12px; }
  .field { margin-bottom: 12px; }
  .field label { display: block; font-size: 0.85rem; color: var(--muted); margin-bottom: 4px; }
  .field input[type=text], .field input[type=password], .field textarea {
    width: 100%; padding: 8px 10px; border-radius: 6px; border: 1px solid var(--border);
    background: transparent; color: var(--text); font-size: 0.92rem;
  }
  .field textarea {
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 0.82rem;
    min-height: 70px; resize: vertical;
  }
  .field .hint { font-size: 0.78rem; color: var(--muted); margin-top: 4px; }
  .row { display: flex; gap: 12px; flex-wrap: wrap; }
  .row > .field { flex: 1 1 220px; }
  .checkline { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; font-size: 0.9rem; }
  .btns { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  button {
    border: none; border-radius: 6px; padding: 9px 16px; font-size: 0.9rem;
    cursor: pointer; font-weight: 600;
  }
  button.primary { background: var(--accent); color: #fff; }
  button.primary:hover { background: var(--accent-dark); }
  button.danger { background: var(--danger); color: #fff; }
  button.danger:hover { background: var(--danger-dark); }
  button.secondary { background: transparent; color: var(--text); border: 1px solid var(--border); }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .status { font-size: 0.85rem; margin-left: 4px; }
  .status.ok { color: var(--ok); }
  .status.err { color: var(--danger); }
  .note {
    font-size: 0.82rem; color: var(--muted); background: rgba(128,128,128,0.08);
    border-radius: 6px; padding: 10px 12px; margin-bottom: 16px;
  }
  #log {
    background: var(--log-bg); color: var(--log-text); border-radius: 8px;
    padding: 12px 14px; height: 360px; overflow-y: auto; font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 0.8rem; white-space: pre-wrap; word-break: break-word;
  }
  .line-ok { color: #7ee08a; }
  .line-skip { color: #e0c67e; }
  .line-err { color: #ff8a80; }
  .logbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; gap: 10px; flex-wrap: wrap; }
  .logbar-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  #log_search {
    padding: 6px 10px; border-radius: 6px; border: 1px solid var(--border);
    background: transparent; color: var(--text); font-size: 0.82rem; min-width: 180px;
  }
  #log_search_count { font-size: 0.78rem; color: var(--muted); white-space: nowrap; }
  .line-hidden { display: none; }
  #copy_log_btn.copied { background: var(--ok); color: #06280f; border-color: var(--ok); }
  #type_table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
  #type_table th, #type_table td {
    text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border);
  }
  #type_table th { color: var(--muted); font-weight: 600; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.03em; }
  #type_table td.count-found { color: var(--ok); font-weight: 600; }
  #type_table td.count-zero { color: var(--muted); }
  #type_table td.count-none { color: var(--muted); font-style: italic; }
  .muted-row { color: var(--muted); font-style: italic; padding: 10px !important; }
  #type_table_wrap { max-height: 340px; overflow-y: auto; }
</style>
</head>
<body>
<div class="wrap">
  <h1>New Central Migration Tool</h1>
  <p class="subtitle">Copies Library Profiles from one New Central tenant to another. Runs entirely on this machine.</p>

  <div class="note">
    Tokens you enter below are used only in memory for one run and are never written to disk by this
    tool or sent anywhere except the tenant hosts you specify. This page only talks to a server on
    your own machine (127.0.0.1) &mdash; nothing leaves this computer except the calls to your two
    New Central tenants. Always review Step 1's dry-run output before running Step 2.
  </div>

  <div class="card">
    <h2>Export directory (shared by both steps)</h2>
    <div class="field">
      <label>Folder to save/read exported JSON files</label>
      <input type="text" id="export_dir" value="./export" onchange="refreshCounts()">
    </div>
  </div>

  <div class="card">
    <div class="logbar">
      <h2 style="margin:0;">Object types covered by this migration</h2>
      <button class="secondary" onclick="refreshCounts()">Refresh counts</button>
    </div>
    <p class="subtitle" style="margin:0 0 10px;">
      Every row below is a real object type this tool can migrate &mdash; roles, policies, WLANs,
      NTP, and the rest. "Items found" fills in once Step 1 has exported from a source tenant into
      the folder above, so you can confirm what's actually there before applying it.
    </p>
    <div id="type_table_wrap">
      <table id="type_table">
        <thead><tr><th>Phase</th><th>Object type</th><th>Items found</th></tr></thead>
        <tbody id="type_table_body">
          <tr><td colspan="3" class="muted-row">Loading&hellip;</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <h2>Step 1 &mdash; Export from source tenant (read-only, no destination needed)</h2>
    <div class="row">
      <div class="field">
        <label>Source base URL</label>
        <input type="text" id="src_base" placeholder="https://us2.api.central.arubanetworks.com">
      </div>
      <div class="field">
        <label>Source token</label>
        <input type="password" id="src_token" placeholder="Bearer token">
      </div>
    </div>
    <div class="btns">
      <button class="secondary" onclick="testConn('source')">Test connection</button>
      <button class="primary" onclick="runExport()">Export &amp; dry run</button>
      <span id="status_source" class="status"></span>
    </div>
  </div>

  <div class="card">
    <h2>Step 2 &mdash; Apply to destination tenant (writes!)</h2>
    <div class="row">
      <div class="field">
        <label>Destination base URL</label>
        <input type="text" id="dst_base" placeholder="https://us2.api.central.arubanetworks.com">
      </div>
      <div class="field">
        <label>Destination token</label>
        <input type="password" id="dst_token" placeholder="Bearer token">
      </div>
    </div>
    <div class="row">
      <div class="field">
        <label>Only these object types (optional, comma-separated list_keys, e.g. role,policy)</label>
        <input type="text" id="only" placeholder="leave blank to migrate everything">
      </div>
    </div>
    <div class="checkline">
      <input type="checkbox" id="skip_role_repatch">
      <label for="skip_role_repatch">Skip phase F (role &rarr; policy reattachment)</label>
    </div>
    <div class="field">
      <label>Secret values (optional, JSON) &mdash; automates secrets that can't travel from source</label>
      <textarea id="secrets_json" placeholder='{"alias:NAM_RADIUS_KEY": "the real shared secret", "auth-server:US_RADIUS_CHI_VIP1": "the real shared secret"}'></textarea>
      <div class="hint">
        Some values (auth server shared secrets, WPA passphrases, secret-holding Aliases like a RADIUS key)
        are encrypted with the SOURCE tenant's own key and can't be decrypted in the destination &mdash;
        normally you'd retype these by hand in the destination UI. Paste the real values here instead
        (keyed <code>"&lt;list_key&gt;:&lt;name&gt;"</code> &mdash; the dry-run output and reference-check
        report show you the exact names) and this tool will write them in directly via the API. Only held
        in memory here and in a private temp file for the length of one run, then deleted immediately after
        &mdash; never saved to the export folder or anywhere permanent. Leave blank to skip and retype
        those fields by hand as before.
      </div>
    </div>
    <div class="btns">
      <button class="secondary" onclick="testConn('destination')">Test connection</button>
      <button class="danger" onclick="runApply()">Apply to destination</button>
      <span id="status_destination" class="status"></span>
    </div>
  </div>

  <div class="card">
    <div class="logbar">
      <h2 style="margin:0;">Output</h2>
      <div class="logbar-actions">
        <input type="text" id="log_search" placeholder="Search output..." oninput="filterLog()">
        <span id="log_search_count"></span>
        <button class="secondary" id="copy_log_btn" onclick="copyLog()">Copy output</button>
        <button class="secondary" onclick="clearLog()">Clear</button>
      </div>
    </div>
    <div id="log"></div>
  </div>
</div>

<script>
let running = false;

function appendLine(line) {
  const log = document.getElementById('log');
  const span = document.createElement('span');
  if (/\\bOK\\b/.test(line)) span.className = 'line-ok';
  else if (/\\bSKIP\\b/.test(line)) span.className = 'line-skip';
  else if (/\\bERR\\b|\\[FAILED\\]|\\[FATAL\\]/.test(line)) span.className = 'line-err';
  span.textContent = line;
  const term = document.getElementById('log_search').value.trim().toLowerCase();
  if (term && !line.toLowerCase().includes(term)) span.classList.add('line-hidden');
  log.appendChild(span);
  if (!span.classList.contains('line-hidden')) log.scrollTop = log.scrollHeight;
  updateSearchCount();
}

function clearLog() {
  document.getElementById('log').textContent = '';
  document.getElementById('log_search').value = '';
  updateSearchCount();
}

function filterLog() {
  const term = document.getElementById('log_search').value.trim().toLowerCase();
  const log = document.getElementById('log');
  for (const span of log.children) {
    const matches = !term || span.textContent.toLowerCase().includes(term);
    span.classList.toggle('line-hidden', !matches);
  }
  updateSearchCount();
}

function updateSearchCount() {
  const term = document.getElementById('log_search').value.trim();
  const countEl = document.getElementById('log_search_count');
  if (!term) { countEl.textContent = ''; return; }
  const log = document.getElementById('log');
  const total = log.children.length;
  const shown = total - log.querySelectorAll('.line-hidden').length;
  countEl.textContent = shown + ' / ' + total + ' lines';
}

async function copyLog() {
  const log = document.getElementById('log');
  const btn = document.getElementById('copy_log_btn');
  const text = log.innerText;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    const original = btn.textContent;
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = original; btn.classList.remove('copied'); }, 1500);
  } catch (e) {
    alert('Could not copy automatically -- select the output text and copy it manually. (' + e + ')');
  }
}

async function testConn(role) {
  const base = document.getElementById(role === 'source' ? 'src_base' : 'dst_base').value.trim();
  const token = document.getElementById(role === 'source' ? 'src_token' : 'dst_token').value;
  const statusEl = document.getElementById('status_' + role);
  statusEl.textContent = 'Testing...';
  statusEl.className = 'status';
  const resp = await fetch('/api/test', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({base_url: base, token: token})
  });
  const data = await resp.json();
  statusEl.textContent = data.message;
  statusEl.className = 'status ' + (data.ok ? 'ok' : 'err');
}

async function runMigration(params) {
  if (running) { alert('A run is already in progress -- wait for it to finish.'); return; }
  running = true;
  appendLine('\\n=== starting ' + (params.apply ? 'APPLY' : 'EXPORT / DRY RUN') + ' ===\\n');
  try {
    const resp = await fetch('/api/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(params)
    });
    if (!resp.ok) {
      const text = await resp.text();
      appendLine('[ERROR] ' + resp.status + ' ' + text);
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {stream: true});
      let idx;
      while ((idx = buf.indexOf('\\n')) >= 0) {
        appendLine(buf.slice(0, idx + 1));
        buf = buf.slice(idx + 1);
      }
    }
    if (buf) appendLine(buf);
  } catch (e) {
    appendLine('[ERROR] ' + e);
  } finally {
    running = false;
    appendLine('\\n=== finished ===\\n');
    refreshCounts();
  }
}

let objectTypes = [];

async function loadObjectTypes() {
  const resp = await fetch('/api/object-types');
  const data = await resp.json();
  objectTypes = data.types;
  renderTypeTable({});
  refreshCounts();
}

function renderTypeTable(counts) {
  const body = document.getElementById('type_table_body');
  body.innerHTML = '';
  for (const t of objectTypes) {
    const tr = document.createElement('tr');
    const count = counts[t.slug];
    let countHtml, countClass;
    if (count === undefined) { countHtml = 'not exported yet'; countClass = 'count-none'; }
    else if (count === null) { countHtml = 'not exported yet'; countClass = 'count-none'; }
    else if (count === 0) { countHtml = '0 (none in source)'; countClass = 'count-zero'; }
    else { countHtml = count + ' item' + (count === 1 ? '' : 's'); countClass = 'count-found'; }
    tr.innerHTML = '<td>' + t.phase + '</td><td>' + t.label + '</td><td class="' + countClass + '">' + countHtml + '</td>';
    body.appendChild(tr);
  }
}

async function refreshCounts() {
  if (!objectTypes.length) return;
  const exportDir = document.getElementById('export_dir').value.trim() || './export';
  try {
    const resp = await fetch('/api/export-summary', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({export_dir: exportDir})
    });
    const data = await resp.json();
    renderTypeTable(data.counts || {});
  } catch (e) {
    // export dir probably doesn't exist yet -- leave table as "not exported yet"
  }
}

loadObjectTypes();

function runExport() {
  runMigration({
    mode: 'export',
    export_dir: document.getElementById('export_dir').value.trim() || './export',
    src_base: document.getElementById('src_base').value.trim(),
    src_token: document.getElementById('src_token').value,
    from_export: false,
    apply: false
  });
}

function runApply() {
  if (!confirm('This will write to the DESTINATION tenant. Have you reviewed Step 1\\'s output? Continue?')) return;
  runMigration({
    mode: 'apply',
    export_dir: document.getElementById('export_dir').value.trim() || './export',
    dst_base: document.getElementById('dst_base').value.trim(),
    dst_token: document.getElementById('dst_token').value,
    only: document.getElementById('only').value.trim(),
    skip_role_repatch: document.getElementById('skip_role_repatch').checked,
    secrets_json: document.getElementById('secrets_json').value.trim(),
    from_export: true,
    apply: true
  });
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    # BaseHTTPRequestHandler defaults to protocol_version "HTTP/1.0", under
    # which "Transfer-Encoding: chunked" (used by /api/run below to stream
    # output live) is not a defined mechanism -- a browser talking HTTP/1.0
    # to this server does NOT decode the chunk framing, it just hands the
    # raw bytes (the hex chunk-size lines and \r\n's included) straight to
    # the page's fetch() reader. That's exactly the garbled output with hex
    # fragments like "4f", "31", "97" interspersed between real log lines
    # that showed up when output was copied out of the browser -- it was
    # never a copy/paste artifact, it was this server speaking a protocol
    # version that doesn't support the transfer encoding it was declaring.
    # Confirmed live 2026-09-13 by reproducing it locally end-to-end (a
    # real browser against a real instance of this server) and fixing it
    # by switching to HTTP/1.1, which defines chunked encoding and which
    # every browser this GUI targets decodes transparently.
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet; the browser is the UI

    def _send_json(self, status, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else {}

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/object-types":
            self._send_json(200, {"types": list_object_types()})
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/test":
            params = self._read_json()
            result = test_connection(params.get("base_url"), params.get("token"))
            self._send_json(200, result)
            return

        if self.path == "/api/export-summary":
            params = self._read_json()
            export_dir = params.get("export_dir") or "./export"
            self._send_json(200, {"counts": export_summary(export_dir)})
            return

        if self.path == "/api/run":
            params = self._read_json()
            if not RUN_LOCK.acquire(blocking=False):
                self._send_json(409, {"message": "A migration run is already in progress."})
                return

            q = queue.Queue()
            t = threading.Thread(target=run_migration, args=(params, q), daemon=True)
            t.start()

            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    line = q.get()
                    if line is None:
                        break
                    data = line.encode("utf-8")
                    self.wfile.write(f"{len(data):x}\r\n".encode())
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass  # browser tab closed mid-run; the worker thread still finishes on its own
            finally:
                RUN_LOCK.release()
            return

        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"New Central Migration GUI running at {url}")
    print("Press Ctrl+C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")


if __name__ == "__main__":
    main()
