# Luraph v15 Deobfuscator API

HTTP API (FastAPI) around the **Luraph v15** deobfuscator from `deobf`: a
protected Roblox Luau script goes in, readable Luau comes out. Ready to deploy
on **Vercel** (Python runtime, Fluid compute) and runs anywhere with `uvicorn`.

It is a *dynamic* deobfuscator: the protected script runs inside a real Luau
VM (`deobf/bin/luau`, built from Luau 0.739 with one patch) against a fake
Roblox/executor environment, the VM bytecode is **devirtualized** back to Luau
(control flow, locals, closures, untaken branches) and, when lifting is not
possible, a **behaviour trace** (everything the script did, rendered as Luau)
is returned instead. Nothing is stored and nothing touches the network.

> Only the Luraph v15 pipeline is included in this build. Other inputs are
> rejected with HTTP 400 (`force=true` runs the pipeline anyway).

---

## ภาษาไทย (สรุปสั้น ๆ)

- โปรเจกต์นี้คือ API สำหรับ deobfuscate สคริปต์ Roblox ที่ป้องกันด้วย **Luraph v15** เท่านั้น
- **Deploy บน Vercel:** กด *Add New → Project* เลือก repo นี้ → Vercel ตรวจเจอ FastAPI เอง → กด Deploy ได้เลย (ไม่ต้องตั้งค่าอะไรเพิ่ม, `vercel.json` ตั้ง `maxDuration` 300 วินาทีไว้แล้ว)
- **ใช้งาน:** `POST /api/deobfuscate` ส่งไฟล์แบบ multipart (field `file`), JSON `{"script": "..."}` หรือ raw text ก็ได้ → ได้ JSON กลับมา (`output` คือโค้ดที่ถอดแล้ว) หรือใส่ `?format=text` เพื่อรับไฟล์ .lua ตรง ๆ
- เปิดหน้าเว็บ `/` จะมีฟอร์มอัปโหลดให้ลองใช้ทันที
- **ข้อจำกัด:** แผน Hobby ของ Vercel ให้ฟังก์ชันรันได้สูงสุด 300 วินาที สคริปต์ใหญ่มาก ๆ ที่ต้องใช้เวลาหลายนาทีจะ timeout → ลอง `mode=trace` (เร็วกว่ามาก) หรือใช้แผน Pro แล้วเพิ่ม `maxDuration` ใน `vercel.json` (สูงสุด 800)

---

## Deploy to Vercel

1. Push this repository to GitHub (already done if you are reading it there).
2. In Vercel: **Add New → Project → Import** the repository. The FastAPI
   preset is detected automatically (`app.py` exposes `app`). No build
   command, no environment variables required.
3. Deploy. `vercel.json` sets `maxDuration: 300` and excludes `samples/`,
   `tests/` and `docs/` from the function bundle. Fluid compute (default for
   new projects) is required for the 300 s limit; on Hobby that is also the
   maximum. On Pro you may raise `maxDuration` up to 800.
4. Open `https://<your-app>.vercel.app/` for the upload page,
   `/docs` for the OpenAPI UI, `/api/health` to check the runtime.

Optional environment variables (Project → Settings → Environment Variables):

| Variable | Default | Meaning |
|---|---|---|
| `DEOBF_TOTAL_TIMEOUT` | `280` | wall-clock limit per request in seconds; keep it ~20 s under `maxDuration` |
| `DEOBF_RUN_TIMEOUT` | `90` | default `--timeout` (hard limit per harness run) |
| `DEOBF_BUDGET` | `30` | default `--budget` (soft time budget for the traced script) |
| `DEOBF_MAX_INPUT` | `4000000` | maximum script size in bytes (Vercel caps request bodies at 4.5 MB) |
| `DEOBF_CREDIT` | `Deobfuscated by ccjvwsod on Discord` | first comment line of every result (empty string removes it) |

The Luau binaries in `deobf/bin/` are **static Linux x86-64** builds, so they
run on Vercel's Amazon Linux image as-is. If the deployment strips their
executable bit the API copies them to `/tmp` and `chmod +x`es them on first use.

## API

### `POST /api/deobfuscate`

Input (any of):

- `multipart/form-data` with the script as field `file` (or as text field `script`); options as further fields
- `application/json`: `{"script": "<protected source>", "mode": "devirt", ...options}`
- any other content type: the raw body is the script; options as query parameters

Options (query parameters, form fields or JSON keys; body wins over query):

| Option | Default | Meaning |
|---|---|---|
| `mode` | `devirt` | `devirt`: lift the VM bytecode (falls back to the trace when lifting fails); `trace`: behaviour trace only, much faster |
| `force` | `false` | run the Luraph v15 pipeline even when the header / VM shape is not recognized |
| `format` | `json` | `text`: respond with the Lua file itself (`text/plain`, `Content-Disposition: attachment`) |
| `timeout` | `90` | hard limit per harness run, seconds |
| `budget` | `30` | soft time budget for the traced script, seconds |
| `executor` | `Wave` | name `identifyexecutor()` returns inside the sandbox |
| `input_text` | – | text TextBoxes contain when event handlers are traced (exercises key-check branches) |
| `cfg` | – | repeatable `KEY=VALUE` runtime options of the fake environment, e.g. `prelude=rawset(G,'webhook','x') setprop(game,'PlaceId',123)` or `falsy=IsLoaded` |
| `strings` | `false` | also dump every string the script builds (into the log) |
| `no_fold`, `keep_preamble` | `false` | trace rendering switches (see `docs/LURAPH.md`) |
| `max_runs`, `devirt_rounds` | `12`, `200` | anti-tamper trap reruns / constant-request rounds |

Response (`200`):

```json
{
  "ok": true,
  "obfuscator": {"name": "luraph_v15", "label": "Luraph v15", "confidence": 1.0, "forced": false},
  "mode": "devirt",
  "result": "devirtualized",
  "elapsed": 2.5,
  "output_bytes": 706,
  "output": "-- Deobfuscated by ...\n-- Detected obfuscation: Luraph v15\n...",
  "log": "[*] tracing ...\n[*] devirt round 1: 5 functions ...\n"
}
```

`result` is `"devirtualized"` (lifted bytecode) or `"trace"` (behaviour trace:
`mode=trace`, or lifting failed). Errors are JSON `{"ok": false, "error": ...}`
with status `400` (not a Luraph v15 script), `413` (too big), `422` (no
script), `500` (pipeline failed, `log` has the tail of its output) or `504`
(did not finish within `DEOBF_TOTAL_TIMEOUT`).

### `POST /api/detect`

Same input formats; no execution. Returns
`{"ok": true, "obfuscator": "luraph_v15" | null, "confidence": 0..1, "is_luraph_v15": bool}`.

### `GET /api/health`

Runtime check: Luau build, binary folder, Python version, limits.

### Examples

```bash
# multipart upload -> JSON
curl -F "file=@protected.lua" https://HOST/api/deobfuscate

# raw body -> the Lua file
curl --data-binary @protected.lua -H "Content-Type: text/plain" \
     "https://HOST/api/deobfuscate?format=text" -o deobfuscated.lua

# fast behaviour trace, forced pipeline
curl -F "file=@protected.lua" -F mode=trace -F force=true https://HOST/api/deobfuscate

# JSON body
curl -H "Content-Type: application/json" \
     -d '{"script": "...", "mode": "devirt", "cfg": ["prelude=rawset(G,\"key\",\"abc\")"]}' \
     https://HOST/api/deobfuscate
```

## Run locally

```bash
pip install -r requirements-dev.txt
uvicorn app:app --host 0.0.0.0 --port 8000     # http://localhost:8000/
python tests/smoke_test.py                       # end-to-end check on the bundled sample
python deobf/deob.py samples/001_vm_like_dispatch-obfuscated.lua   # CLI: -> samples/output/
```

Linux x86-64 works out of the box. On another platform rebuild the runtime:
`python deobf/build_luau.py --portable` (needs git, cmake, a C++ compiler;
writes `deobf/bin/luau`), plus the `Luau.Ast.CLI` target for `luau-ast`.

## Layout

```
app.py                     FastAPI app (Vercel entrypoint) - runs deobf/deob.py per request
vercel.json                maxDuration + excluded files
deobf/deob.py              CLI: detect -> Luraph v15 plugin -> one result file (--meta JSON summary)
deobf/obfuscators/         registry (Luraph v15 only) + luraph_v15/ (driver, devirt, vmmap)
deobf/harness.py           runs harnesses in the Luau VM (DEOBF_BIN overrides the binary folder)
deobf/envlog.luau ...      fake Roblox/executor environment (+ roblox_api, unicode_data, datatypes)
deobf/ir.py, backend.py, structure.py, loops.py, codegen.py, variables.py, idioms.py, names.py, ...
                           the lifter back end (bytecode -> Luau)
deobf/traceout.py, tidy.py, fold.py, spacing.py, lexer.py   trace rendering
deobf/bin/                 luau, luau-ast (static Linux x86-64, Luau 0.739 patched)
docs/LURAPH.md             how the Luraph v15 VM is handled (reference notes)
samples/                   one Luraph v15 sample with its source
```

## Limits and caveats

- **Time.** Small scripts take seconds; big commercial scripts can need
  5-15 minutes, which no Vercel plan allows in one request (Hobby 300 s,
  Pro 800 s). `mode=trace` is the fast path. For long jobs run the CLI on a
  normal server instead.
- **Memory / CPU.** Lifting is pure Python; one request may use hundreds of
  MB on large inputs. The default function memory is fine for typical scripts.
- **Concurrency.** Each request is an isolated subprocess in its own temp
  folder; several may run in one function instance under Fluid compute.
- **Environment fidelity.** Scripts that read settings from `_G`/`getgenv()`
  or check game state stop early; drive them further with `cfg=prelude=...`
  and `input_text` (see `docs/LURAPH.md`).

## Credits

The deobfuscator (`deobf/`) is the Luraph v15 part of the *deobf* project
("Deobfuscated by ccjvwsod on Discord"); this repository packages it as an
HTTP API and ships prebuilt Linux Luau binaries.
