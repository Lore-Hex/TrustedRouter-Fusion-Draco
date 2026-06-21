#!/usr/bin/env python3
"""Extract graded rows from workflow output file(s) into /tmp/claude/sonnet_results.jsonl
(idempotent, deduped). Output file is {summary,agentCount,logs,result}; result may be a
(possibly double-encoded) JSON string. Falls back to regex if structure differs.
Usage: parse_wf.py <output-file> [<output-file> ...]"""
import sys, re, json, os

MASTER="/tmp/claude/sonnet_results.jsonl"
ROW=re.compile(r'\{\\*"idx\\*":\s*(\d+),\s*\\*"ci\\*":\s*(\d+),\s*\\*"m\\*":\s*(\[[^\]]*\]),\s*\\*"n\\*":\s*(\d+)\}')

rows={}
if os.path.exists(MASTER):
    for line in open(MASTER):
        line=line.strip()
        if not line: continue
        r=json.loads(line); rows[(r["idx"],r["ci"])]=r

def extract(path):
    txt=open(path, errors="ignore").read()
    # try structured parse
    try:
        d=json.loads(txt)
        res=d.get("result")
        while isinstance(res,str):
            res=json.loads(res)
        if isinstance(res,list):
            return [(int(o["idx"]),int(o["ci"]),o.get("m",[]),int(o.get("n",0)))
                    for o in res if isinstance(o,dict) and "idx" in o and "ci" in o]
    except Exception:
        pass
    # regex fallback (handles escaped quotes)
    out=[]
    for mo in ROW.finditer(txt):
        try: m=json.loads(mo.group(3).replace('\\"','"'))
        except Exception: continue
        out.append((int(mo.group(1)),int(mo.group(2)),m,int(mo.group(4))))
    return out

added=0
for path in sys.argv[1:]:
    if not os.path.exists(path):
        print("MISSING", path); continue
    for idx,ci,m,n in extract(path):
        key=(idx,ci)
        if key not in rows:
            rows[key]={"idx":idx,"ci":ci,"m":m,"n":n}; added+=1
        elif rows[key]["n"]!=3 and n==3:
            rows[key]={"idx":idx,"ci":ci,"m":m,"n":n}

with open(MASTER,"w") as f:
    for k in sorted(rows): f.write(json.dumps(rows[k])+"\n")
print(f"parsed: +{added} new, total {len(rows)} rows")
counts=json.load(open("/tmp/claude/chunkjobs3/_counts.json"))
seen={}
for (idx,ci) in rows: seen.setdefault(idx,set()).add(ci)
complete=sum(1 for i,c in seen.items() if str(i) in counts and len(c)>=counts[str(i)])
miss=[i for i in (int(k) for k in counts) if i not in seen or len(seen[i])<counts[str(i)]]
print(f"cells with any chunk: {len(seen)} | complete cells: {complete}/{len(counts)} | incomplete cells: {len(miss)}")
