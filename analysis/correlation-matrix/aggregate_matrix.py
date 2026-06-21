#!/usr/bin/env python3
"""Aggregate gemini + Sonnet member-solo grades -> 100x5 DRACO score grid -> per-task
error-structure correlation matrix (the Post B diversity figure).

Reads:
  /tmp/claude/gradejobs/*.json     (idx -> member, task_id, gemini float score)
  /tmp/claude/gradejobs/_rubrics.json (task_id -> {criterion_id: weight})
  /tmp/claude/chunkjobs3/_counts.json (idx -> n chunks expected; completeness check)
  /tmp/claude/sonnet_results.jsonl (accumulated {idx,ci,m:[met ids],n} from grading workflows)

Writes:
  /tmp/claude/score_grid.json   (task_id -> {member: {score, src}})
  /tmp/claude/corr_matrix.json  (members, resid corr matrix, per-member mean off-diag, etc.)
"""
import json, os, glob, math, collections, sys

GJ="/tmp/claude/gradejobs"
CJ="/tmp/claude/chunkjobs3"
MEMBERS=["m3","kimi","deepseek","gemma4","glm"]

rubrics=json.load(open(os.path.join(GJ,"_rubrics.json")))
counts=json.load(open(os.path.join(CJ,"_counts.json")))  # idx(str)->n chunks
gj={}
for f in glob.glob(os.path.join(GJ,"*.json")):
    if os.path.basename(f).startswith("_"): continue
    g=json.load(open(f)); gj[g["idx"]]={"member":g["member"],"task_id":g["task_id"],"gemini":g.get("gemini")}

def score_from_met(task_id, met_ids):
    rb=rubrics[task_id]; pos=sum(w for w in rb.values() if w>0)
    if pos<=0: return None
    earned=sum(rb[c] for c in met_ids if c in rb)
    return max(0.0,min(100.0,100.0*earned/pos))

# --- accumulate Sonnet results ---
son_met=collections.defaultdict(set)     # idx -> union met ids
son_chunks=collections.defaultdict(set)  # idx -> set ci seen
spath="/tmp/claude/sonnet_results.jsonl"
n_son_rows=0
if os.path.exists(spath):
    for line in open(spath):
        line=line.strip()
        if not line: continue
        r=json.loads(line); n_son_rows+=1
        idx=r["idx"]; son_chunks[idx].add(r["ci"]); son_met[idx].update(r.get("m",[]))

# completeness: a Sonnet cell is "complete" iff #chunks seen == expected
son_complete={}
for idx,chunks in son_chunks.items():
    exp=counts.get(str(idx))
    son_complete[idx]= (exp is not None and len(chunks)>=exp)

# --- build score grid: task_id -> {member: (score, src)} ---
grid=collections.defaultdict(dict)
for idx,meta in gj.items():
    mem=meta["member"]; tid=meta["task_id"]; gem=meta["gemini"]
    if gem is not None:
        grid[tid][mem]=(float(gem),"gemini")
    elif idx in son_complete and son_complete[idx]:
        sc=score_from_met(tid, son_met[idx])
        if sc is not None: grid[tid][mem]=(sc,"sonnet")

tasks=sorted(grid.keys())
complete_tasks=[t for t in tasks if all(m in grid[t] for m in MEMBERS)]

def pearson(xs,ys):
    n=len(xs)
    if n<3: return None
    mx=sum(xs)/n; my=sum(ys)/n
    sxx=sum((x-mx)**2 for x in xs); syy=sum((y-my)**2 for y in ys)
    sxy=sum((x-mx)*(y-my) for x,y in zip(xs,ys))
    if sxx<=0 or syy<=0: return None
    return sxy/math.sqrt(sxx*syy)

# PRIMARY: raw per-task score correlation across complete tasks (low = diverse errors = fusion gain).
# Pearson is invariant to per-member location/scale, so mixing gemini+Sonnet graders is safe.
raw={m:[grid[t][m][0] for t in complete_tasks] for m in MEMBERS}
corr={i:{} for i in MEMBERS}
for i in MEMBERS:
    for j in MEMBERS:
        corr[i][j]=1.0 if i==j else pearson(raw[i],raw[j])
mean_offdiag={}
for i in MEMBERS:
    vs=[corr[i][j] for j in MEMBERS if j!=i and corr[i][j] is not None]
    mean_offdiag[i]=sum(vs)/len(vs) if vs else None

# SECONDARY diagnostic: leave-one-out residual correlation (error structure net of task difficulty;
# LOO mean excludes self to avoid the k-member sum-to-zero artifact).
lresid={m:[] for m in MEMBERS}
for t in complete_tasks:
    vals={m:grid[t][m][0] for m in MEMBERS}
    for m in MEMBERS:
        others=[vals[o] for o in MEMBERS if o!=m]
        lresid[m].append(vals[m]-sum(others)/len(others))
resid_corr={i:{} for i in MEMBERS}
for i in MEMBERS:
    for j in MEMBERS:
        resid_corr[i][j]=1.0 if i==j else pearson(lresid[i],lresid[j])
resid_offdiag={}
for i in MEMBERS:
    vs=[resid_corr[i][j] for j in MEMBERS if j!=i and resid_corr[i][j] is not None]
    resid_offdiag[i]=sum(vs)/len(vs) if vs else None

# per-member mean score (calibration check: gemini-graded vs sonnet-graded subsets)
cal={}
for m in MEMBERS:
    gsc=[grid[t][m][0] for t in tasks if m in grid[t] and grid[t][m][1]=="gemini"]
    ssc=[grid[t][m][0] for t in tasks if m in grid[t] and grid[t][m][1]=="sonnet"]
    cal[m]={"gemini_n":len(gsc),"gemini_mean":(sum(gsc)/len(gsc) if gsc else None),
            "sonnet_n":len(ssc),"sonnet_mean":(sum(ssc)/len(ssc) if ssc else None)}

out={"members":MEMBERS,"n_tasks_total":len(tasks),"n_complete_tasks":len(complete_tasks),
     "n_sonnet_rows":n_son_rows,"n_sonnet_cells":len(son_chunks),
     "n_sonnet_complete":sum(1 for v in son_complete.values() if v),
     "raw_corr":corr,"mean_offdiag_corr":mean_offdiag,
     "resid_corr_loo":resid_corr,"resid_offdiag_loo":resid_offdiag,
     "member_mean_score":{m:(sum(raw[m])/len(raw[m]) if raw[m] else None) for m in MEMBERS},
     "calibration":cal}
json.dump({t:{m:grid[t].get(m) for m in MEMBERS} for t in tasks}, open("/tmp/claude/score_grid.json","w"))
json.dump(out, open("/tmp/claude/corr_matrix.json","w"), indent=1)

print(f"tasks total={len(tasks)} complete(all5)={len(complete_tasks)} | sonnet rows={n_son_rows} cells={len(son_chunks)} complete={sum(1 for v in son_complete.values() if v)}")
print("\nper-member mean RAW score-corr with others (lower = more diverse / less redundant):")
for m in sorted(MEMBERS, key=lambda x:(mean_offdiag[x] is None, mean_offdiag[x] if mean_offdiag[x] is not None else 9)):
    v=mean_offdiag[m]; ms=out["member_mean_score"][m]
    print((f"  {m:9s} corr={v:+.3f}  meanscore={ms:.1f}" if v is not None and ms is not None else f"  {m:9s}  n/a"))
print("\n5x5 RAW score correlation:")
print("           "+" ".join(f"{m:>8s}" for m in MEMBERS))
for i in MEMBERS:
    print(f"  {i:9s}"+" ".join((f"{corr[i][j]:+.2f}".rjust(9) if corr[i][j] is not None else '     n/a') for j in MEMBERS))
print("\n(secondary) LOO-residual error correlation, per-member mean:")
for m in sorted(MEMBERS, key=lambda x:(resid_offdiag[x] is None, resid_offdiag[x] if resid_offdiag[x] is not None else 9)):
    v=resid_offdiag[m]; print(f"  {m:9s} {v:+.3f}" if v is not None else f"  {m:9s}  n/a")
print("\ncalibration (per-member mean score, gemini subset vs sonnet subset):")
for m in MEMBERS:
    c=cal[m]; gm=f"{c['gemini_mean']:.1f}" if c['gemini_mean'] is not None else "--"
    sm=f"{c['sonnet_mean']:.1f}" if c['sonnet_mean'] is not None else "--"
    print(f"  {m:9s} gemini n={c['gemini_n']:3d} mean={gm:>5s}   sonnet n={c['sonnet_n']:3d} mean={sm:>5s}")
