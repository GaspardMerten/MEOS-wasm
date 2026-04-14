#!/usr/bin/env python3
"""End-to-end acceptance test for the meos-wasm showcase.

Drives headless Firefox through every surface of the demo and asserts that
MEOS-native computations actually produce sensible results.
"""
import os, sys, time, json

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

URL = os.environ.get('MEOS_URL', 'http://localhost:8765/')

opts = Options()
opts.binary_location = os.environ.get(
    'FIREFOX_BIN', '/snap/firefox/current/usr/lib/firefox/firefox')
opts.add_argument('-headless')
opts.set_preference('javascript.options.wasm_memory64', True)

driver = webdriver.Firefox(options=opts)
driver.set_page_load_timeout(60)
failures = []

def hud(elem_id):
    return driver.find_element(By.ID, elem_id).text.strip()

def js(src, *args):
    return driver.execute_script(src, *args)

def section(name):
    print(f'\n── {name} ─────────────────────────')

def assert_true(cond, msg):
    if cond:
        print(f'  ✓ {msg}')
    else:
        print(f'  ✗ {msg}')
        failures.append(msg)

try:
    section('boot')
    driver.get(URL)
    WebDriverWait(driver, 90).until(
        lambda d: 'hidden' in d.find_element(By.ID, 'curtain').get_attribute('class')
    )
    print(f'  ✓ wasm boot: curtain dismissed')

    meta = js('return { module: typeof window.__meosModule, facade: typeof window.MEOS, count: window.__meosFacadeCount }')
    assert_true(meta['module'] == 'object',  f"Module loaded ({meta['module']})")
    assert_true(meta['facade'] == 'object',  f"MEOS façade attached ({meta['facade']})")
    assert_true(meta['count'] >= 2000,       f'façade exposes {meta["count"]} funcs')

    section('default dataset')
    assert_true(int(hud('hud-traj')) == 38,         f'38 Brussels taxis parsed')
    parts = hud('hud-length').split('°')
    length = float(parts[0])
    assert_true(length > 1.0,                        f'fleet length via tpoint_length: {length:.3f}°')
    nad_text = hud('hud-nad')
    assert_true('↔' in nad_text,                     f'closest pair computed via nad_tgeo_tgeo: {nad_text}')
    assert_true('ms' in hud('hud-parse'),            f'parse budget present: {hud("hud-parse")}')

    section('dataset switching')
    expected = {
        'Brussels urban fleet' : 38,
        'North Sea vessels'    : 24,
        'Bird migration'       : 18,
        'Cycling peloton'      : 56,
        'Brussels event zones' : 12,
        'MF-JSON import'       : 8,
        'STIB live · today'    : 200,
    }
    for btn in driver.find_elements(By.CSS_SELECTOR, '.dataset'):
        name = btn.text.splitlines()[0] if btn.text else ''
        if name not in expected:
            continue
        btn.click()
        want = expected[name]
        try:
            WebDriverWait(driver, 60).until(lambda d: int(hud('hud-traj') or 0) == want)
            assert_true(True, f'dataset → {name} ({hud("hud-traj")} entities)')
        except Exception:
            assert_true(False, f'dataset → {name} stuck at {hud("hud-traj")} (expected {want})')

    section('format lab')
    def lab(text, expected_fmt):
        lin  = driver.find_element(By.ID, 'fmt-in')
        lin.clear(); lin.send_keys(text)
        driver.find_element(By.ID, 'fmt-parse').click()
        WebDriverWait(driver, 10).until(
            lambda d: d.find_element(By.ID, 'fmt-out').text.strip() != ''
        )
        out = driver.find_element(By.ID, 'fmt-out').text.lower()
        return out, 'detected ' + expected_fmt in out
    out, ok = lab('POINT(4.36 50.84)@2026-04-14 09:00:00', 'wkt')
    assert_true(ok, f'WKT point detected & parsed')
    out, ok = lab('[POINT(4.36 50.84)@2026-04-14 09:00:00, POINT(4.50 50.88)@2026-04-14 09:30:00]', 'wkt')
    assert_true(ok, f'WKT sequence detected & parsed')
    driver.find_element(By.ID, 'fmt-roundtrip').click()
    time.sleep(0.4)
    out = driver.find_element(By.ID, 'fmt-out').text.lower()
    assert_true('detected hexwkb' in out, 'HexWKB round-trip succeeded')
    out, ok = lab('{"type":"MovingPoint","coordinates":[[4.36,50.84],[4.5,50.88]],"datetimes":["2026-04-14T09:00:00+00","2026-04-14T09:30:00+00"],"lower_inc":true,"upper_inc":true,"interpolation":"Linear"}', 'mfjson')
    assert_true(ok, 'MF-JSON detected & parsed')

    section('function explorer')
    # Go back to Brussels so handles are in the registry.
    driver.find_element(By.XPATH, "//button[contains(@class,'dataset')][1]").click()
    WebDriverWait(driver, 30).until(lambda d: int(hud('hud-traj')) == 38)

    # Probe: count functions in the list, search, pick tpoint_length, execute
    n_options = js('return document.querySelectorAll("#fn-list .row").length')
    assert_true(n_options > 0, f'explorer list populated ({n_options} rows visible)')

    # Search → select → execute tpoint_length
    js("""
      const s = document.getElementById('fn-search');
      s.value = 'tpoint_length'; s.dispatchEvent(new Event('input'));
    """)
    time.sleep(0.2)
    js("""
      const row = document.querySelector('#fn-list .row[data-fn="tpoint_length"]');
      if (row) row.click();
    """)
    time.sleep(0.3)
    js("document.getElementById('fn-run').click()")
    time.sleep(0.3)
    result = driver.find_element(By.ID, 'fn-result').text
    assert_true('double' in result and any(c.isdigit() for c in result),
                f'tpoint_length returned a number: {result.splitlines()[-1][:80]}')

    # Chain: tpoint_speed (returns Temporal*) — stores a handle
    js("""
      const s = document.getElementById('fn-search');
      s.value = 'tpoint_speed'; s.dispatchEvent(new Event('input'));
    """)
    time.sleep(0.2)
    js("""
      const row = document.querySelector('#fn-list .row[data-fn="tpoint_speed"]');
      if (row) row.click();
    """)
    time.sleep(0.3)
    js("document.getElementById('fn-run').click()")
    time.sleep(0.3)
    result = driver.find_element(By.ID, 'fn-result').text
    assert_true('stored as' in result or 'interp' in result,
                f'tpoint_speed returned a Temporal* handle')

    section('simplify slider')
    slider = driver.find_element(By.ID, 'sp-eps')
    # Set via JS directly since range input dragging is unreliable
    js("""
      const s = document.getElementById('sp-eps');
      s.value = '0.01'; s.dispatchEvent(new Event('input'));
    """)
    time.sleep(0.3)
    eps_text = driver.find_element(By.ID, 'sp-eps-v').text
    assert_true(eps_text == '0.0100', f'simplify slider shows {eps_text}')
    # Reset
    js("""
      const s = document.getElementById('sp-eps');
      s.value = '0'; s.dispatchEvent(new Event('input'));
    """)

    section('facade probe')
    result = js("""
      const F = window.MEOS;
      const h = F.tgeompoint_in('[POINT(4.36 50.84)@2026-04-14 09:00:00, POINT(4.42 50.88)@2026-04-14 09:20:00, POINT(4.50 50.85)@2026-04-14 09:45:00]');
      if (!h) return { ok: false };
      const len = F.tpoint_length(h);
      const ninst = F.temporal_num_instants(h);
      const interp = F.temporal_interp(h);
      const ewkt = F.meos_temporal_as_ewkt ? F.meos_temporal_as_ewkt(h, 6) : null;
      // also try a distance calculation between a fresh pair
      const h2 = F.tgeompoint_in('[POINT(4.38 50.86)@2026-04-14 09:00:00, POINT(4.46 50.84)@2026-04-14 09:30:00]');
      const nad = h2 ? F.nad_tgeo_tgeo(h, h2) : -1;
      F.temporal_destroy_safe(h);
      F.temporal_destroy_safe(h2);
      return { ok: true, len, ninst, interp, ewkt_len: (ewkt || '').length, nad };
    """)
    assert_true(result['ok'], 'façade parse succeeded')
    assert_true(result['ninst'] == 3, f'façade num_instants = {result["ninst"]}')
    assert_true(result['interp'] == 'Linear', f'façade interp = {result["interp"]}')
    assert_true(result['len'] > 0, f'façade tpoint_length = {result["len"]:.4f}°')
    assert_true(result['nad'] >= 0, f'façade nad_tgeo_tgeo = {result["nad"]:.4f}°')

    section('error capture')
    errs = js('return (window.__meosErrors || []).map(String)')
    severe = [e for e in errs if e and 'draw' not in e.lower()]
    if severe:
        print(f'  ⚠ in-page errors:')
        for e in severe[:5]:
            print(f'     {e[:200]}')
    else:
        print(f'  ✓ no uncaught errors (of {len(errs)} captured, 0 severe)')

    section('results')
    if failures:
        print(f'  ✗ {len(failures)} failures')
        for f in failures:
            print(f'     {f}')
        sys.exit(2)
    print('  ✓ all checks passed')

finally:
    driver.quit()
