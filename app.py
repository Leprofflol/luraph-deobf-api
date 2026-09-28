"""
Luraph v15 deobfuscator as an HTTP API (FastAPI; deploys as a Vercel Python
Function, runs anywhere with `uvicorn app:app`).

    POST /api/deobfuscate   protected script in -> deobfuscated Luau out
    POST /api/detect        is this a Luraph v15 script? (cheap, no execution)
    GET  /api/health        runtime check (Luau binaries, limits)
    GET  /                  tiny web page with an upload form + usage

Every request runs `deobf/deob.py` in its own subprocess (the deobfuscator
keeps global state and is not safe to call twice in one process), inside a
private temp folder, with a wall-clock deadline that stays under the
platform's function timeout.
"""
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

ROOT = os.path.dirname(os.path.abspath(__file__))
DEOBF_DIR = os.path.join(ROOT, "deobf")
DEOB_PY = os.path.join(DEOBF_DIR, "deob.py")
SRC_BIN = os.path.join(DEOBF_DIR, "bin")
TOOLS = ("luau", "luau-ast")

# ---- limits (override with environment variables) -------------------------
# Vercel: request bodies are capped at 4.5 MB and (with Fluid compute) a
# function may run 300 s on Hobby / up to 800 s on Pro (vercel.json maxDuration).
MAX_INPUT_BYTES = int(os.environ.get("DEOBF_MAX_INPUT", 4_000_000))
TOTAL_TIMEOUT = int(os.environ.get("DEOBF_TOTAL_TIMEOUT", 280))      # whole request, seconds
DEFAULT_RUN_TIMEOUT = int(os.environ.get("DEOBF_RUN_TIMEOUT", 90))   # deob.py --timeout (per harness run)
DEFAULT_BUDGET = int(os.environ.get("DEOBF_BUDGET", 30))             # deob.py --budget
LOG_TAIL = 6000                                                      # bytes of stderr returned

EXIT_UNRECOGNIZED = 3

app = FastAPI(
    title="Luraph v15 Deobfuscator API",
    version="1.0.0",
    description="Dynamic deobfuscator for Roblox Luau scripts protected with Luraph v15: "
                "the script runs in a real Luau VM against a fake Roblox environment and its "
                "VM bytecode is lifted back to readable Luau.",
)


# ---------------------------------------------------------------------------
# Luau binaries: deobf/bin/luau + luau-ast. On a read-only deployment they may
# have lost their executable bit; copy them to a writable place and chmod +x.

_bin_lock = threading.Lock()
_bin_state = {"dir": None, "error": None}


def _executable(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)


def prepare_bin():
    """Folder with runnable luau/luau-ast (None if missing), cached."""
    with _bin_lock:
        if _bin_state["dir"] or _bin_state["error"]:
            return _bin_state["dir"]
        missing = [t for t in TOOLS if not os.path.isfile(os.path.join(SRC_BIN, t))]
        if missing:
            _bin_state["error"] = "missing binaries in deobf/bin: %s (build them with deobf/build_luau.py)" \
                                  % ", ".join(missing)
            return None
        if all(_executable(os.path.join(SRC_BIN, t)) for t in TOOLS):
            _bin_state["dir"] = SRC_BIN
            return SRC_BIN
        dest = os.path.join(tempfile.gettempdir(), "deobf-bin")
        os.makedirs(dest, exist_ok=True)
        for t in TOOLS:
            src, dst = os.path.join(SRC_BIN, t), os.path.join(dest, t)
            if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
                shutil.copyfile(src, dst)
            os.chmod(dst, os.stat(dst).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _bin_state["dir"] = dest
        return dest


def child_env():
    env = dict(os.environ)
    env["DEOBF_BIN"] = prepare_bin() or SRC_BIN
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def luau_version():
    """Build description from deobf/bin/VERSION.txt, and whether luau actually runs."""
    d = prepare_bin()
    if not d:
        return None
    desc = "luau"
    try:
        with open(os.path.join(SRC_BIN, "VERSION.txt"), encoding="utf-8") as f:
            desc = f.read().strip() or desc
    except OSError:
        pass
    ok = False
    try:
        fd, path = tempfile.mkstemp(suffix=".luau")
        with os.fdopen(fd, "w") as f:
            f.write("print(_VERSION)\n")
        try:
            r = subprocess.run([os.path.join(d, "luau"), path], capture_output=True, timeout=10)
            ok = r.returncode == 0 and b"Luau" in r.stdout
        finally:
            os.remove(path)
    except Exception:  # noqa: BLE001
        pass
    return desc + (" [runs]" if ok else " [DOES NOT RUN]")


# ---------------------------------------------------------------------------
# running deob.py

def run_deob(argv, timeout, cwd):
    """Run deob.py (its own process group, killed as a whole on timeout).
    -> (returncode or None when timed out, stderr text)."""
    cmd = [sys.executable or "python3", DEOB_PY] + argv
    kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE, "cwd": cwd, "env": child_env()}
    if os.name != "nt":
        kw["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kw)
    try:
        _, err = proc.communicate(timeout=timeout)
        return proc.returncode, err.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except OSError:
            pass
        _, err = proc.communicate()
        return None, err.decode("utf-8", "replace")


def tail(text, n=LOG_TAIL):
    text = text.replace("\r\n", "\n")
    return text if len(text) <= n else "...\n" + text[-n:]


def as_bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def clamp_int(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


async def read_script(request: Request):
    """The script from a multipart upload (`file` or `script` field), a JSON
    body ({"script": ...} plus options) or a raw text body.
    -> (script bytes, filename, options dict from the body)."""
    ctype = (request.headers.get("content-type") or "").lower()
    opts = {}
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is not None and hasattr(upload, "read"):
            data = await upload.read()
            name = getattr(upload, "filename", None) or "script.lua"
        else:
            data = str(form.get("script") or "").encode("latin-1", "replace")
            name = "script.lua"
        for k, v in form.multi_items():
            if k not in ("file", "script"):
                opts.setdefault(k, []).append(str(v))
        return data, name, opts
    body = await request.body()
    if ctype.startswith("application/json"):
        try:
            j = json.loads(body.decode("utf-8"))
        except ValueError:
            return None, None, {"_error": "invalid JSON body"}
        if not isinstance(j, dict) or not isinstance(j.get("script"), str):
            return None, None, {"_error": 'JSON body must be {"script": "<protected source>", ...options}'}
        for k, v in j.items():
            if k != "script":
                opts[k] = v if isinstance(v, list) else [v]
        return j["script"].encode("latin-1", "replace"), str(j.get("filename") or "script.lua"), opts
    return body, "script.lua", opts


def option(opts, query, key, default=None):
    """Body options win over query parameters."""
    if key in opts and opts[key]:
        return opts[key][-1] if isinstance(opts[key], list) else opts[key]
    v = query.get(key)
    return default if v is None else v


def safe_name(name):
    name = os.path.basename(str(name or "script.lua")).replace("\x00", "")
    name = re.sub(r"[^\w.\-]+", "_", name)[:80] or "script.lua"
    if not re.search(r"\.(luau?|txt)$", name, re.I):
        name += ".lua"
    return name


def build_argv(inp, out, meta, opts, query):
    mode = str(option(opts, query, "mode", "devirt")).lower()
    argv = [inp, "-o", out, "--meta", meta,
            "--timeout", str(clamp_int(option(opts, query, "timeout"), 5, TOTAL_TIMEOUT, DEFAULT_RUN_TIMEOUT)),
            "--budget", str(clamp_int(option(opts, query, "budget"), 1, TOTAL_TIMEOUT, DEFAULT_BUDGET)),
            "--executor", re.sub(r"[^\w .\-]", "", str(option(opts, query, "executor", "Wave")))[:40] or "Wave"]
    if mode in ("trace", "no-devirt", "nodevirt"):
        argv.append("--no-devirt")
        mode = "trace"
    else:
        mode = "devirt"
    if as_bool(option(opts, query, "force", False)):
        argv += ["--obfuscator", "luraph_v15"]
    if as_bool(option(opts, query, "strings", False)):
        argv.append("--strings")
    if as_bool(option(opts, query, "no_fold", False)):
        argv.append("--no-fold")
    if as_bool(option(opts, query, "keep_preamble", False)):
        argv.append("--keep-preamble")
    it = option(opts, query, "input_text")
    if it is not None:
        argv += ["--input-text", str(it)]
    mr = option(opts, query, "max_runs")
    if mr is not None:
        argv += ["--max-runs", str(clamp_int(mr, 1, 12, 12))]
    dr = option(opts, query, "devirt_rounds")
    if dr is not None:
        argv += ["--devirt-rounds", str(clamp_int(dr, 1, 200, 200))]
    cfgs = opts.get("cfg") or query.getlist("cfg")
    for c in cfgs or []:
        c = str(c)
        if "@file:" in c:      # no reading of server files through --cfg
            continue
        argv += ["--cfg", c]
    return argv, mode


def error(status, message, **extra):
    return JSONResponse({"ok": False, "error": message, **extra}, status_code=status)


# ---------------------------------------------------------------------------
# routes

@app.get("/api/health")
def health():
    d = prepare_bin()
    return {
        "ok": d is not None,
        "luau": luau_version(),
        "bin_dir": d,
        "bin_error": _bin_state["error"],
        "python": sys.version.split()[0],
        "limits": {"max_input_bytes": MAX_INPUT_BYTES, "total_timeout_s": TOTAL_TIMEOUT,
                   "default_run_timeout_s": DEFAULT_RUN_TIMEOUT, "default_budget_s": DEFAULT_BUDGET},
    }


@app.post("/api/detect")
async def detect(request: Request):
    data, name, opts = await read_script(request)
    if data is None:
        return error(422, opts.get("_error", "no script"))
    if not data.strip():
        return error(422, "empty script")
    if len(data) > MAX_INPUT_BYTES:
        return error(413, "script too big (%d bytes, limit %d)" % (len(data), MAX_INPUT_BYTES))
    work = tempfile.mkdtemp(prefix="deobf_api_")
    try:
        inp = os.path.join(work, safe_name(name))
        meta = os.path.join(work, "meta.json")
        with open(inp, "wb") as f:
            f.write(data)
        code, err = run_deob([inp, "--detect", "--meta", meta], 60, work)
        if code != 0 or not os.path.exists(meta):
            return error(500, "detection failed", log=tail(err))
        with open(meta, encoding="utf-8") as f:
            m = json.load(f)
        return {"ok": True, "obfuscator": m.get("obfuscator"), "label": m.get("label"),
                "confidence": m.get("confidence"), "is_luraph_v15": m.get("obfuscator") == "luraph_v15"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.post("/api/deobfuscate")
async def deobfuscate(request: Request,
                      format: Optional[str] = Query(None, description="json (default) or text")):
    t0 = time.time()
    data, name, opts = await read_script(request)
    if data is None:
        return error(422, opts.get("_error", "no script"))
    if not data.strip():
        return error(422, "empty script: upload it as multipart field `file`, JSON {\"script\": ...} "
                          "or a raw text body")
    if len(data) > MAX_INPUT_BYTES:
        return error(413, "script too big (%d bytes, limit %d)" % (len(data), MAX_INPUT_BYTES))
    if not prepare_bin():
        return error(500, _bin_state["error"])
    fmt = str(option(opts, request.query_params, "format", format or "json")).lower()
    work = tempfile.mkdtemp(prefix="deobf_api_")
    try:
        inp = os.path.join(work, safe_name(name))
        out = os.path.join(work, "out", "result.lua")
        meta = os.path.join(work, "meta.json")
        with open(inp, "wb") as f:
            f.write(data)
        argv, mode = build_argv(inp, out, meta, opts, request.query_params)
        code, err = run_deob(argv, TOTAL_TIMEOUT, work)
        info = {}
        if os.path.exists(meta):
            with open(meta, encoding="utf-8") as f:
                info = json.load(f)
        elapsed = round(time.time() - t0, 2)
        if code is None:
            return error(504, "deobfuscation did not finish within %ds; try mode=trace, a smaller script, "
                              "or a longer maxDuration (Pro plan)" % TOTAL_TIMEOUT,
                         mode=mode, elapsed=elapsed, log=tail(err))
        if code == EXIT_UNRECOGNIZED:
            return error(400, "not recognized as a Luraph v15 script (confidence %.2f); pass force=true to "
                              "run the Luraph v15 pipeline anyway" % (info.get("confidence") or 0.0),
                         confidence=info.get("confidence"), elapsed=elapsed)
        if code != 0 or not os.path.exists(out):
            return error(500, "deobfuscation failed (exit %s)" % code, mode=mode, elapsed=elapsed,
                         log=tail(err))
        with open(out, encoding="utf-8", errors="replace") as f:
            text = f.read()
        result = {
            "ok": True,
            "obfuscator": {"name": info.get("obfuscator"), "label": info.get("label"),
                           "confidence": info.get("confidence"), "forced": info.get("forced")},
            "mode": mode,
            "result": info.get("result"),        # "devirtualized" (lifted bytecode) or "trace" (behaviour)
            "elapsed": elapsed,
            "output_bytes": len(text.encode("utf-8")),
            "output": text,
            "log": tail(err),
        }
        if fmt in ("text", "lua", "raw"):
            base = re.sub(r"\.(luau?|txt)$", "", safe_name(name), flags=re.I)
            headers = {"X-Deobf-Result": str(info.get("result")), "X-Deobf-Elapsed": str(elapsed),
                       "Content-Disposition": 'attachment; filename="%s-deobfuscated.lua"' % base}
            return PlainTextResponse(text, headers=headers, media_type="text/plain; charset=utf-8")
        return JSONResponse(result)
    finally:
        shutil.rmtree(work, ignore_errors=True)


INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Luraph v15 Deobfuscator API</title>
<style>
 body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:900px;margin:32px auto;padding:0 16px;
      background:#0f1115;color:#e6e6e6;line-height:1.45}
 h1{font-size:1.5rem;margin:0 0 4px} h2{font-size:1.1rem;margin:28px 0 8px;color:#9ecbff}
 .sub{color:#9aa0a6;margin-bottom:20px}
 code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.86rem}
 pre{background:#161a22;border:1px solid #262b36;border-radius:8px;padding:12px;overflow:auto;white-space:pre-wrap}
 form{background:#161a22;border:1px solid #262b36;border-radius:8px;padding:16px;display:grid;gap:10px}
 textarea{width:100%;min-height:160px;background:#0f1115;color:#e6e6e6;border:1px solid #333a48;border-radius:6px;padding:8px;box-sizing:border-box}
 input[type=file],select{color:#e6e6e6} label{color:#c9ced6;font-size:.92rem}
 .row{display:flex;gap:14px;flex-wrap:wrap;align-items:center}
 button{background:#2f6feb;color:#fff;border:0;border-radius:6px;padding:9px 16px;font-size:.95rem;cursor:pointer}
 button:disabled{opacity:.5;cursor:default}
 #status{color:#9aa0a6;font-size:.9rem} .err{color:#ff7b72}
 table{border-collapse:collapse;font-size:.9rem} td,th{border:1px solid #262b36;padding:5px 8px;text-align:left}
</style></head><body>
<h1>Luraph v15 Deobfuscator API</h1>
<div class="sub">Upload a script protected with <b>Luraph v15</b>; it runs in a sandboxed Luau VM and the VM
bytecode is lifted back to readable Luau. Nothing is stored.</div>

<form id="f">
  <div class="row"><label>File: <input type="file" name="file" accept=".lua,.luau,.txt"></label>
    <label>or paste below</label></div>
  <textarea name="script" placeholder="-- This file was protected using Luraph Obfuscator v15 ..."></textarea>
  <div class="row">
    <label>Mode <select name="mode"><option value="devirt">devirt (lift bytecode)</option>
      <option value="trace">trace (fast, behaviour only)</option></select></label>
    <label><input type="checkbox" name="force" value="true"> force (skip detection)</label>
    <button type="submit" id="go">Deobfuscate</button>
    <span id="status"></span>
  </div>
</form>
<pre id="out" hidden></pre>

<h2>Endpoints</h2>
<table>
<tr><th>Method</th><th>Path</th><th>What</th></tr>
<tr><td>POST</td><td><code>/api/deobfuscate</code></td><td>script in, deobfuscated Luau out (JSON, or <code>?format=text</code>)</td></tr>
<tr><td>POST</td><td><code>/api/detect</code></td><td>detection only: is it Luraph v15?</td></tr>
<tr><td>GET</td><td><code>/api/health</code></td><td>runtime check</td></tr>
<tr><td>GET</td><td><code>/docs</code></td><td>OpenAPI UI</td></tr>
</table>

<h2>Examples</h2>
<pre># multipart upload, JSON response
curl -F "file=@protected.lua" https://HOST/api/deobfuscate

# raw body, plain Lua back
curl --data-binary @protected.lua -H "Content-Type: text/plain" "https://HOST/api/deobfuscate?format=text" -o out.lua

# JSON body with options
curl -H "Content-Type: application/json" -d '{"script": "...", "mode": "trace", "force": true}' https://HOST/api/deobfuscate</pre>

<h2>Options</h2>
<table>
<tr><th>Name</th><th>Default</th><th>Meaning</th></tr>
<tr><td><code>mode</code></td><td>devirt</td><td><code>devirt</code>: lift the VM bytecode (falls back to the trace); <code>trace</code>: behaviour trace only (fast)</td></tr>
<tr><td><code>force</code></td><td>false</td><td>run the Luraph v15 pipeline even when the header/shape is not recognized</td></tr>
<tr><td><code>format</code></td><td>json</td><td><code>text</code> returns the Lua file itself</td></tr>
<tr><td><code>timeout</code>, <code>budget</code></td><td>90, 30</td><td>seconds: hard limit per harness run / soft budget for the traced script</td></tr>
<tr><td><code>executor</code></td><td>Wave</td><td>name <code>identifyexecutor()</code> returns inside the sandbox</td></tr>
<tr><td><code>input_text</code></td><td>-</td><td>text TextBoxes contain when event handlers are traced (key checks)</td></tr>
<tr><td><code>cfg</code></td><td>-</td><td>repeatable <code>KEY=VALUE</code> runtime options, e.g. <code>prelude=rawset(G,'webhook','x')</code></td></tr>
<tr><td><code>max_runs</code>, <code>devirt_rounds</code></td><td>12, 200</td><td>trap reruns / constant-request rounds</td></tr>
</table>
<p class="sub">Whole request limit: <span id="lim">-</span> s (Vercel function <code>maxDuration</code>). Big scripts that need
minutes will time out: use <code>mode=trace</code> or a Pro plan with a longer <code>maxDuration</code>.</p>

<script>
fetch('/api/health').then(r=>r.json()).then(h=>{document.getElementById('lim').textContent=h.limits.total_timeout_s}).catch(()=>{});
const f=document.getElementById('f'),out=document.getElementById('out'),st=document.getElementById('status'),go=document.getElementById('go');
f.addEventListener('submit',async e=>{
  e.preventDefault(); out.hidden=true; st.className=''; st.textContent='running... (seconds to minutes)'; go.disabled=true;
  const fd=new FormData(f); if(!fd.get('file')||!fd.get('file').size) fd.delete('file');
  const t=Date.now();
  try{
    const r=await fetch('/api/deobfuscate',{method:'POST',body:fd}); const j=await r.json();
    if(!j.ok){st.className='err'; st.textContent='error '+r.status+': '+j.error; out.textContent=j.log||''; out.hidden=!j.log; return;}
    st.textContent='done: '+j.result+' in '+j.elapsed+' s ('+j.output_bytes+' bytes)';
    out.textContent=j.output; out.hidden=false;
  }catch(err){st.className='err'; st.textContent='request failed: '+err;}
  finally{go.disabled=false;}
});
</script>
</body></html>
"""

@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML
