import sqlite3, os, sys
os.chdir('/home/mverde/projects/flash-next-local/workspaces/context-vm/proxy')
db, path = sys.argv[1], sys.argv[2]
d = sqlite3.connect('bench/data/%s.sqlite' % db)
for r in d.execute("select idx,op,path,outcome,rid_args,rid_out,substr(head,1,90) from fileops where path like ? order by idx", ('%' + path,)):
    print(r)
