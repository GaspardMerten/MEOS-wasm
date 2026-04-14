# meos-wasm

MEOS — the Moving Entity Objects engine that powers MobilityDB — compiled to
WebAssembly (32-bit *and* 64-bit / MEMORY64) with all of its C dependencies
statically linked. Parse WKT temporal geometry, reproject through PROJ, and
emit OGC MF-JSON in a single 3.6 MB `.wasm` module, entirely client-side.

Upstream MEOS lives in `third_party/meos/` as a git submodule pinned to a
release tag. **Zero patches** are applied — the wasm64 ABI is LP64, so
MEOS's Postgres-derived code builds cleanly out of the box.

## Repository layout

```
meos-wasm/
├── third_party/meos/         # submodule → MobilityDB/MEOS (pinned)
├── deps/
│   ├── versions.sh           # pinned C dep versions
│   └── build-deps.sh         # cross-compiles GEOS + PROJ + SQLite + JSON-C + GSL
├── cmake/
│   └── wasm-toolchain.cmake  # Emscripten + sysroot wiring for MEOS's CMake
├── src/
│   └── glue.c                # C shim — the JS-facing API surface
├── scripts/
│   ├── build.sh              # deps → libmeos.a → web/meos.wasm
│   └── serve.sh              # python http.server on :8765
├── test/
│   └── smoke.mjs             # parses a trajectory under node, asserts round-trip
└── web/                      # showcase page + bundled .wasm/.data artifacts
```

## Build

```bash
# 1. Prerequisites
git submodule update --init --recursive
source <path-to-emsdk>/emsdk_env.sh

# 2. Full build (deps + MEOS + glue + web artifacts)
./scripts/build.sh              # wasm64, default
TARGET=wasm32 ./scripts/build.sh   # wasm32 variant

# 3. Serve the showcase
./scripts/serve.sh              # http://localhost:8765
```

First run takes ~10 minutes (deps are the slow part). Subsequent runs skip
already-built libraries in `build/sysroot-$TARGET/`.

## Usage (browser)

```html
<script src="./meos.js"></script>
<script type="module">
  const Module = await createMeos();
  const parse  = (wkt) => Module.ccall('meos_parse_tgeompoint', 'string', ['string'], [wkt]);
  const json   = parse('[POINT(4.36 50.84)@2026-04-14 09:00:00, POINT(4.50 50.88)@2026-04-14 09:30:00]');
  console.log(JSON.parse(json));
</script>
```

## Exported C functions

| C symbol                    | JS type              | Notes |
|-----------------------------|----------------------|-------|
| `meos_start`                | `() → void`          | Idempotent. Called lazily on first parse. |
| `meos_stop`                 | `() → void`          | Releases MEOS global state. |
| `meos_parse_tgeompoint`     | `(string) → string`  | Returns MF-JSON, `NULL` on parse failure. |
| `meos_parse_tgeogpoint`     | `(string) → string`  | WGS84 variant. |
| `meos_version_string`       | `() → string`        | Build-time MobilityDB version. |

The glue layer lives in [`src/glue.c`](src/glue.c) and is intentionally
minimal. Extend it as you need more of MEOS's ~2000 API functions.

## Targets

**wasm64 / MEMORY64=1** is the default. The binary declares i64-indexed
memory, compiles with LP64 sizes (`sizeof(long) = sizeof(void*) = 8`), and
returns pointers to JS as `BigInt`. This matches the x86_64 ABI that MEOS
was originally written for and requires zero source modifications.

**wasm32** works too via `TARGET=wasm32 ./scripts/build.sh`, but needs the
Postgres-derived code to be patched for ILP32 (`long = 4`, `void* = 4`).
Those patches are not yet applied here — they're in the parent project.
PRs welcome if you need a wasm32 build.

## Runtime requirements

- **Browsers**: any modern Chrome / Firefox / Safari with WebAssembly
  Memory64 enabled. Recent releases ship it on by default; older ones need
  `chrome://flags/#enable-experimental-webassembly-features`.
- **Node.js**: v23 or later. v22 rejects wasm64 table encodings.

## License

MEOS is released under the PostgreSQL License. This wrapper inherits that
license — see [`LICENSE`](LICENSE) and [`third_party/meos/LICENSE.txt`](third_party/meos/LICENSE.txt).


## Testing

Three progressively deeper tests live under `test/`:

| Script | What it checks |
|---|---|
| `test/smoke.mjs`        | Node.js round-trip — parses a WKT trajectory, asserts the MF-JSON comes back with the right timestamp. Needs Node ≥ 23. |
| `test/browser_full.py`  | Headless Firefox acceptance — 30 assertions covering every UI surface (datasets, format lab, function explorer, HUD, simplify slider, façade probe, error capture). |
| `test/coverage.py`      | Walks every function in the auto-generated façade (2,353 of them), picks the most appropriate sample handle per pointer arg, executes, and reports per-category pass/fail. |

```bash
# Acceptance + coverage (assumes the server is running)
./scripts/serve.sh &
python3 -m venv /tmp/venv && /tmp/venv/bin/pip install selenium
FIREFOX_BIN=$(which firefox) /tmp/venv/bin/python test/browser_full.py
FIREFOX_BIN=$(which firefox) /tmp/venv/bin/python test/coverage.py
```

## Continuous integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push and PR:

1. Checkout with submodules (pulls upstream MEOS)
2. Install emsdk 5.0.5 (cached across runs)
3. Cross-compile the dependency sysroot (GEOS + PROJ + SQLite + JSON-C + GSL), cached by the hash of `deps/build-deps.sh` + `deps/versions.sh`
4. Run `./scripts/build.sh` to build MEOS, generate the façade, link the wasm module
5. Start a local HTTP server
6. Run `test/browser_full.py` and `test/coverage.py` against headless Firefox-ESR
7. Upload `web/meos.wasm`, `web/meos-api.*`, and the coverage JSON as build artifacts

The coverage step has a `--fail-threshold 0.20` gate — runs are rejected if the productive/callable ratio drops below 20 %.
