#!/usr/bin/env python3
"""Produce an EXPORTED_FUNCTIONS manifest for emcc by intersecting MEOS's
public header declarations with the symbols actually defined in libmeos.a.

Usage: gen-exports.py <meos-include-dir> <libmeos.a> <llvm-nm> <out.json>
"""
import json, os, re, subprocess, sys

inc_dir, libmeos_a, llvm_nm, out = sys.argv[1:5]

# 1. Names declared in the public headers (authoritative for "API surface")
declared = set()
pat = re.compile(r'^extern\s+[^;]*?\b([a-z_][A-Za-z0-9_]*)\s*\(', re.M)
for fn in sorted(os.listdir(inc_dir)):
    if not fn.startswith('meos') or not fn.endswith('.h'):
        continue
    with open(os.path.join(inc_dir, fn)) as f:
        for m in pat.finditer(f.read()):
            declared.add(m.group(1))

# 2. Names actually defined as external globals inside libmeos.a
out_nm = subprocess.check_output([llvm_nm, '--defined-only', '--extern-only', libmeos_a],
                                 stderr=subprocess.DEVNULL).decode()
defined = set()
for line in out_nm.splitlines():
    parts = line.split()
    if len(parts) < 3:
        continue
    kind, name = parts[-2], parts[-1]
    if kind in ('T', 'W', 'D'):  # text, weak, data
        defined.add(name)

# 3. Intersection — declared AND defined. These are exportable.
exported = declared & defined

# 4. Glue helpers + malloc/free — always keep.
extras = {
    'malloc', 'free',
    'meos_start', 'meos_stop', 'meos_version_string',
    'meos_parse_tgeompoint', 'meos_parse_tgeogpoint',
    'meos_tgeompoint_create', 'meos_temporal_destroy',
    'meos_temporal_start_epoch_ms', 'meos_temporal_end_epoch_ms',
    'meos_temporal_num_instants', 'meos_temporal_interp_name',
    'meos_temporal_srid', 'meos_tgeompoint_value_at',
    'meos_batch_value_at',
    'meos_temporal_as_ewkt', 'meos_temporal_as_mfjson',
    'meos_tgeometry_create', 'meos_tgeometry_value_at_geojson',
    'meos_tgeompoint_from_mfjson', 'meos_tgeometry_from_mfjson',
    'meos_parse_any', 'meos_detect_format_name',
    'meos_temporal_as_hexwkb', 'meos_temporal_describe',
    'meos_tspatial_bbox', 'meos_tpoint_trajectory_geojson',
    'meos_tpoint_length', 'meos_temporal_simplify_dp',
    'meos_nad_tgeo_tgeo', 'meos_nai_tgeo_tgeo_ms',
    'meos_tdistance_tgeo_tgeo', 'meos_tfloat_value_at',
    'meos_tfloat_min', 'meos_tfloat_max',
}
exported.update(extras)

# Prefix every name with "_" for emcc.
result = sorted('_' + n for n in exported)
with open(out, 'w') as f:
    json.dump(result, f)

print(f'  declared: {len(declared)}  defined: {len(defined)}  exported: {len(exported)}')
