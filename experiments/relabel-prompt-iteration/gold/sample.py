import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import json, hashlib, re, sys, os
D = os.path.dirname(os.path.abspath(__file__))
T = {
    'v1': ROOT + r'\relabel\hans-relabel-202611222146.tgz.trace.jsonl',
    'v2': ROOT + r'\export\hans-relabel-v2.trace.jsonl',
    'v3': ROOT + r'\export\hans-v3.trace.jsonl',
}
CLASSES = ['blocker', 'suggestion', 'question', 'nit', 'out_of_scope', 'optimization']

def load(p):
    out = {}
    with open(p, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get('batch'):
                continue
            c = r.get('context') or {}
            if c.get('role') != 'opener':
                continue
            out[c['thread_id']] = r
    return out

recs = {k: load(p) for k, p in T.items()}
pool = recs['v3']
mand = set(l.strip() for l in open(ROOT + r'\relabel\relabel-mandatory-threads.txt', encoding='utf-8') if l.strip())

def key(tid, salt='eval'):
    return hashlib.sha256((salt + tid).encode('utf-8')).hexdigest()

def lab(v, tid):
    r = recs[v].get(tid)
    if not r:
        return None
    return (r.get('labels') or {}).get('comment_type')

DEF = re.compile(r"\bfail(s|ed|ing|ure)?\b|\bNPE\b|NullPointer|\bwrong\b|\bincorrect(ly)?\b|\bleak(s|ing)?\b|not closed|\brace\b|race condition|doesn'?t work|does not work|\bbroken\b|missing (null )?(check|close)", re.I)

ids = sorted(pool, key=key)
nonmand = [t for t in ids if t not in mand]
sel = {}
def take(name, cands, n):
    k = 0
    for t in cands:
        if k >= n:
            break
        if t in sel:
            continue
        sel[t] = name
        k += 1
    return k

counts = {}
for c in CLASSES:
    counts['class:' + c] = take('class:' + c, [t for t in nonmand if any(lab(v, t) == c for v in T)], 40)
counts['defect'] = take('defect', [t for t in nonmand if DEF.search(pool[t].get('text') or '') or any(lab(v, t) == 'blocker' for v in T)], 40)
counts['uniform'] = take('uniform', nonmand, 60)
counts['mandatory'] = take('mandatory', [t for t in ids if t in mand], 20)

def split(t):
    h = int(hashlib.sha256(t.encode('utf-8')).hexdigest(), 16) % 10
    return 'holdout' if h >= 7 else ('dev' if h < 3 else 'train')

with open(os.path.join(D, 'strata.jsonl'), 'w', encoding='utf-8') as f:
    for t, s in sel.items():
        f.write(json.dumps({'id': t, 'stratum': s, 'split': split(t)}) + '\n')
order = sorted(sel, key=lambda t: key(t, 'shuffle'))
with open(os.path.join(D, 'texts_full.jsonl'), 'w', encoding='utf-8') as f:
    for t in order:
        f.write(json.dumps({'id': t, 'text': pool[t].get('text') or ''}, ensure_ascii=False) + '\n')
print('pool', len(pool), 'mand_in_pool', sum(1 for t in pool if t in mand), 'selected', len(sel))
print(json.dumps(counts))
