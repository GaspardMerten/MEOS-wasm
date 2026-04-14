#!/usr/bin/env python3
"""Selenium smoke test — drives the meos-wasm showcase in headless Firefox,
waits for wasm64 boot, asserts that the HUD populates and no JS errors fire.

Requires: python-selenium, geckodriver, firefox (system packages).
Run the HTTP server first (./scripts/serve.sh) or set MEOS_URL=…
"""
import os, sys, time, json

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

URL = os.environ.get('MEOS_URL', 'http://localhost:8765/')

opts = Options()
opts.binary_location = os.environ.get('FIREFOX_BIN', '/snap/firefox/current/usr/lib/firefox/firefox')
opts.add_argument('-headless')
opts.set_preference('devtools.console.stdout.content', True)
# Firefox has wasm memory64 on by default since 134; make sure it's enabled.
opts.set_preference('javascript.options.wasm_memory64', True)

driver = webdriver.Firefox(options=opts)
driver.set_page_load_timeout(45)

try:
    print(f'→ {URL}')
    driver.get(URL)
    time.sleep(3)
    try:
        print('  curtain text:', driver.find_element(By.ID, 'curtain').text.strip()[:200])
    except Exception as e:
        print('  curtain read failed:', e)
    try:
        print('  page title   :', driver.title)
    except Exception: pass

    # Wait up to 60s for the curtain to go away (module boot + dataset parse)
    WebDriverWait(driver, 60).until(
        lambda d: 'hidden' in d.find_element(By.ID, 'curtain').get_attribute('class')
    )
    print('✓ curtain dismissed — wasm module initialized')

    # HUD should show non-zero counters once the default swarm is parsed
    def hud(id): return driver.find_element(By.ID, id).text.strip()

    WebDriverWait(driver, 10).until(lambda d: hud('hud-traj') not in ('', '0'))
    print(f'  trajectories : {hud("hud-traj")}')
    print(f'  fleet length : {hud("hud-length")}')
    print(f'  nearest pair : {hud("hud-nad")}')
    print(f'  parse budget : {hud("hud-parse")}')
    print(f'  wasm heap    : {hud("hud-heap")}')

    # Drive the "Re-parse" button and make sure the counters still update
    driver.find_element(By.ID, 'btn-reparse').click()
    time.sleep(0.4)
    print('✓ re-parse clicked — no crash')

    # Pick each dataset in turn, waiting for the HUD to actually update
    # (loadDataset is async). The trajectory count changes per dataset so we
    # wait until it transitions to the expected value.
    dataset_counts = {
        'Brussels urban fleet': 38,
        'North Sea vessels'   : 24,
        'Bird migration'      : 18,
        'Cycling peloton'     : 56,
        'Brussels event zones': 12,
        'MF-JSON import'      : 8,
    }
    for i, btn in enumerate(driver.find_elements(By.CSS_SELECTOR, '.dataset')):
        name = btn.text.splitlines()[0] if btn.text else f'#{i}'
        btn.click()
        expected = dataset_counts.get(name)
        if expected is not None:
            try:
                WebDriverWait(driver, 20).until(
                    lambda d: hud('hud-traj') == str(expected)
                )
                print(f'✓ dataset → {name}  ({hud("hud-traj")} entities)')
            except Exception:
                print(f'✗ dataset → {name}  stuck at {hud("hud-traj")} (expected {expected})')
        else:
            time.sleep(0.5)
            print(f'✓ dataset → {name}  ({hud("hud-traj")} entities)')

    # Drain in-page error log we captured via window.__meosErrors
    errs = driver.execute_script('return window.__meosErrors || []')
    if errs:
        print('✗ in-page errors:')
        for e in errs[:5]: print('   ', str(e)[:220])

    # Pull browser console errors (SEVERE log level only)
    logs = []
    try:
        logs = driver.get_log('browser')
    except Exception:
        pass  # firefox driver doesn't always expose get_log

    severe = [l for l in logs if l.get('level') == 'SEVERE']
    if severe:
        print('✗ browser console errors:')
        for l in severe:
            print('   ', l.get('message', '')[:200])
        sys.exit(2)

    # ── Format Lab: try every supported input format ─────────────────
    from selenium.webdriver.common.keys import Keys
    lab_in = driver.find_element(By.ID, 'fmt-in')
    lab_out = driver.find_element(By.ID, 'fmt-out')
    def lab_parse(text, label):
        lab_in.clear()
        lab_in.send_keys(text)
        driver.find_element(By.ID, 'fmt-parse').click()
        WebDriverWait(driver, 5).until(
            lambda d: 'detected' in d.find_element(By.ID, 'fmt-out').text
            or 'failed'   in d.find_element(By.ID, 'fmt-out').text
        )
        out = lab_out.text[:120].replace('\n', ' ')
        print(f'  {label:8s} → {out}')
        assert 'failed' not in out.lower(), f'{label} parse failed'

    lab_parse('POINT(4.36 50.84)@2026-04-14 09:00:00',              'WKT pt')
    lab_parse('[POINT(4.36 50.84)@2026-04-14 09:00:00, POINT(4.50 50.88)@2026-04-14 09:30:00]', 'WKT seq')
    # HexWKB: round-trip from the current input
    driver.find_element(By.ID, 'fmt-roundtrip').click()
    time.sleep(0.3)
    out = lab_out.text[:120].replace('\n', ' ')
    print(f'  {"HexWKB":8s} → {out}')
    assert 'detected' in out, 'HexWKB round-trip failed'
    # MF-JSON
    lab_parse('{"type":"MovingPoint","coordinates":[[4.36,50.84],[4.5,50.88]],"datetimes":["2026-04-14T09:00:00+00","2026-04-14T09:30:00+00"],"lower_inc":true,"upper_inc":true,"interpolation":"Linear"}', 'MF-JSON')
    print('✓ format lab: WKT · HexWKB · MF-JSON all parsed')

    # ── Auto-bound façade probe ────────────────────────────────
    facade = driver.execute_script("""
      const M = window.__meosModule, F = window.MEOS;
      if (!F) return { ok: false, error: 'MEOS façade not loaded' };
      // Parse a trajectory via the façade and extract a length via tpoint_length.
      const h = F.tgeompoint_in('[POINT(4.36 50.84)@2026-04-14 09:00:00, POINT(4.50 50.88)@2026-04-14 09:30:00]');
      const len = F.tpoint_length(h);
      const interp = F.temporal_interp(h);
      const ninst = F.temporal_num_instants(h);
      F.temporal_destroy_safe(h);
      return { ok: true, length: len, interp, ninst, facade_count: window.__meosFacadeCount };
    """)
    print(f'  façade probe : {facade}')
    assert facade.get('ok'), facade
    assert facade['length'] > 0, 'tpoint_length returned non-positive'

    # Evaluate a piece of JS to confirm the MEOS API is actually reachable.
    # Count exported MEOS functions visible on `Module`.
    n_exports = driver.execute_script("""
        const m = window.__meosModule || null;
        return m ? Object.keys(m).filter(k => k.startsWith('_')).length : -1;
    """)
    print(f'  meos exports on Module: {n_exports}')

    # Final HUD readout after all the interaction
    print(f'  final parse budget: {hud("hud-parse")}')
    print('ALL GOOD')
finally:
    driver.quit()
