import sqlite3, os, sys
os.chdir('/home/mverde/projects/flash-next-local/workspaces/context-vm/proxy')
db, lo, hi, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
roles = sys.argv[5].split(',') if len(sys.argv) > 5 else ['assistant']
d = sqlite3.connect('bench/data/%s.sqlite' % db)
for rid, idx, rl, name, c, tok in d.execute("select rid,idx,role,name,content,tokens from archive where idx between ? and ? order by idx, role", (lo, hi)):
    if rl not in roles or not (c or '').strip():
        continue
    print('%s idx=%d %s tok=%d | %s' % (rid, idx, rl, tok, c.replace('\n', ' ')[:n]))
