/*
 * meos-wasm · JS-facing C shim
 *
 * Two layers of API:
 *
 *   1. High-level helpers tagged with EMSCRIPTEN_KEEPALIVE + exported
 *      explicitly via EXPORTED_FUNCTIONS. These are the fast paths the
 *      showcase uses every frame: lifecycle (start/stop), WKT→MF-JSON
 *      round-trip, handle-based value-at-timestamp for real MEOS-driven
 *      interpolation.
 *
 *   2. The whole MEOS public API, kept alive by `-Wl,--whole-archive`
 *      on libmeos.a and re-exported by `-sEXPORT_ALL=1` in the emcc
 *      link step. Any function in meos.h / meos_geo.h / meos_internal.h
 *      is then reachable from JS via Module.ccall('<name>', ...) without
 *      touching this file.
 *
 * Memory ownership:
 *   - char* returns are malloc'd by MEOS; JS consumers use ccall('string')
 *     and accept the one-alloc-per-call leak, or call Module._free() on
 *     the raw pointer version.
 *   - Temporal* handles returned by *_create are owned by JS and must be
 *     released via meos_temporal_destroy.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <meos.h>
#include <meos_geo.h>
#include <emscripten.h>

/* POINT4D + GSERIALIZED are defined in meos_geo.h (it re-exports the
 * liblwgeom public types). We only need to forward-declare the peek
 * helper, which lives in liblwgeom and is linked in via libmeos.a. */
extern int gserialized_peek_first_point(const GSERIALIZED *g, POINT4D *out_point);

static int g_initialized = 0;

/* Swallow MEOS errors so a malformed input never abort()s the wasm
 * process. JS code can still see them via meos_errno. */
static void meos_noop_error_handler(int err_level, int err_code, const char *msg) {
  (void)err_level; (void)err_code; (void)msg;
}

/* ────────────────────────────── lifecycle ────────────────────────────── */

EMSCRIPTEN_KEEPALIVE
void meos_start(void) {
  if (!g_initialized) {
    meos_initialize();
    meos_initialize_error_handler(meos_noop_error_handler);
    g_initialized = 1;
  }
}

EMSCRIPTEN_KEEPALIVE
void meos_stop(void) {
  if (g_initialized) {
    meos_finalize();
    g_initialized = 0;
  }
}

EMSCRIPTEN_KEEPALIVE
const char *meos_version_string(void) {
  #ifdef MEOS_WASM_VERSION_STRING
  return MEOS_WASM_VERSION_STRING;
#else
  return "MEOS wasm build";
#endif
}

/* ────────────────────────── WKT → MF-JSON path ──────────────────────── */

EMSCRIPTEN_KEEPALIVE
char *meos_parse_tgeompoint(const char *wkt) {
  meos_start();
  Temporal *t = tgeompoint_in(wkt);
  if (!t) return NULL;
  char *s = temporal_as_mfjson(t, true, 3, 6, NULL);
  free(t);
  return s;
}

EMSCRIPTEN_KEEPALIVE
char *meos_parse_tgeogpoint(const char *wkt) {
  meos_start();
  Temporal *t = tgeogpoint_in(wkt);
  if (!t) return NULL;
  char *s = temporal_as_mfjson(t, true, 3, 6, NULL);
  free(t);
  return s;
}

/* ────────────────────────── handle-based API ────────────────────────── */
/* JS keeps opaque pointers to Temporal* objects and queries them per
 * animation frame. This lets MEOS itself do the interpolation — linear,
 * step, or discrete — instead of the JS draw loop. */

EMSCRIPTEN_KEEPALIVE
Temporal *meos_tgeompoint_create(const char *wkt) {
  meos_start();
  return tgeompoint_in(wkt);
}

EMSCRIPTEN_KEEPALIVE
void meos_temporal_destroy(Temporal *t) {
  if (t) free(t);
}

EMSCRIPTEN_KEEPALIVE
double meos_temporal_start_epoch_ms(const Temporal *t) {
  if (!t) return 0.0;
  TimestampTz ts = temporal_start_timestamptz(t);
  /* TimestampTz is microseconds since 2000-01-01 00:00:00 UTC.
   * 946684800000 ms = 2000-01-01T00:00:00Z in JS epoch. */
  return (double)ts / 1000.0 + 946684800000.0;
}

EMSCRIPTEN_KEEPALIVE
double meos_temporal_end_epoch_ms(const Temporal *t) {
  if (!t) return 0.0;
  TimestampTz ts = temporal_end_timestamptz(t);
  return (double)ts / 1000.0 + 946684800000.0;
}

EMSCRIPTEN_KEEPALIVE
int meos_temporal_num_instants(const Temporal *t) {
  return t ? temporal_num_instants(t) : 0;
}

EMSCRIPTEN_KEEPALIVE
const char *meos_temporal_interp_name(const Temporal *t) {
  return t ? temporal_interp(t) : "";
}

EMSCRIPTEN_KEEPALIVE
int meos_temporal_srid(const Temporal *t) {
  return t ? tspatial_srid(t) : 0;
}

/* Reads x,y at the given JS epoch ms via MEOS's temporal_value_at_timestamptz.
 * Returns 1 on success, 0 on miss/error. Fills out[0]=x, out[1]=y. */
EMSCRIPTEN_KEEPALIVE
int meos_tgeompoint_value_at(const Temporal *t, double epoch_ms, double *out) {
  if (!t || !out) return 0;
  TimestampTz ts = (TimestampTz)((epoch_ms - 946684800000.0) * 1000.0);
  GSERIALIZED *gs = NULL;
  bool ok = tgeo_value_at_timestamptz(t, ts, false, &gs);
  if (!ok || !gs) return 0;
  POINT4D p = {0};
  if (!gserialized_peek_first_point(gs, &p)) { free(gs); return 0; }
  out[0] = p.x;
  out[1] = p.y;
  free(gs);
  return 1;
}

/* A convenience batch query: evaluate N entities at a single timestamp.
 * Packs results into a Float64Array laid out as [x0,y0,x1,y1,...,xN-1,yN-1]
 * with NaN for misses. This is the hot path used by the animation loop. */
EMSCRIPTEN_KEEPALIVE
int meos_batch_value_at(const Temporal *const *handles, int n,
                        double epoch_ms, double *out) {
  if (!handles || !out || n <= 0) return 0;
  TimestampTz ts = (TimestampTz)((epoch_ms - 946684800000.0) * 1000.0);
  int hits = 0;
  for (int i = 0; i < n; i++) {
    const Temporal *t = handles[i];
    out[i*2]     = 0.0 / 0.0;  /* NaN */
    out[i*2 + 1] = 0.0 / 0.0;
    if (!t) continue;
    GSERIALIZED *gs = NULL;
    if (!tgeo_value_at_timestamptz(t, ts, false, &gs) || !gs) continue;
    POINT4D p = {0};
    if (gserialized_peek_first_point(gs, &p)) {
      out[i*2]     = p.x;
      out[i*2 + 1] = p.y;
      hits++;
    }
    free(gs);
  }
  return hits;
}

/* ────────────────────────── misc diagnostics ────────────────────────── */

EMSCRIPTEN_KEEPALIVE
char *meos_temporal_as_ewkt(const Temporal *t, int precision) {
  if (!t) return NULL;
  return tspatial_as_text(t, precision);
}

EMSCRIPTEN_KEEPALIVE
char *meos_temporal_as_mfjson(const Temporal *t, int precision) {
  if (!t) return NULL;
  return temporal_as_mfjson(t, true, 3, precision, NULL);
}

/* ────────────────────── moving regions (tgeometry) ─────────────────── */

EMSCRIPTEN_KEEPALIVE
Temporal *meos_tgeometry_create(const char *wkt) {
  meos_start();
  return tgeometry_in(wkt);
}

/* Returns a malloc'd GeoJSON string for the geometry at the given timestamp,
 * or NULL on failure. JS parses it with JSON.parse() and renders the ring. */
EMSCRIPTEN_KEEPALIVE
char *meos_tgeometry_value_at_geojson(const Temporal *t, double epoch_ms, int precision) {
  if (!t) return NULL;
  TimestampTz ts = (TimestampTz)((epoch_ms - 946684800000.0) * 1000.0);
  GSERIALIZED *gs = NULL;
  bool ok = tgeo_value_at_timestamptz(t, ts, false, &gs);
  if (!ok || !gs) return NULL;
  char *s = geo_as_geojson(gs, 0, precision, NULL);
  free(gs);
  return s;
}

/* ──────────────────────── MF-JSON input path ────────────────────────── */

EMSCRIPTEN_KEEPALIVE
Temporal *meos_tgeompoint_from_mfjson(const char *mfjson) {
  meos_start();
  return tgeompoint_from_mfjson(mfjson);
}

EMSCRIPTEN_KEEPALIVE
Temporal *meos_tgeometry_from_mfjson(const char *mfjson) {
  meos_start();
  return tgeometry_from_mfjson(mfjson);
}

/* ───────────────── universal format parsing + introspection ───────── */

/* Format autodetection for meos_parse_any. */
typedef enum {
  MEOS_FMT_UNKNOWN = 0,
  MEOS_FMT_MFJSON,
  MEOS_FMT_HEXWKB,
  MEOS_FMT_WKT,
} meos_format;

static meos_format meos_detect_format(const char *s) {
  if (!s) return MEOS_FMT_UNKNOWN;
  while (*s == ' ' || *s == '\n' || *s == '\t' || *s == '\r') s++;
  if (!*s) return MEOS_FMT_UNKNOWN;
  if (*s == '{' || *s == '[') {
    /* MF-JSON is a JSON object. WKT sequences also start with '[' but with
     * a letter after: [POINT / [POLYGON. Distinguish by scanning. */
    if (*s == '{') return MEOS_FMT_MFJSON;
    const char *p = s + 1;
    while (*p == ' ' || *p == '\n') p++;
    if (*p == '{' || *p == '"') return MEOS_FMT_MFJSON;
    return MEOS_FMT_WKT;
  }
  /* All-hex means HexWKB. Scan up to a reasonable limit to decide. */
  int hex = 1, seen = 0;
  for (const char *p = s; *p && seen < 64; p++, seen++) {
    char c = *p;
    if (!((c >= '0' && c <= '9') ||
          (c >= 'a' && c <= 'f') ||
          (c >= 'A' && c <= 'F'))) { hex = 0; break; }
  }
  if (hex && seen >= 16) return MEOS_FMT_HEXWKB;
  return MEOS_FMT_WKT;
}

EMSCRIPTEN_KEEPALIVE
const char *meos_detect_format_name(const char *s) {
  switch (meos_detect_format(s)) {
    case MEOS_FMT_MFJSON: return "mfjson";
    case MEOS_FMT_HEXWKB: return "hexwkb";
    case MEOS_FMT_WKT:    return "wkt";
    default:              return "unknown";
  }
}

/* Try to parse ANY temporal input in any supported format. Returns a
 * Temporal* handle or NULL. Tries moving-point first, then moving-geometry. */
EMSCRIPTEN_KEEPALIVE
Temporal *meos_parse_any(const char *input) {
  meos_start();
  if (!input) return NULL;
  Temporal *t = NULL;
  meos_format fmt = meos_detect_format(input);
  switch (fmt) {
    case MEOS_FMT_MFJSON:
      t = tgeompoint_from_mfjson(input);
      if (!t) t = tgeometry_from_mfjson(input);
      break;
    case MEOS_FMT_HEXWKB:
      t = temporal_from_hexwkb(input);
      break;
    case MEOS_FMT_WKT:
    default:
      t = tgeompoint_in(input);
      if (!t) t = tgeometry_in(input);
      break;
  }
  return t;
}

EMSCRIPTEN_KEEPALIVE
char *meos_temporal_as_hexwkb(const Temporal *t) {
  if (!t) return NULL;
  size_t out_size = 0;
  return temporal_as_hexwkb(t, 0, &out_size);
}

/* Introspection: returns a small malloc'd JSON blob describing the handle. */
EMSCRIPTEN_KEEPALIVE
char *meos_temporal_describe(const Temporal *t) {
  if (!t) return NULL;
  char *buf = (char *)malloc(256);
  if (!buf) return NULL;
  const char *interp = temporal_interp(t);
  int ninst = temporal_num_instants(t);
  int32_t srid = tspatial_srid(t);
  double startMs = (double)temporal_start_timestamptz(t) / 1000.0 + 946684800000.0;
  double endMs   = (double)temporal_end_timestamptz(t)   / 1000.0 + 946684800000.0;
  snprintf(buf, 256,
           "{\"interp\":\"%s\",\"instants\":%d,\"srid\":%d,\"startMs\":%.0f,\"endMs\":%.0f}",
           interp ? interp : "", ninst, srid, startMs, endMs);
  return buf;
}

/* ═══════════════════════════════════════════════════════════════════
 * Analytical operators — the real MEOS showcase surface.
 *
 * Everything below wraps one or more MEOS operators in a shape that's
 * trivial for JS to call: inputs as plain pointers/doubles, outputs as
 * doubles, JSON strings, or fixed-size buffers written through a ptr.
 * The wrappers never hold state — each call is independent.
 * ═══════════════════════════════════════════════════════════════════ */

/* Bounding box via MEOS — writes [xmin, ymin, xmax, ymax] into out[0..3].
 * Returns 1 on success, 0 if the Temporal has no spatial extent. */
EMSCRIPTEN_KEEPALIVE
int meos_tspatial_bbox(const Temporal *t, double *out) {
  if (!t || !out) return 0;
  STBox *box = tspatial_to_stbox(t);
  if (!box) return 0;
  double xmin = 0, ymin = 0, xmax = 0, ymax = 0;
  int ok = stbox_xmin(box, &xmin) && stbox_ymin(box, &ymin)
        && stbox_xmax(box, &xmax) && stbox_ymax(box, &ymax);
  free(box);
  if (!ok) return 0;
  out[0] = xmin; out[1] = ymin; out[2] = xmax; out[3] = ymax;
  return 1;
}

/* Returns the trajectory as a malloc'd GeoJSON LineString string. JS
 * parses it once per entity to feed its canvas draw path. Under the
 * hood: tpoint_trajectory → geo_as_geojson. */
EMSCRIPTEN_KEEPALIVE
char *meos_tpoint_trajectory_geojson(const Temporal *t, int precision) {
  if (!t) return NULL;
  GSERIALIZED *gs = tpoint_trajectory(t, false);
  if (!gs) return NULL;
  char *s = geo_as_geojson(gs, 0, precision, NULL);
  free(gs);
  return s;
}

/* Total length of a moving point trajectory, in the SRID's native units
 * (degrees for 4326, meters for projected CRSes). Pass through MEOS. */
EMSCRIPTEN_KEEPALIVE
double meos_tpoint_length(const Temporal *t) {
  return t ? tpoint_length(t) : 0.0;
}

/* Simplify via Douglas-Peucker, return a new Temporal* handle. The JS
 * side owns the new handle and must meos_temporal_destroy it. */
EMSCRIPTEN_KEEPALIVE
Temporal *meos_temporal_simplify_dp(const Temporal *t, double eps, int sync) {
  if (!t || eps <= 0) return NULL;
  return temporal_simplify_dp(t, eps, sync ? true : false);
}

/* Pairwise nearest-approach distance between two moving points. */
EMSCRIPTEN_KEEPALIVE
double meos_nad_tgeo_tgeo(const Temporal *a, const Temporal *b) {
  if (!a || !b) return -1.0;
  return nad_tgeo_tgeo(a, b);
}

/* Nearest-approach instant as epoch_ms. Returns 0 on failure. */
EMSCRIPTEN_KEEPALIVE
double meos_nai_tgeo_tgeo_ms(const Temporal *a, const Temporal *b) {
  if (!a || !b) return 0.0;
  TInstant *inst = nai_tgeo_tgeo(a, b);
  if (!inst) return 0.0;
  TimestampTz ts = temporal_start_timestamptz((Temporal *)inst);
  free(inst);
  return (double)ts / 1000.0 + 946684800000.0;
}

/* Temporal distance between two entities. Returns a new Temporal float*
 * handle. JS samples it with tfloat_value_at_timestamptz to draw a sparkline. */
EMSCRIPTEN_KEEPALIVE
Temporal *meos_tdistance_tgeo_tgeo(const Temporal *a, const Temporal *b) {
  if (!a || !b) return NULL;
  return tdistance_tgeo_tgeo(a, b);
}

/* Sample a tfloat at a given epoch ms. Returns NaN on failure. */
EMSCRIPTEN_KEEPALIVE
double meos_tfloat_value_at(const Temporal *t, double epoch_ms) {
  if (!t) return 0.0 / 0.0;
  TimestampTz ts = (TimestampTz)((epoch_ms - 946684800000.0) * 1000.0);
  double v = 0;
  return tfloat_value_at_timestamptz(t, ts, false, &v) ? v : 0.0 / 0.0;
}

/* Min / max of a tfloat — used for sparkline y-axis scaling. */
EMSCRIPTEN_KEEPALIVE
double meos_tfloat_min(const Temporal *t) { return t ? tfloat_min_value(t) : 0.0 / 0.0; }
EMSCRIPTEN_KEEPALIVE
double meos_tfloat_max(const Temporal *t) { return t ? tfloat_max_value(t) : 0.0 / 0.0; }

/* Required because we build with EXIT_RUNTIME=0 and still need a main. */
int main(void) { return 0; }
