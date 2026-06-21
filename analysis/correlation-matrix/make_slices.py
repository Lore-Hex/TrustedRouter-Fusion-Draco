import json, os, glob, math
CJ="/tmp/claude/chunkjobs3"
files=sorted(os.path.basename(f) for f in glob.glob(os.path.join(CJ,"*.json"))
             if not os.path.basename(f).startswith("_"))
# sanity: filenames are idx_ci.json
bad=[f for f in files if not f.replace(".json","").replace("_","").isdigit()]
assert not bad, bad[:5]
print("total chunk files:", len(files))
# interleave so each slice has a mix of idxs/members (gentler, more uniform) — round-robin
NSLICE=4
slices=[[] for _ in range(NSLICE)]
for i,f in enumerate(files):
    slices[i%NSLICE].append(f)
for i,s in enumerate(slices):
    json.dump(s, open(f"/tmp/claude/slice_{i}.json","w"))
    print(f"slice_{i}: {len(s)} chunks")
json.dump(files, open("/tmp/claude/all_chunks.json","w"))
print("wrote slices + all_chunks.json")
