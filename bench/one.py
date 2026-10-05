import os, sys
os.chdir('/home/mverde/projects/flash-next-local/workspaces/context-vm/proxy')
sys.path.insert(0, '.')
sys.path.insert(0, 'bench')
import json
import ctxproxy.core as core
from recall_bench import build_store, TOK
db, conv_args, maxidx = sys.argv[1], json.loads(sys.argv[2]), int(sys.argv[3])
sets = sys.argv[4:]
cfg = core.Config()
for kv in sets:
    k, v = kv.split('=')
    setattr(cfg, k, v == 'true')
st = build_store(core, 'bench/data/%s.sqlite' % db)
tc = core.TokenCounter(cfg.chars_per_token, TOK)
m = core.Manager(cfg, st, tc, core.Journal(None))
conv = st.db.execute("SELECT conv FROM archive GROUP BY conv ORDER BY count(*) DESC LIMIT 1").fetchone()[0]
meta = []
o = m.recall(conv, conv_args, max_idx=maxidx, meta=meta)
print(o)
print('TOKENS', tc.count(o), [(x['rid'], x['idx']) for x in meta])
