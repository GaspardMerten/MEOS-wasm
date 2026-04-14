#!/usr/bin/env bash
# Cross-compile MEOS's C dependencies (GEOS, PROJ, JSON-C, GSL, SQLite)
# into a local sysroot for wasm64 via Emscripten.
#
# Usage:
#   source <emsdk>/emsdk_env.sh
#   ./deps/build-deps.sh         # builds everything into build/sysroot-wasm64
#   TARGET=wasm32 ./deps/build-deps.sh
#
# Env overrides:
#   TARGET=wasm32|wasm64 (default wasm64)
#   SYSROOT=<path>       (default build/sysroot-$TARGET)
#   JOBS=<n>             (default nproc)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=deps/versions.sh
source "$SCRIPT_DIR/versions.sh"

TARGET="${TARGET:-wasm64}"
SYSROOT="${SYSROOT:-$ROOT/build/sysroot-$TARGET}"
JOBS="${JOBS:-$(nproc)}"
WORK="$ROOT/build/deps-work"

case "$TARGET" in
  wasm64) EM_FLAGS="-sMEMORY64=1" ;;
  wasm32) EM_FLAGS="" ;;
  *) echo "TARGET must be wasm32 or wasm64" >&2; exit 1 ;;
esac

if ! command -v emcmake >/dev/null; then
  echo "error: emcmake not on PATH. Source <emsdk>/emsdk_env.sh first." >&2
  exit 1
fi

mkdir -p "$SYSROOT"/{bin,include,lib} "$WORK"
cd "$WORK"

log() { printf '\n\033[1;34m[deps]\033[0m %s\n' "$*"; }

# ───────────────────────────────────────── SQLite (for PROJ)
if [[ ! -f "$SYSROOT/lib/libsqlite3.a" ]]; then
  log "SQLite $SQLITE_VERSION"
  [[ -d sqlite-amalgamation-$SQLITE_VERSION ]] || {
    curl -sSL "https://www.sqlite.org/2024/sqlite-amalgamation-$SQLITE_VERSION.zip" -o sqlite.zip
    unzip -q sqlite.zip
  }
  ( cd "sqlite-amalgamation-$SQLITE_VERSION"
    emcc $EM_FLAGS -O2 \
      -DSQLITE_OMIT_LOAD_EXTENSION -DSQLITE_THREADSAFE=0 -DSQLITE_OMIT_DEPRECATED \
      -c sqlite3.c -o sqlite3.o
    emar rcs "$SYSROOT/lib/libsqlite3.a" sqlite3.o
    cp sqlite3.h sqlite3ext.h "$SYSROOT/include/"
  )
  # Native sqlite3 CLI needed by PROJ to bake proj.db at build time.
  if [[ ! -x "$SYSROOT/bin/sqlite3" ]]; then
    ( cd "sqlite-amalgamation-$SQLITE_VERSION"
      cc -O2 -DSQLITE_THREADSAFE=0 shell.c sqlite3.c -lm -o "$SYSROOT/bin/sqlite3"
    )
  fi
fi

# ───────────────────────────────────────── GEOS
if [[ ! -f "$SYSROOT/lib/libgeos.a" ]]; then
  log "GEOS $GEOS_VERSION"
  [[ -d geos ]] || git clone --depth 1 -b "$GEOS_VERSION" https://github.com/libgeos/geos.git
  mkdir -p geos/build-$TARGET && cd geos/build-$TARGET
  emcmake cmake .. \
    -DCMAKE_INSTALL_PREFIX="$SYSROOT" -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF -DBUILD_TESTING=OFF -DBUILD_GEOSOP=OFF \
    -DBUILD_DOCUMENTATION=OFF -DGEOS_BUILD_DEVELOPER=OFF \
    -DCMAKE_C_FLAGS="$EM_FLAGS" -DCMAKE_CXX_FLAGS="$EM_FLAGS" >/dev/null
  emmake make -j"$JOBS" install >/dev/null
  cd "$WORK"
fi

# ───────────────────────────────────────── JSON-C
if [[ ! -f "$SYSROOT/lib/libjson-c.a" ]]; then
  log "JSON-C $JSONC_VERSION"
  [[ -d json-c ]] || git clone --depth 1 -b "json-c-$JSONC_VERSION" https://github.com/json-c/json-c.git
  mkdir -p json-c/build-$TARGET && cd json-c/build-$TARGET
  emcmake cmake .. \
    -DCMAKE_INSTALL_PREFIX="$SYSROOT" -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF -DBUILD_STATIC_LIBS=ON -DBUILD_TESTING=OFF \
    -DDISABLE_BSYMBOLIC=ON -DDISABLE_THREAD_LOCAL_STORAGE=ON -DENABLE_THREADING=OFF \
    -DDISABLE_WERROR=ON -DDISABLE_EXTRA_LIBS=ON \
    -DCMAKE_C_FLAGS="$EM_FLAGS" -DCMAKE_CXX_FLAGS="$EM_FLAGS" >/dev/null
  emmake make -j"$JOBS" install >/dev/null
  cd "$WORK"
fi

# ───────────────────────────────────────── GSL
if [[ ! -f "$SYSROOT/lib/libgsl.a" ]]; then
  log "GSL $GSL_VERSION"
  [[ -d gsl-$GSL_VERSION ]] || {
    curl -sSL "https://ftp.gnu.org/gnu/gsl/gsl-$GSL_VERSION.tar.gz" -o gsl.tar.gz
    tar xf gsl.tar.gz
  }
  ( cd "gsl-$GSL_VERSION"
    make distclean >/dev/null 2>&1 || true
    emconfigure ./configure \
      CFLAGS="$EM_FLAGS -O2" LDFLAGS="$EM_FLAGS" \
      --prefix="$SYSROOT" --host=none --disable-shared --enable-static >/dev/null
    emmake make -j"$JOBS" >/dev/null
    emmake make install >/dev/null
  )
fi

# ───────────────────────────────────────── PROJ (after SQLite)
if [[ ! -f "$SYSROOT/lib/libproj.a" ]]; then
  log "PROJ $PROJ_VERSION"
  [[ -d proj-$PROJ_VERSION ]] || {
    curl -sSL "https://download.osgeo.org/proj/proj-$PROJ_VERSION.tar.gz" -o proj.tar.gz
    tar xf proj.tar.gz
  }
  mkdir -p "proj-$PROJ_VERSION/build-$TARGET" && cd "proj-$PROJ_VERSION/build-$TARGET"
  PATH="$SYSROOT/bin:$PATH" emcmake cmake .. \
    -DCMAKE_INSTALL_PREFIX="$SYSROOT" -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF -DBUILD_TESTING=OFF -DBUILD_APPS=OFF \
    -DENABLE_CURL=OFF -DENABLE_TIFF=OFF -DBUILD_PROJSYNC=OFF \
    -DSQLITE3_INCLUDE_DIR="$SYSROOT/include" \
    -DSQLITE3_LIBRARY="$SYSROOT/lib/libsqlite3.a" \
    -DEXE_SQLITE3="$SYSROOT/bin/sqlite3" \
    -DCMAKE_C_FLAGS="$EM_FLAGS" -DCMAKE_CXX_FLAGS="$EM_FLAGS" >/dev/null
  PATH="$SYSROOT/bin:$PATH" emmake make -j"$JOBS" install >/dev/null
  cd "$WORK"
fi

log "sysroot ready at $SYSROOT"
ls -1 "$SYSROOT/lib"/lib*.a
