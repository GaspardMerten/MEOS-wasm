#!/usr/bin/env python3
"""Auto-generate a JS façade for the MEOS C API.

Reads the installed MEOS public headers, intersects the declarations with
the symbols actually defined in libmeos.a, and emits a JS module that
exposes one method per MEOS function on a `MEOS` object.

Two output files:
  - <out>/meos-api.json  · machine-readable signature manifest
  - <out>/meos-api.js    · the runtime façade (UMD-ish, attaches MEOS to
                           globalThis when loaded as a classic <script>)

Usage:
  gen-bindings.py <meos-include-dir> <libmeos.a> <llvm-nm> <out-dir>
"""
from __future__ import annotations
import json, os, re, subprocess, sys

inc_dir, libmeos_a, llvm_nm, out_dir = sys.argv[1:5]
os.makedirs(out_dir, exist_ok=True)

# ────────────────────────────────────────────────────────────────────────
# Parse the headers — multi-line aware. We collapse whitespace inside
# parens then run a single regex over each header.
# ────────────────────────────────────────────────────────────────────────
# Per-declaration matcher (run on a single decl, no anchors).
DECL_RE = re.compile(
    r'^extern\s+(.+?)\s+(\*?\s*[a-zA-Z_][A-Za-z0-9_]*)\s*\((.*)\)$'
)

def strip_comments(src: str) -> str:
    """Remove /* */ and // comments AND string literals so paren-counting
    isn't fooled by unbalanced punctuation inside them."""
    out = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        nxt = src[i+1] if i+1 < n else ''
        if c == '/' and nxt == '*':
            j = src.find('*/', i+2)
            i = j + 2 if j >= 0 else n
            continue
        if c == '/' and nxt == '/':
            j = src.find('\n', i+2)
            i = j if j >= 0 else n
            continue
        if c == '"':
            j = i + 1
            while j < n and src[j] != '"':
                if src[j] == '\\': j += 2
                else: j += 1
            i = j + 1
            continue
        out.append(c)
        i += 1
    return ''.join(out)

def collapse(src: str) -> str:
    """Strip comments, swallow #define macro continuations, then collapse
    every declaration onto a single line by splitting on ; and joining."""
    src = strip_comments(src)
    # Remove preprocessor directives entirely — they include multi-line
    # macros whose embedded parens fool depth tracking.
    out_lines = []
    in_macro = False
    for line in src.split('\n'):
        stripped = line.lstrip()
        if in_macro:
            if not line.rstrip().endswith('\\'):
                in_macro = False
            continue
        if stripped.startswith('#'):
            if line.rstrip().endswith('\\'):
                in_macro = True
            continue
        out_lines.append(line)
    src = '\n'.join(out_lines)
    # Now collapse multi-line declarations: replace any whitespace between
    # the start of a non-blank line and the next ; with a single space.
    return re.sub(r'\s+', ' ', src)

decls = []
for fn in sorted(os.listdir(inc_dir)):
    if not fn.startswith('meos') or not fn.endswith('.h'):
        continue
    path = os.path.join(inc_dir, fn)
    src = collapse(open(path).read())
    # Each declaration ends in `;`. Split, trim, parse one at a time.
    for stmt in src.split(';'):
        stmt = stmt.strip()
        if not stmt.startswith('extern '):
            # Skip until we find an `extern` token (decls don't always start
            # at the beginning of the chunk, e.g. trailing whitespace).
            idx = stmt.find('extern ')
            if idx < 0: continue
            stmt = stmt[idx:]
        m = DECL_RE.match(stmt)
        if not m: continue
        # Re-arrange so the rest of the script keeps working unchanged.
        for _grp in [(m.group(1), m.group(2), m.group(3))]:
            class _M:
                def __init__(self, g): self._g = g
                def group(self, i): return self._g[i-1]
            m = _M(_grp); break
        ret_raw, name_raw, args_raw = m.group(1), m.group(2), m.group(3)
        name = name_raw.replace('*', '').strip()
        # ret_raw may end with extra '*' that belongs to the type
        # if name_raw started with '*'. Normalise:
        ret = (ret_raw + (' ' + name_raw[: name_raw.rfind('*') + 1])).strip() \
              if '*' in name_raw else ret_raw.strip()
        decls.append((ret, name, args_raw.strip(), fn))

# Deduplicate by name (last definition wins).
by_name: dict[str, tuple] = {}
for d in decls:
    by_name[d[1]] = d

# ────────────────────────────────────────────────────────────────────────
# Type classifier
# ────────────────────────────────────────────────────────────────────────
def is_pointer(t: str) -> bool:
    return '*' in t

NUMBER_TYPES = {
    'int', 'int8', 'int16', 'int32', 'int64',
    'int8_t', 'int16_t', 'int32_t', 'int64_t',
    'uint8', 'uint16', 'uint32', 'uint64',
    'uint8_t', 'uint16_t', 'uint32_t', 'uint64_t',
    'size_t', 'ssize_t', 'ptrdiff_t', 'long', 'short', 'unsigned',
    'unsigned int', 'unsigned long', 'unsigned short', 'char',
    'signed char', 'unsigned char',
    'double', 'float', 'long double',
    'TimestampTz', 'DateADT', 'TimeADT', 'meosType', 'lwflags_t',
    'mobdbType', 'TInterpolation', 'tempSubtype', 'meosOper',
}
# 64-bit integer scalars — under wasm64 these cross the FFI as BigInt.
BIGINT_TYPES = {
    'int64', 'int64_t', 'uint64', 'uint64_t',
    'long', 'unsigned long', 'size_t',
    'TimestampTz', 'Datum',
}
BOOL_TYPES = {'bool', '_Bool'}

def normalise_type(t: str) -> str:
    t = t.replace('const', '').strip()
    t = re.sub(r'\s+', ' ', t)
    return t

def js_type(c_type: str) -> str | None:
    """Map a C type to a ccall arg/return type. Returns None if unsupported."""
    c = normalise_type(c_type)
    if c in ('void',):
        return 'null'
    if is_pointer(c):
        # const char* → string for in-args; for return types we still want
        # 'string' so ccall does UTF8ToString.
        bare = c.replace('*', '').strip()
        if bare in ('char', 'unsigned char', 'uint8_t'):
            return 'string'  # may be wrong for binary buffers but covers most APIs
        return 'pointer'
    if c in BOOL_TYPES:
        return 'boolean'
    if c in BIGINT_TYPES:
        return 'bigint'
    if c in NUMBER_TYPES:
        return 'number'
    # Fallback: anything else we don't recognise is opaque — treat as pointer.
    return 'pointer'

def parse_args(args: str) -> list[tuple[str, str]]:
    """Split a C argument list into (type, name) pairs."""
    args = args.strip()
    if not args or args == 'void':
        return []
    out = []
    depth = 0
    buf = ''
    for ch in args:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ',' and depth == 0:
            out.append(buf.strip())
            buf = ''
        else:
            buf += ch
    out.append(buf.strip())

    parsed = []
    for arg in out:
        # Pull off the trailing identifier as the name; everything before is
        # the type. Pointer-to-pointer args have '*' on the name side.
        m = re.match(r'^(.*?)([a-zA-Z_][A-Za-z0-9_]*)\s*(\[[^\]]*\])?$', arg)
        if not m:
            parsed.append((arg, ''))
            continue
        c_type, name, _ = m.groups()
        c_type = c_type.strip()
        # Migrate any leading '*' from name onto type
        while name.startswith('*'):
            c_type += '*'
            name = name[1:]
        parsed.append((c_type or arg, name))
    return parsed

# ────────────────────────────────────────────────────────────────────────
# Build manifest
# ────────────────────────────────────────────────────────────────────────
nm_out = subprocess.check_output(
    [llvm_nm, '--defined-only', '--extern-only', libmeos_a],
    stderr=subprocess.DEVNULL,
).decode()
defined: set[str] = set()
for line in nm_out.splitlines():
    parts = line.split()
    if len(parts) >= 3 and parts[-2] in ('T', 'W', 'D'):
        defined.add(parts[-1])

manifest = {}
skipped: list[tuple[str, str]] = []

for name, (ret_raw, _, args_raw, fn) in by_name.items():
    if name not in defined:
        # Not actually present in libmeos.a — skip
        continue
    args = parse_args(args_raw)
    ret_js = js_type(ret_raw)
    arg_jss = [js_type(t) for t, _ in args]

    # Detect "output pointer" pattern: trailing arg is `<scalar> *out`
    # where the function returns bool/int. We wrap it specially.
    out_param = None
    if args:
        last_t, _ = args[-1]
        if is_pointer(last_t):
            bare = normalise_type(last_t).replace('*', '').strip()
            if bare in ('double', 'int', 'int32_t', 'int64_t',
                        'size_t', 'float', 'bool'):
                if normalise_type(ret_raw) in ('bool', '_Bool', 'int',
                                                'int32_t'):
                    out_param = bare

    if any(t is None for t in arg_jss) or ret_js is None:
        skipped.append((name, f'{ret_raw} ({args_raw})'))
        continue

    manifest[name] = {
        'file': fn,
        'ret': ret_js,
        'ret_c': normalise_type(ret_raw),
        'args': arg_jss,
        'arg_names': [n for _, n in args],
        'arg_types_c': [normalise_type(t) for t, _ in args],
        'out_param': out_param,
    }

with open(os.path.join(out_dir, 'meos-api.json'), 'w') as f:
    json.dump(manifest, f, indent=1, sort_keys=True)

# ────────────────────────────────────────────────────────────────────────
# Emit JS façade
# ────────────────────────────────────────────────────────────────────────
HEADER = """\
// AUTO-GENERATED — DO NOT EDIT.
// Façade over the MEOS C API exported into the wasm module.
// Generated by scripts/gen-bindings.py from MEOS public headers.
//
// Usage:
//   <script src="./meos.js"></script>
//   <script src="./meos-api.js"></script>
//   const Module = await createMeos();
//   MEOS.bindModule(Module);                          // wires up the façade
//   const handle = MEOS.tgeompoint_in('POINT(...)@2026-04-14 09:00');
//   const length = MEOS.tpoint_length(handle);
//   MEOS.temporal_destroy_safe(handle);               // destroy any handle
//
// Functions whose last argument is a scalar output pointer (e.g.
//   bool tfloat_value_at_timestamptz(temp, t, strict, double *value)
// are wrapped to allocate the scratch automatically and return the value
// directly (or null if the function returned false).
//
"""

js = [HEADER, "(function (root) {", "  const MEOS = {};", "  let M = null;",
      "  let scratchPtr = 0;", "  let scratchOff = 0;",
      "  MEOS.bindModule = function bindModule(mod) {",
      "    M = mod;",
      "    scratchPtr = M._malloc(32n);     // 4 × f64",
      "    scratchOff = Number(scratchPtr) / 8;",
      "    for (const name of Object.keys(MEOS.__defs)) MEOS.__build(name);",
      "  };",
      "  MEOS.__defs = " + json.dumps({k: {'r': v['ret'], 'a': v['args'], 'o': v['out_param']} for k, v in manifest.items()}, separators=(',', ':')) + ";",
      "  MEOS.__build = function (name) {",
      "    const def = MEOS.__defs[name];",
      "    if (!def) return;",
      "    const ret = def.r, args = def.a, op = def.o;",
      "    const hasBigInt = ret === 'bigint' || args.indexOf('bigint') >= 0;",
      "    // Direct wasm export bypassing ccall when bigint types are involved.",
      "    function callDirect(argv) {",
      "      const exp = M['_' + name];",
      "      if (!exp) return null;",
      "      const stack = M.stackSave ? M.stackSave() : 0;",
      "      const marshalled = argv.map(function (v, i) {",
      "        const t = args[i];",
      "        if (t === 'bigint') {",
      "          if (typeof v === 'bigint') return v;",
      "          if (typeof v === 'number') return BigInt(Math.trunc(v));",
      "          if (typeof v === 'string') { try { return BigInt(v); } catch { return 0n; } }",
      "          return 0n;",
      "        }",
      "        if (t === 'pointer') {",
      "          if (typeof v === 'bigint') return v;",
      "          return BigInt(Math.trunc(v || 0));",
      "        }",
      "        if (t === 'string') {",
      "          if (v == null) return 0n;",
      "          const addr = M.stringToUTF8OnStack(String(v));",
      "          return BigInt(addr);",
      "        }",
      "        if (t === 'boolean') return v ? 1 : 0;",
      "        return v;",
      "      });",
      "      let r;",
      "      try { r = exp.apply(null, marshalled); }",
      "      catch (e) { if (stack) M.stackRestore(stack); return null; }",
      "      if (stack) M.stackRestore(stack);",
      "      if (ret === 'pointer')  return typeof r === 'bigint' ? Number(r) : r;",
      "      if (ret === 'bigint')   return r;",
      "      if (ret === 'string')   return r ? M.UTF8ToString(Number(r)) : null;",
      "      if (ret === 'boolean')  return Boolean(r);",
      "      if (ret === 'null')     return null;",
      "      return typeof r === 'bigint' ? Number(r) : r;",
      "    }",
      "    if (op) {",
      "      MEOS[name] = function () {",
      "        const rest = Array.prototype.slice.call(arguments);",
      "        rest.push(scratchPtr);",
      "        callDirect(rest);",
      "        if (op === 'double' || op === 'float') return M.HEAPF64[scratchOff];",
      "        if (op === 'int'    || op === 'int32_t') return M.HEAP32[Number(scratchPtr) / 4];",
      "        if (op === 'int64_t'|| op === 'int64' || op === 'uint64' || op === 'uint64_t') return M.HEAPF64[scratchOff];",
      "        if (op === 'size_t') return M.HEAPU32 ? M.HEAPU32[Number(scratchPtr) / 4] : M.HEAPF64[scratchOff];",
      "        if (op === 'bool')   return Boolean(M.HEAPU8[Number(scratchPtr)]);",
      "        return null;",
      "      };",
      "    } else if (hasBigInt) {",
      "      MEOS[name] = function () {",
      "        return callDirect(Array.prototype.slice.call(arguments));",
      "      };",
      "    } else {",
      "      MEOS[name] = function () {",
      "        try { return M.ccall(name, ret, args, Array.prototype.slice.call(arguments)); }",
      "        catch (e) { return null; }",
      "      };",
      "    }",
      "  };",
      "  // Convenience: free any pointer returned by MEOS functions.",
      "  MEOS.free = function (ptr) { if (ptr) M._free(ptr); };",
      "  // Best-effort destroy. Most MEOS handles can be released via _free,",
      "  // matching how the upstream functions allocate them.",
      "  MEOS.temporal_destroy_safe = function (h) { MEOS.free(h); };",
      "  if (typeof module !== 'undefined' && module.exports) module.exports = MEOS;",
      "  root.MEOS = MEOS;",
      "})(typeof globalThis !== 'undefined' ? globalThis : window);",
      ""]

with open(os.path.join(out_dir, 'meos-api.js'), 'w') as f:
    f.write('\n'.join(js))

# ────────────────────────────────────────────────────────────────────────
# TypeScript declarations
# ────────────────────────────────────────────────────────────────────────
def ts_type(js_type: str) -> str:
    return {
        'pointer' : 'number',   # ccall converts BigInt → Number on return
        'number'  : 'number',
        'string'  : 'string',
        'boolean' : 'boolean',
        'null'    : 'void',
    }.get(js_type, 'unknown')

def safe_ident(name: str, idx: int) -> str:
    if not name or not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
        return f'arg{idx}'
    if name in ('class', 'function', 'default', 'new', 'delete', 'void'):
        return name + '_'
    return name

ts_lines = [
    '// AUTO-GENERATED — DO NOT EDIT.',
    '// TypeScript declarations for the MEOS C API exposed via web/meos-api.js.',
    '// Generated by scripts/gen-bindings.py from MEOS public headers.',
    '',
    'export interface MEOSModule {',
    '  /** Wire the façade to a loaded Emscripten Module instance. */',
    '  bindModule(mod: any): void;',
    '  /** Free any pointer returned by a MEOS function. */',
    '  free(ptr: number): void;',
    '  /** Best-effort destroy for any Temporal/Set/Span/STBox handle. */',
    '  temporal_destroy_safe(handle: number): void;',
    '',
]
for name in sorted(manifest.keys()):
    info = manifest[name]
    args  = info['args']
    names = info['arg_names']
    cargs = info['arg_types_c']
    ret   = info['ret']
    ret_c = info['ret_c']
    out_p = info['out_param']
    if out_p:
        # Drop the trailing scratch arg
        ts_args = []
        for i, (jt, cn, ct) in enumerate(list(zip(args, names, cargs))[:-1]):
            ident = safe_ident(cn, i)
            ts_args.append(f'{ident}: {ts_type(jt)}')
        out_ts = 'number | null' if out_p in ('double', 'float', 'int',
                                                 'int32_t', 'int64_t', 'size_t') \
                else 'boolean | null' if out_p == 'bool' \
                else 'unknown'
        ts_lines.append(f'  /** `{ret_c} {name}({", ".join(cargs)})` (out-param wrapped) */')
        ts_lines.append(f'  {name}({", ".join(ts_args)}): {out_ts};')
    else:
        ts_args = []
        for i, (jt, cn, ct) in enumerate(zip(args, names, cargs)):
            ts_args.append(f'{safe_ident(cn, i)}: {ts_type(jt)}')
        ts_lines.append(f'  /** `{ret_c} {name}({", ".join(cargs)})` */')
        ts_lines.append(f'  {name}({", ".join(ts_args)}): {ts_type(ret)};')
ts_lines.append('}')
ts_lines.append('')
ts_lines.append('declare const MEOS: MEOSModule;')
ts_lines.append('export default MEOS;')
ts_lines.append('declare global { interface Window { MEOS: MEOSModule; } }')
ts_lines.append('')

with open(os.path.join(out_dir, 'meos-api.d.ts'), 'w') as f:
    f.write('\n'.join(ts_lines))

print(f'  bindings: {len(manifest)} fns ({sum(1 for v in manifest.values() if v["out_param"])} with out-params)')
print(f'  ts decls: web/meos-api.d.ts ({len(ts_lines)} lines)')
if skipped:
    print(f'  skipped:  {len(skipped)}')
