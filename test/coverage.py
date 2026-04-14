#!/usr/bin/env python3
"""End-to-end coverage test — calls every MEOS function in the façade
against the synthetic sample library and reports pass/fail per function.

Usage:
  FIREFOX_BIN=/snap/firefox/current/usr/lib/firefox/firefox \\
    ./test/coverage.py [--limit 500] [--batch 500]
"""
import argparse, os, sys, json, time
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url',    default=os.environ.get('MEOS_URL', 'http://localhost:8765/'))
    ap.add_argument('--limit',  type=int, default=0,   help='max functions to run (0=all)')
    ap.add_argument('--start',  type=int, default=0,   help='starting function index (for bisection)')
    ap.add_argument('--batch',  type=int, default=500, help='batch size per JS call')
    ap.add_argument('--json',   default='',            help='write raw report to file')
    ap.add_argument('--fail-threshold', type=float, default=0.5,
                    help='fail the run if productive/callable ratio is below this')
    args = ap.parse_args()

    opts = Options()
    opts.binary_location = os.environ.get(
        'FIREFOX_BIN', '/snap/firefox/current/usr/lib/firefox/firefox')
    opts.add_argument('-headless')

    driver = webdriver.Firefox(options=opts)
    driver.set_page_load_timeout(60)
    try:
        print(f'→ {args.url}')
        driver.get(args.url)
        WebDriverWait(driver, 90).until(
            lambda d: 'hidden' in d.find_element(By.ID, 'curtain').get_attribute('class')
        )
        # execute_async_script: the script must call arguments[-1] as callback
        driver.set_script_timeout(60)
        total_fns = driver.execute_async_script('''
          const cb = arguments[arguments.length - 1];
          fetch("./meos-api.json").then(r => r.json()).then(j => {
            window.__meosManifest = j;
            cb(Object.keys(j).length);
          });
        ''')
        print(f'  manifest: {total_fns} functions')
        limit = args.limit or total_fns
        cursor = args.start

        # Aggregate across batches — Selenium/Firefox can get unhappy
        # returning 50 KB+ JSON in a single execute_script call.
        agg = dict(total=0, ok=0, threw=0, nullPtr=0, skipped=0,
                   categories={}, failures=[])
        start = args.start
        t0 = time.time()
        while start < limit:
            n = min(args.batch, limit - start)
            batch = driver.execute_async_script('''
              const cb = arguments[arguments.length - 1];
              window.__runCoverage({ start: arguments[0], limit: arguments[1] }).then(cb).catch(e => cb({ error: String(e) }));
            ''', start, n)
            agg['total']    += batch['total']
            agg['ok']       += batch['ok']
            agg['threw']    += batch['threw']
            agg['nullPtr']  += batch['nullPtr']
            agg['skipped']  += batch['skipped']
            for cat, c in batch['categories'].items():
                a = agg['categories'].setdefault(cat, {'ok': 0, 'threw': 0, 'empty': 0})
                for k in ('ok', 'threw', 'empty'):
                    a[k] += c[k]
            agg['failures'].extend(batch['failures'])
            start += n
            pct = 100.0 * start / limit
            print(f'  batch {start:>5}/{limit} · ok={agg["ok"]} threw={agg["threw"]} skipped={agg["skipped"]}'
                  f' · {pct:5.1f}%')

        dt = time.time() - t0

        callable_fns = agg['ok'] + agg['threw']
        productive   = sum(c['ok']   for c in agg['categories'].values())
        empty        = sum(c['empty'] for c in agg['categories'].values())
        threw        = sum(c['threw'] for c in agg['categories'].values())
        ratio        = productive / callable_fns if callable_fns else 0.0

        print('\n── summary ─────────────────────────')
        print(f'  total       {agg["total"]:>5}')
        print(f'  skipped     {agg["skipped"]:>5}  (non-callable / missing samples)')
        print(f'  callable    {callable_fns:>5}  ({callable_fns/agg["total"]*100:.1f}% of total)')
        print(f'    productive  {productive:>5}  (returned non-null / non-NaN / non-empty)')
        print(f'    empty       {empty:>5}  (returned 0 / NaN / "")')
        print(f'    threw       {threw:>5}')
        print(f'  productive/callable ratio: {ratio*100:.1f}%')
        print(f'  elapsed     {dt:.1f}s')

        top = sorted(agg['categories'].items(),
                     key=lambda kv: -(kv[1]['ok'] + kv[1]['empty'] + kv[1]['threw']))
        print('\n── top 15 categories ───────────────')
        for cat, c in top[:15]:
            total = c['ok'] + c['empty'] + c['threw']
            print(f'  {cat:20s}  ok {c["ok"]:>3}  empty {c["empty"]:>3}  threw {c["threw"]:>3}  / {total}')

        if agg['failures']:
            print(f'\n── sample failures ({len(agg["failures"])} shown) ───')
            for f in agg['failures'][:25]:
                if 'err' in f:
                    print(f'  ✗ {f["fn"]:32s} threw · {f["err"][:80]}')
                else:
                    print(f'  · {f["fn"]:32s} {f["why"]}  ret={f.get("ret", "")}')

        if args.json:
            with open(args.json, 'w') as fh:
                json.dump(agg, fh, indent=2)
            print(f'\n  wrote {args.json}')

        if ratio < args.fail_threshold:
            print(f'\n  ✗ productive ratio {ratio*100:.1f}% below threshold {args.fail_threshold*100:.0f}%')
            sys.exit(2)
        print('\n  ✓ coverage acceptable')
    finally:
        driver.quit()

if __name__ == '__main__':
    main()
