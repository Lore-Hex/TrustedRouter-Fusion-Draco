labels_full=["MiniMax-M3","Kimi K2.6","DeepSeek V4","Gemma-4","GLM-5.2"]
labels_short=["M3","Kimi","DeepSeek","Gemma","GLM"]
M=[[1.00,0.56,0.47,0.56,0.65],
   [0.56,1.00,0.71,0.49,0.48],
   [0.47,0.71,1.00,0.51,0.51],
   [0.56,0.49,0.51,1.00,0.66],
   [0.65,0.48,0.51,0.66,1.00]]
mean_off=[0.559,0.561,0.550,0.556,0.576]

def lerp(a,b,t): return a+(b-a)*t
def fill(c):
    # off-diagonal sequential: 0.45 (light) -> 0.72 (brand dark)
    t=max(0.0,min(1.0,(c-0.45)/(0.72-0.45)))
    c0=(233,243,238); c1=(15,110,86)
    r=int(lerp(c0[0],c1[0],t)); g=int(lerp(c0[1],c1[1],t)); b=int(lerp(c0[2],c1[2],t))
    return f"#{r:02x}{g:02x}{b:02x}", ("#ffffff" if t>0.55 else "#0f3d30")

X0=312; Y0=196; CELL=86
W=1180; H=748
s=[]
s.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="100%" style="height:auto" font-family="Inter,Arial,sans-serif">')
s.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
s.append('<text x="60" y="58" font-size="37" font-weight="700" fill="#111827">The panel has no diversity hero</text>')
s.append('<text x="60" y="90" font-size="18" fill="#6b7280">Per-task DRACO score correlation between open-committee members · 100 tasks · gemini + Sonnet chunk-of-3 grades</text>')
# column headers
for j,sh in enumerate(labels_short):
    cx=X0+j*CELL+CELL/2
    s.append(f'<text x="{cx:.0f}" y="184" font-size="16" font-weight="600" text-anchor="middle" fill="#374151">{sh}</text>')
# rows
for i in range(5):
    cy=Y0+i*CELL
    s.append(f'<text x="{X0-16}" y="{cy+CELL/2+6:.0f}" font-size="17" font-weight="600" text-anchor="end" fill="#111827">{labels_full[i]}</text>')
    for j in range(5):
        x=X0+j*CELL; y=cy
        if i==j:
            s.append(f'<rect x="{x}" y="{y}" width="{CELL}" height="{CELL}" rx="3" fill="#f1f3f2" stroke="#ffffff" stroke-width="3"/>')
            s.append(f'<text x="{x+CELL/2:.0f}" y="{y+CELL/2+6:.0f}" font-size="17" text-anchor="middle" fill="#c2c8c5">—</text>')
        else:
            fc,tc=fill(M[i][j])
            s.append(f'<rect x="{x}" y="{y}" width="{CELL}" height="{CELL}" rx="3" fill="{fc}" stroke="#ffffff" stroke-width="3"/>')
            s.append(f'<text x="{x+CELL/2:.0f}" y="{y+CELL/2+6:.0f}" font-size="20" font-weight="600" text-anchor="middle" fill="{tc}">{M[i][j]:.2f}</text>')
# per-row average column
avx=X0+5*CELL+34
s.append(f'<text x="{avx+34:.0f}" y="184" font-size="15" font-weight="600" text-anchor="middle" fill="#6b7280">avg</text>')
for i in range(5):
    cy=Y0+i*CELL
    s.append(f'<text x="{avx+34:.0f}" y="{cy+CELL/2+6:.0f}" font-size="19" font-weight="700" text-anchor="middle" fill="#0f6e56">{mean_off[i]:.3f}</text>')
# bracket + label for the avg band
s.append(f'<line x1="{avx-6}" y1="{Y0+4}" x2="{avx-6}" y2="{Y0+5*CELL-4}" stroke="#d7dbd9" stroke-width="2"/>')
# footnote / takeaway
yb=Y0+5*CELL+52
s.append(f'<line x1="60" y1="{yb-26}" x2="1120" y2="{yb-26}" stroke="#eceae4"/>')
s.append(f'<text x="60" y="{yb}" font-size="19" fill="#111827"><tspan font-weight="700" fill="#0f6e56">Every pair sits at 0.47–0.71 (mean 0.56).</tspan> Each model\'s average correlation with the rest spans just <tspan font-weight="700">0.55–0.58</tspan> —</text>')
s.append(f'<text x="60" y="{yb+28}" font-size="19" fill="#374151">a 0.03 spread, inside the noise. No model is the orthogonal outlier; the diversity that fuels fusion is real but <tspan font-style="italic">diffuse</tspan>.</text>')
s.append(f'<text x="1120" y="{yb+62}" text-anchor="end" font-size="20" font-weight="700" fill="#0f6e56">TrustedRouter.com</text>')
s.append('</svg>')
open("/tmp/claude/matrix_chart.svg","w").write("\n".join(s))
print("wrote /tmp/claude/matrix_chart.svg", len("\n".join(s)),"bytes")
