"""
Smoke test: runs the bundled Luraph v15 sample through the API in-process.

    pip install -r requirements-dev.txt
    python tests/smoke_test.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402

SAMPLE = os.path.join(ROOT, "samples", "001_vm_like_dispatch-obfuscated.lua")
PLAIN = os.path.join(ROOT, "samples", "001_vm_like_dispatch.lua")


def main():
    c = TestClient(app.app)
    h = c.get("/api/health").json()
    assert h["ok"], h
    print("[+] health:", h["luau"])

    with open(SAMPLE, "rb") as f:
        r = c.post("/api/detect", files={"file": ("sample.lua", f.read())})
    assert r.status_code == 200 and r.json()["is_luraph_v15"], r.text
    print("[+] detect: Luraph v15, confidence", r.json()["confidence"])

    with open(SAMPLE, "rb") as f:
        r = c.post("/api/deobfuscate", files={"file": ("sample.lua", f.read())})
    j = r.json()
    assert r.status_code == 200 and j["ok"], r.text
    assert j["result"] == "devirtualized", j["result"]
    assert "handlers[tbl[i][1]](tbl[i])" in j["output"] and "assert(n == 1 and tbl2[1] == 34)" in j["output"]
    print("[+] deobfuscate: %s in %ss, %d bytes" % (j["result"], j["elapsed"], j["output_bytes"]))

    with open(SAMPLE, "rb") as f:
        r = c.post("/api/deobfuscate?format=text&mode=trace", content=f.read(),
                   headers={"Content-Type": "text/plain"})
    assert r.status_code == 200 and r.headers["x-deobf-result"] == "trace", r.text[:300]
    print("[+] trace mode as text: %d bytes" % len(r.text))

    with open(PLAIN, "rb") as f:
        r = c.post("/api/deobfuscate", content=f.read(), headers={"Content-Type": "text/plain"})
    assert r.status_code == 400, r.text
    print("[+] plain script rejected with 400")
    print("all good")


if __name__ == "__main__":
    main()
