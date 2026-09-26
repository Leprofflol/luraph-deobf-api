"""
Obfuscator registry and detection (Luraph v15 only in this build).

deob.py picks the plugin whose detect() is most confident; `--obfuscator NAME`
forces one. Inputs no plugin is confident about are rejected (there is no
generic behaviour-trace fallback in this build).
"""
from obfuscators.base import Obfuscator, Job  # noqa: F401
from obfuscators.luraph_v15 import LuraphV15

PLUGINS = [
    LuraphV15(),
]

MIN_CONFIDENCE = 0.5    # below this the input counts as unrecognized


def by_name(name):
    for p in PLUGINS:
        if p.name == name:
            return p
    raise KeyError("unknown obfuscator %r (known: %s)" % (name, ", ".join(p.name for p in PLUGINS)))


def scores(source):
    """[(confidence, plugin)], most confident first."""
    out = []
    for p in PLUGINS:
        try:
            c = p.detect(source)
        except Exception:  # noqa: BLE001 - a broken detector must not stop the others
            c = 0.0
        out.append((c, p))
    return sorted(out, key=lambda cp: -cp[0])


def detect(source):
    """(plugin, confidence) for `source`; (None, confidence) when no plugin
    is confident enough."""
    best = scores(source)[0]
    if best[0] >= MIN_CONFIDENCE:
        return best[1], best[0]
    return None, best[0]
