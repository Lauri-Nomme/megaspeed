import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import json, os, re, collections, hashlib
D = os.path.dirname(os.path.abspath(__file__)); S = os.path.dirname(D)
src = open(os.path.join(S, 'score.py'), encoding='utf-8').read().splitlines()
ns = {'r': {}}
exec('def put(start, s):\n    for i, c in enumerate(s): r[start + i] = c', ns)
for l in src:
    if l.startswith('put('):
        exec(l, ns)
r = ns['r']
B = [json.loads(l) for l in open(os.path.join(S, 'v3-audit-B.jsonl'), encoding='utf-8')]
assert len(r) == len(B) == 302
# verify gold frozen
h = hashlib.sha256(open(os.path.join(D, 'gold.jsonl'), 'rb').read()).hexdigest()
assert open(os.path.join(D, 'gold.sha256')).read().split()[0] == h
out = []
for i, x in enumerate(B):
    v = r[i]
    if v == '3': out.append({'id': x['thread_id'], 'comment_type': x['new'], 'source': 'v3-audit-B', 'verdict': '3'})
    elif v == 'M': out.append({'id': x['thread_id'], 'comment_type': x['old'], 'source': 'v3-audit-B', 'verdict': 'M'})
with open(os.path.join(D, 'gold_audit.jsonl'), 'w', encoding='utf-8', newline='\n') as f:
    for o in out: f.write(json.dumps(o) + '\n')
print('gold_audit rows', len(out), dict(collections.Counter(o['comment_type'] for o in out)))
gold = {json.loads(l)['id']: json.loads(l) for l in open(os.path.join(D, 'gold.jsonl'), encoding='utf-8')}
ov = [o for o in out if o['id'] in gold]
print('overlap', len(ov))
agree = sum(gold[o['id']]['comment_type'] == o['comment_type'] for o in ov)
print('agree', agree)
pairs = collections.Counter((gold[o['id']]['comment_type'], o['comment_type'], gold[o['id']]['confidence']) for o in ov)
for k, v in sorted(pairs.items()): print(' gold=%s audit=%s conf=%s : %d' % (k + (v,)))
