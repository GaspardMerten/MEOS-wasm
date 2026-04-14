# Emscripten toolchain wired to the local dep sysroot.
#
# Usage:
#   cmake -B build -S third_party/meos \
#     -DCMAKE_TOOLCHAIN_FILE=../cmake/wasm-toolchain.cmake \
#     -DMEOS_WASM_SYSROOT=<repo>/build/sysroot-wasm64
#
# The caller must also pass -sMEMORY64=1 via CMAKE_C_FLAGS/CMAKE_CXX_FLAGS
# for wasm64 builds (the top-level scripts/build.sh handles this).

include($ENV{EMSDK}/upstream/emscripten/cmake/Modules/Platform/Emscripten.cmake)

# Read the sysroot from environment when invoked via a try_compile
# (which does not propagate top-level cache variables).
if(NOT DEFINED MEOS_WASM_SYSROOT AND DEFINED ENV{MEOS_WASM_SYSROOT})
  set(MEOS_WASM_SYSROOT "$ENV{MEOS_WASM_SYSROOT}" CACHE PATH "")
endif()
if(NOT DEFINED MEOS_WASM_SYSROOT)
  message(FATAL_ERROR "Pass -DMEOS_WASM_SYSROOT=<sysroot dir> (and/or set env) built by deps/build-deps.sh")
endif()

list(APPEND CMAKE_PREFIX_PATH    "${MEOS_WASM_SYSROOT}")
list(APPEND CMAKE_FIND_ROOT_PATH "${MEOS_WASM_SYSROOT}")

# MEOS's hand-rolled Find*.cmake modules search for geos-config/binaries on
# PATH, not in CMAKE_PREFIX_PATH, so we pin the variables they expect directly.
set(GEOS_INCLUDE_DIR "${MEOS_WASM_SYSROOT}/include" CACHE PATH "")
set(GEOS_LIBRARY     "${MEOS_WASM_SYSROOT}/lib/libgeos_c.a;${MEOS_WASM_SYSROOT}/lib/libgeos.a" CACHE STRING "")
set(GEOS_CONFIG      "${MEOS_WASM_SYSROOT}/bin/geos-config" CACHE FILEPATH "")

set(PROJ_INCLUDE_DIRS  "${MEOS_WASM_SYSROOT}/include" CACHE PATH "")
set(PROJ_LIBRARIES     "${MEOS_WASM_SYSROOT}/lib/libproj.a;${MEOS_WASM_SYSROOT}/lib/libsqlite3.a" CACHE STRING "")
set(PROJ_VERSION_MAJOR 9 CACHE STRING "")
set(PROJ_VERSION_MINOR 5 CACHE STRING "")

set(JSON-C_INCLUDE_DIRS "${MEOS_WASM_SYSROOT}/include/json-c" CACHE PATH "")
set(JSON-C_LIBRARIES    "${MEOS_WASM_SYSROOT}/lib/libjson-c.a" CACHE STRING "")

set(GSL_INCLUDE_DIRS  "${MEOS_WASM_SYSROOT}/include" CACHE PATH "")
set(GSL_LIBRARY       "${MEOS_WASM_SYSROOT}/lib/libgsl.a" CACHE FILEPATH "")
set(GSL_CBLAS_LIBRARY "${MEOS_WASM_SYSROOT}/lib/libgslcblas.a" CACHE FILEPATH "")
set(GSL_VERSION "2.8" CACHE STRING "")
set(GSL_FOUND TRUE CACHE BOOL "")
