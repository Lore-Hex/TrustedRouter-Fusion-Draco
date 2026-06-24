import json, math, itertools
MEMBERS=["m3","kimi","deepseek","gemma4","glm"]
grid=json.load(open("/tmp/claude/score_grid.json"))  # task -> {member: [score,src] or null}

def pear(xs,ys):
    n=len(xs)
    if n<5: return None,n
    mx=sum(xs)/n; my=sum(ys)/n
    sxx=sum((x-mx)**2 for x in xs); syy=sum((y-my)**2 for y in ys)
    sxy=sum((x-mx)*(y-my) for x,y in zip(xs,ys))
    if sxx<=0 or syy<=0: return None,n
    return sxy/math.sqrt(sxx*syy),n

def corr_subset(i,j,pred):
    xs=[];ys=[]
    for t,row in grid.items():
        a=row.get(i); b=row.get(j)
        if not a or not b: continue
        if pred(a[1],b[1]):
            xs.append(a[0]); ys.append(b[0])
    return pear(xs,ys)

allp=lambda si,sj: True
samegem=lambda si,sj: si=="gemini" and sj=="gemini"
sameson=lambda si,sj: si=="sonnet" and sj=="sonnet"
same=lambda si,sj: si==sj

print(f"{'pair':22s} {'all(n)':>10s} {'sameGRADER(n)':>15s}  geminiPair(n)  sonnetPair(n)")
mean_all=[]; mean_same=[]
for i,j in itertools.combinations(MEMBERS,2):
    ca,na=corr_subset(i,j,allp)
    cs,ns=corr_subset(i,j,same)
    cg,ng=corr_subset(i,j,samegem)
    co,no=corr_subset(i,j,sameson)
    if ca is not None: mean_all.append(ca)
    if cs is not None: mean_same.append(cs)
    def f(c,n): return f"{c:+.2f}({n})" if c is not None else f"  -- ({n})"
    print(f"{i+'-'+j:22s} {f(ca,na):>10s} {f(cs,ns):>15s}  {f(cg,ng):>12s} {f(co,no):>12s}")
print()
print(f"mean pairwise corr: all-tasks={sum(mean_all)/len(mean_all):+.3f}  same-grader-only={sum(mean_same)/len(mean_same):+.3f}")
print("(if same-grader >> all, grader-mixing is attenuating the real structure)")
