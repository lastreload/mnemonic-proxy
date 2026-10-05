"""Ricerca a lotti per etichettare il banco: spec = righe 'id|db|regex|maxidx|role|n'."""
import sqlite3, os, sys, re
os.chdir('/home/mverde/projects/flash-next-local/workspaces/context-vm/proxy')
dbs = {}
for line in open(sys.argv[1], encoding='utf-8'):
    line = line.rstrip('\n')
    if not line.strip() or line.startswith('#'):
        continue
    qid, db, rx, mx, role, n = (line.split('§') + ['-', '-', '-'])[:6]
    mx = int(mx) if mx not in ('-', '') else 10**9
    n = int(n) if n not in ('-', '') else 6
    d = dbs.setdefault(db, sqlite3.connect('bench/data/%s.sqlite' % db))
    R = re.compile(rx, re.I | re.S)
    print('##', qid, db, rx)
    k = 0
    for rid, idx, rl, name, c, tok in d.execute("select rid,idx,role,name,content,tokens from archive where idx<? order by idx", (mx,)):
        if role not in ('-', '') and rl != role:
            continue
        m = R.search(c or '')
        if not m:
            continue
        k += 1
        if k <= n:
            s = (c[max(0, m.start()-90):m.end()+90]).replace('\n', ' ')
            print('  %s idx=%d %s tok=%d | %s' % (rid, idx, rl, tok, s))
    print('  total', k)
