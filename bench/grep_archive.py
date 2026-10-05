import sqlite3, os, sys, re
os.chdir('/home/mverde/projects/flash-next-local/workspaces/context-vm/proxy')
# usage: grep.py db regex [maxidx] [role] [ctx]
db, rx = sys.argv[1], sys.argv[2]
mx = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] != '-' else 10**9
role = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != '-' else None
ctx = int(sys.argv[5]) if len(sys.argv) > 5 else 80
d = sqlite3.connect('bench/data/%s.sqlite' % db)
R = re.compile(rx, re.I | re.S)
n = 0
for rid, idx, rl, name, c, tok in d.execute("select rid,idx,role,name,content,tokens from archive where idx<? order by idx", (mx,)):
    if role and rl != role:
        continue
    ms = list(R.finditer(c or ''))
    if not ms:
        continue
    n += 1
    m = ms[0]
    s = (c[max(0, m.start()-ctx):m.end()+ctx]).replace('\n', ' ')
    print('%s idx=%d %s %s tok=%d hits=%d | %s' % (rid, idx, rl, (name or '')[:20], tok, len(ms), s))
print('total', n)
