"""Independent CPU replay: actual pinned Tiel tokenizer/template and PolyStrata Service.
No model execution; MockEngine only. Synthetic padding calibrated to reported counts.
"""
from pathlib import Path
import sys, json, argparse
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--polystrata', required=True, type=Path, help='CPU-only PolyStrata source checkout')
parser.add_argument('--tokenizer', required=True, type=Path, help='Tiel pack tokenizer directory')
parser.add_argument('--out', required=True, type=Path)
a = parser.parse_args()
root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(a.polystrata), str(a.polystrata/'tools')]
from serve.frontend import ChatTemplate, openai_to_messages
from serve.server import Service, MockEngine
from strata_tokenizer import Tokenizer
from ctxproxy.core import Config, Journal, Store, TokenCounter
from ctxproxy.server import Proxy
from ctxproxy.upstream import Upstream
from ctxproxy.window import WindowExceeded
pack = a.tokenizer
tok = Tokenizer.from_dir(pack)
svc = Service(MockEngine(tok, 'ok'), tok, ChatTemplate(pack/'chat_template.jinja'))
proxy = Proxy(Config(window=32768), Upstream('http://unused'), Store(), Journal(None),
              TokenCounter(tokenizer_path=str(pack)))
template = proxy.tc.prompt_template
base = {'messages':[{'role':'user','content':'hello'}], 'tools':[{'type':'function','function':{
    'name':'terminal','description':'Run a command','parameters':{
        'type':'object','properties':{'command':{'type':'string'}}}}}],
    'reasoning_effort':'low', 'max_tokens':1767}

def engine_count(b):
    m,t,k = openai_to_messages(b)
    return len(svc.encode_prompt(m,t,k,store=False))

results = []
for target, window, mt in ((31223,32768,1767),(65848,65536,289),(66719,65536,289)):
    b = {**base, 'max_tokens':mt}
    history = [{'role':'user','content':'hello'}]
    if window == 65536:
        for i in range(40 if target == 66719 else 5):
            history += [{'role':'assistant','content':'','tool_calls':[{'id':str(i),'type':'function',
                         'function':{'name':'terminal','arguments':'{"command":"false"}'}}]},
                        {'role':'tool','tool_call_id':str(i),'content':'Exit code: 1, error: synthetic failure'}]
        history += [{'role':'user','content':'continue'}]
    b['messages'] = history
    n = target - engine_count(b)
    b['messages'] = history + [{'role':'user','content':'padding' + ' a'*max(0,n-10)}]
    # Adjust the last text by single-token words; this is calibration, not a user prompt.
    delta = target - engine_count(b)
    b['messages'][-1]['content'] += ' a'*delta
    actual = engine_count(b)
    assert actual == target, (actual,target)
    proxy.cfg.window = window
    proxy.tc.prompt_template = None
    old = proxy.wguard.count_body(b)
    old_send = proxy.wguard.enforce(b)
    m,t,k = openai_to_messages(old_send)
    try:
        svc.engine.max_context = window
        # 64K report does not specify its rejected request's max_tokens/fit setting.
        # Enable the engine safety net here to exercise its prompt-only rejection text.
        svc.fit_max_tokens = window == 65536
        svc.prepare(m,t,k,old_send['max_tokens'])
        before = 'accepted'
    except ValueError as e:
        before = str(e)
    proxy.tc.prompt_template = template
    new = proxy.wguard.count_body(b)
    assert new == actual, (new, actual)
    assert before != 'accepted', 'legacy renderer unexpectedly fitted'
    try:
        send = proxy.wguard.enforce(b)
        m,t,k = openai_to_messages(send)
        ids,_,limit = svc.prepare(m,t,k,send['max_tokens'])
        after = {'prompt':len(ids),'max_tokens':limit,'accepted':True}
    except WindowExceeded as e:
        after = {'refused_locally':True,'prompt':e.prompt}
    results.append({'synthetic_prompt':True,'reported_target':target,'window':window,
                    'requested_output':mt,'legacy_prompt':old,'engine_prompt':actual,
                    'new_guard_prompt':new,'delta':actual-old,'before':before,'after':after})
proxy.store.db.close()
print(json.dumps(results,indent=2))
a.out.parent.mkdir(parents=True, exist_ok=True)
a.out.write_text(json.dumps(results,indent=2)+'\n')
