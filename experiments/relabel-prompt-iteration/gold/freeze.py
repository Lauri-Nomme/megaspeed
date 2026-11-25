import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import json, hashlib, os, collections
D = os.path.dirname(os.path.abspath(__file__))
rd = lambda n: [json.loads(l) for l in open(os.path.join(D, n), encoding='utf-8')]
p1 = {r['id']: r for r in rd('pass1.jsonl')}
p2 = {r['id']: r for r in rd('pass2.jsonl')}
st = {r['id']: r for r in rd('strata.jsonl')}
tx = {r['id']: r['text'] for r in rd('texts_full.jsonl')}
assert set(p1) == set(st) == set(tx), 'coverage'
F = ['comment_type', 'question', 'is_defect', 'thread_intent']
agree = collections.Counter(); allf = 0; nlowdis = 0
for i, r2 in p2.items():
    r1 = p1[i]; dis = False
    for f in F:
        if r1[f] == r2[f]:
            agree[f] += 1
        else:
            dis = True
    if all(r1[f] == r2[f] for f in F):
        allf += 1
    if r1['confidence'] == r2['confidence']:
        agree['confidence'] += 1
    if dis:
        r1['confidence'] = 'low'; r1['pass2_disagree'] = True; nlowdis += 1
print('pass2 n', len(p2), {f: '%d/%d' % (agree[f], len(p2)) for f in F + ['confidence']}, 'all4', allf, 'disagree_rows', nlowdis)
order = sorted(p1, key=lambda t: hashlib.sha256(('eval' + t).encode()).hexdigest())
with open(os.path.join(D, 'eval.jsonl'), 'w', encoding='utf-8') as f:
    for t in order:
        f.write(json.dumps({'id': t, 'text': tx[t], 'split': st[t]['split'], 'stratum': st[t]['stratum']}, ensure_ascii=False) + '\n')
with open(os.path.join(D, 'gold.jsonl'), 'w', encoding='utf-8', newline='\n') as f:
    for t in order:
        r = dict(p1[t]); r['second_pass'] = t in p2
        f.write(json.dumps(r) + '\n')
h = hashlib.sha256(open(os.path.join(D, 'gold.jsonl'), 'rb').read()).hexdigest()
open(os.path.join(D, 'gold.sha256'), 'w').write(h + '  gold.jsonl\n')
print('sha256', h)
# stats
c = collections.Counter
print('split', dict(c(st[t]['split'] for t in order)))
tab = collections.defaultdict(c)
for t in order:
    tab[st[t]['stratum']][st[t]['split']] += 1
for k in sorted(tab):
    print(' ', k, dict(tab[k]))
g = [p1[t] for t in order]
print('type', dict(c(r['comment_type'] for r in g)))
print('type high', dict(c(r['comment_type'] for r in g if r['confidence'] == 'high')))
print('question', dict(c(r['question'] for r in g)), 'defect', dict(c(r['is_defect'] for r in g)), 'intent', dict(c(r['thread_intent'] for r in g)))
print('conf', dict(c(r['confidence'] for r in g)))
print('nofit share', round(sum(r['comment_type'] == 'nofit' for r in g) / len(g), 3), 'low share', round(sum(r['confidence'] == 'low' for r in g) / len(g), 3))
ns = c(); nsl = c()
for t in order:
    s = st[t]['stratum']; ns[s] += p1[t]['comment_type'] == 'nofit'
    nsl[s] += 1
print('nofit by stratum', {k: '%d/%d' % (ns[k], nsl[k]) for k in sorted(nsl)})
print('mandatory types', dict(c(p1[t]['comment_type'] for t in order if st[t]['stratum'] == 'mandatory')),
      'mand defect', sum(p1[t]['is_defect'] for t in order if st[t]['stratum'] == 'mandatory'))
print('trainable (non-nofit & high) by split', dict(c(st[t]['split'] for t in order if p1[t]['comment_type'] != 'nofit' and p1[t]['confidence'] == 'high')))
