import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
# usage: python -I rec.py <src> <outfile> "<idx> <T><q><d><i><c>;..."  T in BSQNOPX, q/d in tf, i in uarw, c in hl
import json, sys, os
D = os.path.dirname(os.path.abspath(__file__))
rows = [json.loads(l) for l in open(os.path.join(D, sys.argv[1]), encoding='utf-8')]
out = os.path.join(D, sys.argv[2])
T = dict(B='blocker', S='suggestion', Q='question', N='nit', O='out_of_scope', P='optimization', X='nofit')
I = dict(u='unclear', a='acted', r='rejected', w='withdrawn')
done = set()
if os.path.exists(out):
    done = {json.loads(l)['id'] for l in open(out, encoding='utf-8')}
n = 0
with open(out, 'a', encoding='utf-8') as f:
    for item in sys.argv[3].split(';'):
        item = item.strip()
        if not item:
            continue
        idx, code = item.split()
        assert len(code) == 5, item
        t, q, d, i, c = code
        rid = rows[int(idx)]['id']
        assert rid not in done, ('dup', idx)
        f.write(json.dumps({'id': rid, 'comment_type': T[t], 'question': q == 't', 'is_defect': d == 't',
                            'thread_intent': I[i], 'confidence': 'high' if c == 'h' else 'low'}) + '\n')
        done.add(rid); n += 1
print('wrote', n, 'total', len(done))
