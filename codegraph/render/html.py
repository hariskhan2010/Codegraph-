"""Render ``graph.html`` — a single self-contained interactive view.

No external dependencies: the graph data is inlined and a small vanilla-JS
force simulation draws it on a canvas. Click a node to see its edges; type to
filter; nodes are coloured by community.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import HTML_NAME
from ..db import Db
from .graph_json import build_graph_json

_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>codegraph — {name}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; font:13px/1.4 ui-sans-serif,system-ui,sans-serif;
         background:#0f1115; color:#e6e6e6; overflow:hidden; }}
  #bar {{ position:fixed; top:0; left:0; right:0; padding:8px 12px;
          background:#171a21; border-bottom:1px solid #2a2f3a; z-index:10;
          display:flex; gap:12px; align-items:center; }}
  #bar input {{ background:#0f1115; border:1px solid #2a2f3a; color:#e6e6e6;
                padding:4px 8px; border-radius:4px; width:240px; }}
  #bar .stat {{ color:#8b93a7; }}
  canvas {{ display:block; }}
  #info {{ position:fixed; right:0; top:38px; bottom:0; width:340px;
           background:#171a21; border-left:1px solid #2a2f3a; padding:12px;
           overflow:auto; font-size:12px; }}
  #info h3 {{ margin:0 0 4px; font-size:13px; }}
  #info .meta {{ color:#8b93a7; margin-bottom:10px; }}
  #info .edge {{ padding:2px 0; border-bottom:1px solid #23272f; }}
  .tag {{ font-size:10px; padding:1px 5px; border-radius:3px; background:#23324a; }}
</style></head><body>
<div id="bar">
  <strong>codegraph</strong>
  <input id="q" placeholder="filter nodes…">
  <span class="stat">{n} nodes · {e} edges · {c} communities</span>
</div>
<canvas id="c"></canvas>
<div id="info">Click a node.</div>
<script>
const DATA = {data};
const cv = document.getElementById('c'), ctx = cv.getContext('2d');
const info = document.getElementById('info'), q = document.getElementById('q');
let W, H; function resize(){{ W=cv.width=innerWidth; H=cv.height=innerHeight; }}
addEventListener('resize', resize); resize();

const idx = new Map(DATA.nodes.map((d,i)=>[d.id,i]));
const N = DATA.nodes.map((d,i)=>({{id:d.id,label:d.label,file:d.source_file,
  loc:d.source_location,kind:d.kind,comm:d.community??-1,rat:d.rationale||'',
  x:Math.cos(i)*300+W/2+(Math.random()-.5)*40, y:Math.sin(i)*300+H/2+(Math.random()-.5)*40,
  vx:0, vy:0}}));
const L = DATA.links.filter(l=>idx.has(l.source)&&idx.has(l.target))
  .map(l=>({{s:idx.get(l.source),t:idx.get(l.target),rel:l.relation,conf:l.confidence}}));
const deg = N.map(()=>0); L.forEach(l=>{{deg[l.s]++;deg[l.t]++;}});
const palette = ['#5b9dd9','#d98b5b','#7bd95b','#d95b9d','#d9cf5b','#5bd9cf','#9d5bd9','#d95b5b'];
const col = c => c<0 ? '#555' : palette[c % palette.length];

let sel=-1, filter='';
q.addEventListener('input', ()=>{{ filter=q.value.toLowerCase(); }});

function tick(){{
  for(let i=0;i<N.length;i++){{
    for(let j=i+1;j<N.length;j++){{
      let dx=N[j].x-N[i].x, dy=N[j].y-N[i].y, d2=dx*dx+dy*dy||1;
      if(d2<90000){{ let f=1200/d2; let d=Math.sqrt(d2);
        N[i].vx-=f*dx/d; N[i].vy-=f*dy/d; N[j].vx+=f*dx/d; N[j].vy+=f*dy/d; }}
    }}
  }}
  for(const l of L){{
    let a=N[l.s], b=N[l.t], dx=b.x-a.x, dy=b.y-a.y, d=Math.sqrt(dx*dx+dy*dy)||1;
    let f=(d-80)*0.01;
    a.vx+=f*dx/d; a.vy+=f*dy/d; b.vx-=f*dx/d; b.vy-=f*dy/d;
  }}
  for(const n of N){{
    n.vx+=(W/2-n.x)*0.0008; n.vy+=(H/2-n.y)*0.0008;
    n.x+=n.vx*=0.85; n.y+=n.vy*=0.85;
  }}
}}
function draw(){{
  ctx.clearRect(0,0,W,H);
  ctx.strokeStyle='rgba(255,255,255,0.06)'; ctx.lineWidth=1;
  for(const l of L){{
    ctx.beginPath(); ctx.moveTo(N[l.s].x,N[l.s].y); ctx.lineTo(N[l.t].x,N[l.t].y); ctx.stroke();
  }}
  for(let i=0;i<N.length;i++){{
    const n=N[i], r=3+Math.min(8,deg[i]*0.5);
    const dim = filter && !n.label.toLowerCase().includes(filter) && !(n.file||'').toLowerCase().includes(filter);
    ctx.globalAlpha = dim ? 0.12 : 1;
    ctx.fillStyle = i===sel ? '#fff' : col(n.comm);
    ctx.beginPath(); ctx.arc(n.x,n.y,r,0,7); ctx.fill();
    if((deg[i]>6 || i===sel) && !dim){{
      ctx.fillStyle='#cbd2e0'; ctx.font='10px sans-serif';
      ctx.fillText(n.label, n.x+r+2, n.y+3);
    }}
  }}
  ctx.globalAlpha=1;
}}
function loop(){{ for(let k=0;k<2;k++) tick(); draw(); requestAnimationFrame(loop); }}
loop();

cv.addEventListener('click', ev=>{{
  let best=-1, bd=1e9;
  for(let i=0;i<N.length;i++){{
    let dx=N[i].x-ev.clientX, dy=N[i].y-ev.clientY, d=dx*dx+dy*dy;
    if(d<bd){{ bd=d; best=i; }}
  }}
  if(bd>400) return;
  sel=best; const n=N[best];
  const outs=L.filter(l=>l.s===best).map(l=>`<div class="edge">--${{l.rel}} <span class="tag">${{l.conf}}</span>--&gt; ${{N[l.t].label}}</div>`);
  const ins=L.filter(l=>l.t===best).map(l=>`<div class="edge">${{N[l.s].label}} --${{l.rel}} <span class="tag">${{l.conf}}</span>--&gt;</div>`);
  info.innerHTML = `<h3>${{n.label}}</h3><div class="meta">${{n.file||''}}:${{n.loc||''}} · ${{n.kind||''}} · deg ${{deg[best]}}</div>`
    + (n.rat?`<p>${{n.rat}}</p>`:'')
    + `<b>out (${{outs.length}})</b>${{outs.join('')}}<b>in (${{ins.length}})</b>${{ins.join('')}}`;
}});
</script></body></html>
"""


def write_html(db: Db, out: Path) -> Path:
    data = build_graph_json(db)
    st = db.stats()
    html = _TEMPLATE.format(
        name=(db.get_meta("root") or ".").rstrip("/").rsplit("/", 1)[-1],
        data=json.dumps({"nodes": data["nodes"], "links": data["links"]}),
        n=st["nodes"], e=st["edges"], c=st["communities"],
    )
    out.mkdir(parents=True, exist_ok=True)
    target = out / HTML_NAME
    target.write_text(html, encoding="utf-8")
    return target
