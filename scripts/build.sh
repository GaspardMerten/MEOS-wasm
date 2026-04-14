#!/usr/bin/env bash
# Orchestrates the full wasm build:
#   1. build/sysroot-$TARGET         (deps/build-deps.sh)
#   2. build/meos-$TARGET/libmeos.a  (CMake + Emscripten toolchain)
#   3. web/meos.{js,wasm,data}       (emcc link with src/glue.c)
#
# Usage:
#   source <emsdk>/emsdk_env.sh
#   ./scripts/build.sh                # wasm64 (default)
#   TARGET=wasm32 ./scripts/build.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET="${TARGET:-wasm64}"
JOBS="${JOBS:-$(nproc)}"
SYSROOT="$ROOT/build/sysroot-$TARGET"
MEOS_SRC="$ROOT/third_party/meos"
MEOS_BUILD="$ROOT/build/meos-$TARGET"
MEOS_INSTALL="$ROOT/build/meos-$TARGET-install"
WEB="$ROOT/web"

case "$TARGET" in
  wasm64) EM_FLAGS="-sMEMORY64=1" ;;
  wasm32) EM_FLAGS="" ;;
  *) echo "TARGET must be wasm32 or wasm64" >&2; exit 1 ;;
esac

command -v emcc   >/dev/null || { echo "source emsdk_env.sh first" >&2; exit 1; }

log() { printf '\n\033[1;32m[build]\033[0m %s\n' "$*"; }

# ── 1. deps ──────────────────────────────────────────────
log "dependencies → $SYSROOT"
TARGET="$TARGET" SYSROOT="$SYSROOT" "$ROOT/deps/build-deps.sh"

# ── 2. MEOS ──────────────────────────────────────────────
log "MEOS → $MEOS_INSTALL"
mkdir -p "$MEOS_BUILD"
MEOS_WASM_SYSROOT="$SYSROOT" \
cmake -S "$MEOS_SRC" -B "$MEOS_BUILD" \
  -DCMAKE_TOOLCHAIN_FILE="$ROOT/cmake/wasm-toolchain.cmake" \
  -DMEOS_WASM_SYSROOT="$SYSROOT" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=OFF \
  -DCMAKE_C_FLAGS="$EM_FLAGS -fvisibility=default" \
  -DCMAKE_CXX_FLAGS="$EM_FLAGS -fvisibility=default" \
  >/dev/null
cmake --build "$MEOS_BUILD" --parallel "$JOBS"
rm -rf "$MEOS_INSTALL"
DESTDIR="$MEOS_INSTALL" cmake --install "$MEOS_BUILD" >/dev/null

# The install prefix is Emscripten's sysroot (a hardcoded path inside the
# install tree). Find the actual include/ and lib/ directories.
MEOS_INC="$(find "$MEOS_INSTALL" -type d -name include -path '*/sysroot/*' | head -1)"
MEOS_LIB="$(find "$MEOS_INSTALL" -type d -name lib     -path '*/sysroot/*' | head -1)"
[[ -d "$MEOS_INC" && -d "$MEOS_LIB" ]] || { echo "install layout unexpected" >&2; exit 1; }

# ── 3. link the browser module ───────────────────────────
log "scan MEOS headers → exports manifest"
EXPORTS_FILE="$ROOT/build/meos-exports-$TARGET.json"
python3 "$ROOT/scripts/gen-exports.py" "$MEOS_INC" "$MEOS_LIB/libmeos.a" "$EMSDK/upstream/bin/llvm-nm" "$EXPORTS_FILE"

log "generate JS façade for MEOS API"
python3 "$ROOT/scripts/gen-bindings.py" "$MEOS_INC" "$MEOS_LIB/libmeos.a" "$EMSDK/upstream/bin/llvm-nm" "$WEB"

log "link web/meos.{js,wasm,data}"
mkdir -p "$WEB"
emcc -O2 $EM_FLAGS \
  -I"$MEOS_INC" -I"$SYSROOT/include" \
  -o "$WEB/meos.js" "$ROOT/src/glue.c" \
  -Wl,--whole-archive "$MEOS_LIB/libmeos.a" -Wl,--no-whole-archive \
  "$SYSROOT/lib/libgeos_c.a" "$SYSROOT/lib/libgeos.a" \
  "$SYSROOT/lib/libproj.a"   "$SYSROOT/lib/libsqlite3.a" \
  "$SYSROOT/lib/libjson-c.a" \
  "$SYSROOT/lib/libgsl.a"    "$SYSROOT/lib/libgslcblas.a" \
  -sALLOW_MEMORY_GROWTH=1 -sEXIT_RUNTIME=0 -sSTACK_SIZE=5MB -sWASM_BIGINT \
  -sMODULARIZE=1 -sEXPORT_NAME=createMeos -sENVIRONMENT=web \
  -sLINKABLE=1 \
  -sEXPORT_ALL=1 \
  -sEXPORTED_FUNCTIONS=_malloc,_free,_meos_start,_meos_stop,_meos_version_string,_meos_parse_tgeompoint,_meos_parse_tgeogpoint,_meos_tgeompoint_create,_meos_temporal_destroy,_meos_temporal_start_epoch_ms,_meos_temporal_end_epoch_ms,_meos_temporal_num_instants,_meos_temporal_interp_name,_meos_temporal_srid,_meos_tgeompoint_value_at,_meos_batch_value_at,_meos_temporal_as_ewkt,_meos_temporal_as_mfjson \
  -sEXPORTED_RUNTIME_METHODS=ccall,cwrap,UTF8ToString,HEAPU8,HEAPF64,getValue,setValue \
  --preload-file "$SYSROOT/share/proj@/share/proj" \
  --preload-file /usr/share/zoneinfo@/usr/share/zoneinfo

ls -lh "$WEB/meos.js" "$WEB/meos.wasm" "$WEB/meos.data"
log "done. serve with ./scripts/serve.sh"
