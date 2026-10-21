#!/usr/bin/env python3
"""memwatch: per-app memory on macOS. Run, open http://localhost:8799 (PORT env to change)"""
import hashlib, json, os, re, subprocess, sys, threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 8799))
UNITS = {"B": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def size(s):  # top prints e.g. "3921M", "12K+", "1G-"
    m = re.match(r"([\d.]+)([BKMGT])", s)
    return int(float(m[1]) * UNITS[m[2]]) if m else 0


def app_name(path):
    # first .app in the path, so helpers roll up into their parent app
    m = re.search(r"/([^/]+)\.app/", path + "/")
    return m[1] if m else path.rsplit("/", 1)[-1]


def bundle_of(path):
    m = re.match(r"(/.*?\.app)/", path + "/")
    return m[1] if m else None


# App icons: NSWorkspace via JXA (same icon Finder shows), rendered to 64px PNGs, cached on disk.
ICONS = os.path.expanduser("~/Library/Caches/memwatch/icons")
ICON_JS = r"""ObjC.import('AppKit');
function run(argv) {
  const ws = $.NSWorkspace.sharedWorkspace;
  for (let i = 0; i < argv.length; i += 2) {
    try {
      const img = ws.iconForFile(argv[i]);
      const rep = $.NSBitmapImageRep.alloc.initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(null, 64, 64, 8, 4, true, false, $.NSCalibratedRGBColorSpace, 0, 0);
      $.NSGraphicsContext.saveGraphicsState;
      $.NSGraphicsContext.setCurrentContext($.NSGraphicsContext.graphicsContextWithBitmapImageRep(rep));
      img.drawInRectFromRectOperationFraction($.NSMakeRect(0, 0, 64, 64), $.NSZeroRect, $.NSCompositingOperationSourceOver, 1);
      $.NSGraphicsContext.restoreGraphicsState;
      rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $()).writeToFileAtomically(argv[i + 1], true);
    } catch (e) {}
  }
}"""
icon_lock = threading.Lock()
icon_failed = set()

# Size of each .app bundle on disk (du = allocated blocks, like Finder's "on disk"). Big apps take seconds,
# so it's measured in the background and kept in a JSON cache keyed like the icons (path + mtime).
DISK_CACHE = os.path.join(os.path.dirname(ICONS), "disk.json")
disk_lock = threading.Lock()
try:
    with open(DISK_CACHE) as f:
        disk_sizes = json.load(f)
except (OSError, ValueError):
    disk_sizes = {}


def measure_disk(todo):  # {key: bundle}; one batch at a time
    if not disk_lock.acquire(blocking=False):
        return
    try:
        for k, b in todo.items():
            out = sh("du", "-sk", os.path.realpath(b)).split()
            disk_sizes[k] = int(out[0]) * 1024 if out and out[0].isdigit() else -1  # -1: couldn't read
        os.makedirs(os.path.dirname(DISK_CACHE), exist_ok=True)
        with open(DISK_CACHE + ".tmp", "w") as f:
            json.dump(disk_sizes, f)
        os.replace(DISK_CACHE + ".tmp", DISK_CACHE)
    finally:
        disk_lock.release()


def icon_id(bundle):
    try:  # mtime in the key so an updated app gets its new icon
        return hashlib.sha1(f"{bundle}:{os.stat(bundle).st_mtime}".encode()).hexdigest()[:16]
    except OSError:
        return None


def fetch_icons(todo):  # {icon_id: bundle}; one osascript call per batch, one batch at a time
    if not icon_lock.acquire(blocking=False):
        return
    try:
        os.makedirs(ICONS, exist_ok=True)
        # realpath: /Applications/Safari.app is a symlink and would get an alias badge
        args = [x for i, b in todo.items() for x in (os.path.realpath(b), f"{ICONS}/{i}.png")]
        subprocess.run(["osascript", "-l", "JavaScript", "-", *args], input=ICON_JS, text=True, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        pass
    finally:
        icon_failed.update(i for i in todo if not os.path.exists(f"{ICONS}/{i}.png"))
        icon_lock.release()


def snapshot():
    paths = {}
    for line in sh("ps", "-axo", "pid=,comm=").splitlines():
        pid, _, path = line.strip().partition(" ")
        paths[pid] = path.strip()

    # top MEM = physical footprint (what Activity Monitor shows), CMPRS = compressed/swapped-out share
    apps = defaultdict(lambda: {"mem": 0, "cmprs": 0, "procs": [], "bundle": None})
    for line in sh("top", "-l", "1", "-stats", "pid,mem,cmprs").splitlines():
        parts = line.split()
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        pid, mem, cmprs = parts[0], size(parts[1]), size(parts[2])
        path = paths.get(pid, pid)
        a = apps[app_name(path)]
        a["bundle"] = a["bundle"] or bundle_of(path)
        a["mem"] += mem
        a["cmprs"] += cmprs
        a["procs"].append({"pid": int(pid), "name": path.rsplit("/", 1)[-1], "mem": mem, "cmprs": cmprs})

    rows =[{"app": k, **v, "procs": sorted(v["procs"], key=lambda p: -p["mem"])} for k, v in apps.items()]
    todo, disk_todo = {}, {}
    for r in rows:
        b = r.pop("bundle")
        i = icon_id(b) if b else None
        r["icon"] = i if i and os.path.exists(f"{ICONS}/{i}.png") else None
        if i and not r["icon"] and i not in icon_failed:
            todo[i] = b
        d = disk_sizes.get(i) if i else None
        r["disk"] = d if d is not None and d >= 0 else None
        if i and d is None:
            disk_todo[i] = b
    # icons and disk sizes show up on a later poll
    if todo:
        threading.Thread(target=fetch_icons, args=(todo,), daemon=True).start()
    if disk_todo:
        threading.Thread(target=measure_disk, args=(disk_todo,), daemon=True).start()
    return {"sys": sysmem(), "apps": sorted(rows, key=lambda r: -r["mem"])}


def sysmem():  # cheap (no top): also serves the menu bar number via /sys
    vm = sh("vm_stat")
    page = int(re.search(r"page size of (\d+)", vm)[1])
    pg = {k.strip(' "'): int(v.strip(" .")) * page for k, v in re.findall(r"^(.+?):\s+(\d+)\.$", vm, re.M)}
    swap = dict(re.findall(r"(total|used|free) = ([\d.]+M)", sh("sysctl", "-n", "vm.swapusage")))
    s = {
        "total": int(sh("sysctl", "-n", "hw.memsize")),
        "app": pg["Anonymous pages"] - pg["Pages purgeable"],
        "wired": pg["Pages wired down"],
        "compressed": pg["Pages occupied by compressor"],
        "cached": pg["File-backed pages"] + pg["Pages purgeable"],
        "free": pg["Pages free"] + pg["Pages speculative"],
        "swap_used": size(swap.get("used", "0M")),
        "swap_total": size(swap.get("total", "0M")),
        # % of memory the kernel considers available; low = pressure
        "available_pct": int(sh("sysctl", "-n", "kern.memorystatus_level") or 0),
    }
    s["used"] = s["app"] + s["wired"] + s["compressed"]  # Activity Monitor's "Memory Used"
    return s


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Memwatch</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400..600&family=Geist+Mono:wght@400..600&display=swap" rel="stylesheet">
<style>
:root{--bg:#fff;--bg2:#fafafa;--fg:#171717;--fg2:#666;--border:#eaeaea;--strong:#d4d4d4;
  --seg1:#2a78d6;--seg2:#a1a1a1;--seg3:#eb6834;--seg4:#d4d4d4;--warn:#f5a623;--err:#e5484d;--ok:#45a557;--sk:#ededed}
@media (prefers-color-scheme:dark){:root{--bg:#0a0a0a;--bg2:#111;--fg:#ededed;--fg2:#a1a1a1;--border:#242424;--strong:#3a3a3a;
  --seg1:#3987e5;--seg2:#5c5c5c;--seg3:#d95926;--seg4:#333;--sk:#1f1f1f}}
*{box-sizing:border-box;margin:0}
/* thin floating scrollbar: no track, thumb inset by a bg-colored border */
::-webkit-scrollbar{width:12px;height:12px;background:transparent}
::-webkit-scrollbar-track,::-webkit-scrollbar-corner{background:transparent}
::-webkit-scrollbar-thumb{background:var(--strong);border:3px solid var(--bg);border-radius:6px;min-height:40px}
::-webkit-scrollbar-thumb:hover{background:var(--fg2)}
body{background:var(--bg);color:var(--fg);font:14px/1.5 Geist,system-ui,sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:1080px;margin:0 auto;padding:28px 16px 64px}
/* Top strip. In the app it sits under the transparent native titlebar, which owns drag + double-click zoom,
   so it holds no controls. --tb/--tl come from the window's real metrics (Memwatch.swift); defaults suit a browser tab. */
:root{--tb:52px;--tl:16px}
header{position:sticky;top:0;z-index:2;height:var(--tb);display:flex;justify-content:space-between;align-items:center;gap:16px;
  padding:0 16px 0 var(--tl);background:var(--bg);-webkit-user-select:none;user-select:none;cursor:default}
.fs header{padding-left:16px}
.native{display:none;gap:4px;margin-left:8px}
.mb .native{display:flex}
.native button{font:13px Geist,system-ui,sans-serif;color:var(--fg2);background:none;border:0;border-radius:6px;padding:4px 8px;cursor:pointer}
.native button:hover{color:var(--fg);background:var(--bg2)}
.native button:focus-visible{outline:2px solid var(--fg2)}
.right{display:flex;align-items:center}
/* menu bar popover: compact */
.mb main{padding:12px 16px 24px}
.mb .kpis{margin:4px 0 12px;gap:12px 16px;grid-template-columns:1fr 1fr}
.mb .kpi .v{font-size:20px}
.mb .legend{margin-bottom:20px}
.mb caption,.mb .tools .meta,.mb th:nth-child(2),.mb td:nth-child(2),.mb th:nth-child(4),.mb td:nth-child(4),.mb th:nth-child(6),.mb td:nth-child(6){display:none}
.mb input{width:100%}
.mb th,.mb td{padding-left:8px;padding-right:8px}
.mb tr.proc td:first-child{padding-left:50px}
h1{font-size:14px;font-weight:600}
.meta{color:var(--fg2);font:13px 'Geist Mono',monospace}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:24px;margin:32px 0 16px}
.kpi .l{color:var(--fg2);font-size:13px}
.kpi .v{font-size:24px;font-weight:600;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.kpi .s{color:var(--fg2);font-size:13px;font-variant-numeric:tabular-nums}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px}
/* two accents for what matters (app blue, compressed orange; validated light+dark), neutrals for the rest */
.bar{display:flex;gap:2px;height:6px;border-radius:3px;overflow:hidden;background:var(--bg2)}
.bar span{min-width:0}
.legend{display:flex;flex-wrap:wrap;gap:4px 20px;margin:10px 0 40px;font-size:13px;color:var(--fg2);font-variant-numeric:tabular-nums}
.legend i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px}
.tools{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px}
input{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:6px 10px;width:240px;max-width:100%}
input:focus{outline:2px solid var(--fg2);outline-offset:1px}
.wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
caption{text-align:left;color:var(--fg2);font-size:13px;padding-bottom:8px}
th{font-weight:500;color:var(--fg2);font-size:13px;text-align:left;padding:8px 12px;border-bottom:1px solid var(--border);cursor:pointer;user-select:none;white-space:nowrap}
th[aria-sort]{color:var(--fg)}
th[aria-sort=descending]::after{content:" ↓"}
th[aria-sort=ascending]::after{content:" ↑"}
td{padding:8px 12px;border-bottom:1px solid var(--border);vertical-align:baseline}
.n{text-align:right}
tr.app[aria-expanded]{cursor:pointer}
tr.app:hover td{background:var(--bg2)}
tr.app:focus-visible{outline:2px solid var(--fg2);outline-offset:-2px}
tr.proc td{color:var(--fg2);font-size:13px;border-bottom-color:transparent}
tr.proc td:first-child{padding-left:54px}
.dim{color:var(--fg2)}
.pid{font-family:'Geist Mono',monospace;font-size:12px}
.share{display:inline-block;width:80px;height:4px;background:var(--bg2);border-radius:2px;vertical-align:middle;margin-left:8px}
.share b{display:block;height:100%;background:var(--fg);border-radius:2px}
.chev{display:inline-flex;width:16px;margin-left:-4px;color:var(--fg2);vertical-align:-1px}
.chev svg{transition:transform .12s ease-out}
tr.app[aria-expanded=true] .chev svg{transform:rotate(90deg)}
tr.app:hover .chev{color:var(--fg)}
.ico{display:inline-block;width:20px;height:20px;margin-right:10px;vertical-align:-5px}
.ph{overflow:hidden;text-align:center;border-radius:5px;background:var(--bg2);border:1px solid var(--border);
  color:var(--fg2);font-size:11px;font-weight:500;line-height:18px}
/* skeletons sit inside the real line boxes, so swapping in data causes no layout shift */
.sk{display:inline-block;height:12px;border-radius:4px;background:var(--sk);vertical-align:middle}
.sk.ico{height:20px;border-radius:5px;vertical-align:-5px}
.bar.loading{background:var(--sk);border-color:transparent}
.sk,.bar.loading{animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{50%{opacity:.45}}
@media (prefers-reduced-motion:reduce){.sk,.bar.loading{animation:none}.chev svg{transition:none}}
@media (max-width:640px){.share{display:none}}
</style></head><body>
<header><h1>Memwatch</h1><div class="right"><span class="meta" id="ts">sampling…</span>
<nav class="native"><button data-act="settings">Settings</button><button data-act="quit">Quit</button></nav></div></header>
<main aria-busy="true">
<section class="kpis" id="kpis"></section>
<div class="bar loading" id="bar" role="img" aria-label="Loading"></div>
<div class="legend" id="legend"></div>
<div class="tools"><input id="q" type="search" placeholder="Filter apps" aria-label="Filter apps">
<span class="meta" id="hint">click a row for processes</span></div>
<div class="wrap"><table><caption>RAM by app. RAM = physical footprint (Activity Monitor calls it Memory). Compressed = part of it squeezed in RAM or swapped to disk. On disk = size of the app itself on your drive.</caption>
<thead><tr><th data-k="app" title="App name. Helper processes are counted under their parent app.">App</th>
<th class="n" data-k="n" title="Number of running processes for this app">Procs</th>
<th class="n" data-k="mem" title="Physical footprint: RAM this app is using right now, including its compressed part. Same number as Activity Monitor's Memory column.">RAM</th>
<th class="n" data-k="cmprs" title="Part of the RAM number that macOS has compressed in RAM or moved to swap on disk">Compressed</th>
<th class="n" data-k="mem" id="share" title="RAM use as a share of your installed RAM">RAM %</th>
<th class="n" data-k="disk" title="Space the app itself takes on your drive (its .app bundle). Doesn't include its documents, caches or other data.">On disk</th></tr></thead>
<tbody id="rows"></tbody></table></div>
</main><script>
const $=id=>document.getElementById(id), open=new Set();
let data=null, sortK='mem', asc=false;
const fmt=b=>b>=1<<30?(b/(1<<30)).toFixed(2)+' GB':b>=1<<20?(b/(1<<20)).toFixed(0)+' MB':(b/1024).toFixed(0)+' KB';
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// app shell passes native titlebar metrics + fullscreen state
const P=new URLSearchParams(location.search), R=document.documentElement;
for(const k of ['tb','tl']){const v=+P.get(k); if(v>0) R.style.setProperty('--'+k,v+'px')}
if(P.has('fs')) R.classList.add('fs');
if(P.has('menubar')) R.classList.add('mb');
const every=Math.max(1,+P.get('r')||3), native=window.webkit?.messageHandlers?.mem;
$('hint').textContent=`refresh ${every}s · click a row for processes`;
document.querySelector('.native').onclick=e=>{const a=e.target.dataset.act; if(a) native?.postMessage({action:a})};
const CHEV='<svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3.5 10.5 8 6 12.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const icon=a=>a.icon?`<img class="ico" src="/icon/${a.icon}.png" alt="">`:`<span class="ico ph" aria-hidden="true">${esc(([...a.app][0]||'?').toUpperCase())}</span>`;
const sk=(w,h=12)=>`<span class="sk" style="width:${w}px;height:${h}px"></span>`;
$('kpis').innerHTML=[72,64,80,60].map(w=>`<div class="kpi"><div class="l">${sk(w)}</div><div class="v">${sk(w+56,22)}</div><div class="s">${sk(w-8)}</div></div>`).join('');
$('legend').innerHTML=[64,72,96,92,64].map(w=>`<span>${sk(w)}</span>`).join('');
$('rows').innerHTML=[128,104,88,136,64,112,96,72,120,80].map((w,i)=>`<tr style="opacity:${1-i*.08}"><td><span class="chev"></span><span class="sk ico"></span>${sk(w)}</td><td class="n">${sk(20)}</td><td class="n">${sk(56)}</td><td class="n">${sk(52)}</td><td class="n">${sk(36)}<span class="share">${sk(80,4)}</span></td><td class="n">${sk(48)}</td></tr>`).join('');
function render(){
  if(!data) return;
  document.querySelector('main').removeAttribute('aria-busy'); $('bar').classList.remove('loading');
  const s=data.sys, used=s.app+s.wired+s.compressed;
  const p=s.available_pct, [pc,pl]=p<15?['var(--err)','High']:p<35?['var(--warn)','Elevated']:['var(--ok)','Normal'];
  $('kpis').innerHTML=[
    ['RAM used',fmt(used),'of '+fmt(s.total)],
    ['Swap used',fmt(s.swap_used),'of '+fmt(s.swap_total)],
    ['Compressed',fmt(s.compressed),'in RAM'],
    ['Pressure',`<span class="dot" style="background:${pc}"></span>${pl}`,p+'% available'],
  ].map(([l,v,x])=>`<div class="kpi"><div class="l">${l}</div><div class="v">${v}</div><div class="s">${x}</div></div>`).join('');
  const seg=[['App',s.app,'--seg1'],['Wired',s.wired,'--seg2'],['Compressed',s.compressed,'--seg3'],['Cached files',s.cached,'--seg4']];
  $('bar').innerHTML=seg.map(([l,v,c])=>`<span title="${l} ${fmt(v)}" style="width:${v/s.total*100}%;background:var(${c})"></span>`).join('');
  $('bar').setAttribute('aria-label',seg.map(([l,v])=>l+' '+fmt(v)).join(', '));
  $('legend').innerHTML=seg.map(([l,v,c])=>`<span><i style="background:var(${c})"></i>${l} ${fmt(v)}</span>`).join('')+`<span>Free ${fmt(s.free)}</span>`;
  const q=$('q').value.toLowerCase();
  const rows=data.apps.filter(a=>!q||a.app.toLowerCase().includes(q)||a.procs.some(x=>x.name.toLowerCase().includes(q)))
    .map(a=>({...a,n:a.procs.length}))
    .sort((a,b)=>{const x=a[sortK]??-1,y=b[sortK]??-1;return (typeof x=='string'?x.localeCompare(y):x-y)*(asc?1:-1)});
  const max=Math.max(...data.apps.map(a=>a.mem));
  const focused=document.activeElement?.closest?.('tr.app')?.dataset.a;
  $('rows').innerHTML=rows.slice(0,200).map(a=>{
    const o=open.has(a.app), pct=a.mem/s.total*100;
    let h=`<tr class="app" data-a="${esc(a.app)}"${a.n>1?` tabindex="0" aria-expanded="${o}"`:''}><td><span class="chev">${a.n>1?CHEV:''}</span>${icon(a)}${esc(a.app)}</td><td class="n">${a.n}</td><td class="n">${fmt(a.mem)}</td><td class="n">${fmt(a.cmprs)}</td><td class="n">${pct.toFixed(1)}%<span class="share"><b style="width:${a.mem/max*100}%"></b></span></td><td class="n">${a.disk!=null?fmt(a.disk):`<span class="dim" title="${a.icon?'Measuring…':'Not an app bundle'}">—</span>`}</td></tr>`;
    if(o&&a.n>1) h+=a.procs.map(x=>`<tr class="proc"><td>${esc(x.name)} <span class="pid">${x.pid}</span></td><td></td><td class="n">${fmt(x.mem)}</td><td class="n">${fmt(x.cmprs)}</td><td class="n">${(x.mem/s.total*100).toFixed(1)}%</td><td></td></tr>`).join('');
    return h}).join('');
  // rows are rebuilt every refresh; put keyboard focus back where it was
  if(focused!=null) [...$('rows').querySelectorAll('tr.app')].find(r=>r.dataset.a===focused)?.focus({preventScroll:true});
  document.querySelectorAll('th').forEach(th=>th.dataset.k===sortK&&th.id!=='share'
    ? th.setAttribute('aria-sort',asc?'ascending':'descending') : th.removeAttribute('aria-sort'));
  $('share').textContent=`${R.classList.contains('mb')?'':'RAM '}% of ${fmt(s.total).replace('.00','')}`;
}
function toggle(r){
  const a=r.dataset.a; open.has(a)?open.delete(a):open.add(a);
  r.setAttribute('aria-expanded',open.has(a));  // rotate the existing chevron first, then rebuild rows
  setTimeout(render,120);
}
$('rows').onclick=e=>{const r=e.target.closest('tr.app[aria-expanded]'); if(r) toggle(r)};
$('rows').onkeydown=e=>{if((e.key==='Enter'||e.key===' ')&&e.target.matches('tr.app[aria-expanded]')){e.preventDefault(); toggle(e.target)}};
document.querySelector('thead').onclick=e=>{const k=e.target.dataset.k; if(!k) return; asc=k===sortK?!asc:k==='app'; sortK=k; render()};
$('q').oninput=render;
async function tick(){
  try{const r=await fetch('/api'), j=await r.json(); if(!r.ok) throw j.error;
    data=j; $('ts').textContent='updated '+new Date().toLocaleTimeString(); render()}
  catch(e){$('ts').textContent=typeof e=='string'?'sample failed, retrying':'server not reachable'}
  timer=setTimeout(tick,every*1000);
}
let timer; tick();
window.refreshNow=()=>{clearTimeout(timer); tick()};  // native calls this when the popover opens
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        code, cache = 200, "no-store"
        try:
            icon = re.fullmatch(r"/icon/([0-9a-f]{16})\.png", self.path)  # strict id: no path traversal
            if icon:
                with open(f"{ICONS}/{icon[1]}.png", "rb") as f:
                    body, ctype, cache = f.read(), "image/png", "max-age=31536000, immutable"
            elif self.path in ("/api", "/sys"):
                body, ctype = json.dumps(snapshot() if self.path == "/api" else sysmem()).encode(), "application/json; charset=utf-8"
            else:
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        except FileNotFoundError:
            body, ctype, code = b"", "text/plain", 404
        except Exception as e:  # one bad sample shouldn't kill the server; page retries in 3s
            body, ctype, code = json.dumps({"error": repr(e)}).encode(), "application/json", 500
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if "--check" in sys.argv:
        assert size("3921M") == 3921 << 20 and size("12K+") == 12 << 10 and size("1.5G") == 3 << 29
        assert app_name("/Applications/Brave Browser.app/Contents/Frameworks/x/Brave Browser Helper.app/Contents/MacOS/h") == "Brave Browser"
        assert app_name("/usr/libexec/logd") == "logd"
        s = snapshot()
        assert s["apps"] and s["sys"]["total"] > 0
        print("ok", len(s["apps"]), "apps; top:", [(a["app"], a["mem"] >> 20) for a in s["apps"][:5]])
    else:
        app = "--app" in sys.argv
        srv = ThreadingHTTPServer(("127.0.0.1", 0 if app else PORT), H)  # app: OS picks a free port
        srv.daemon_threads = True
        if app:
            # parent app reads this line for the port; stdin EOF = app gone (even if it crashed) -> exit
            print(f"PORT {srv.server_address[1]}", flush=True)
            threading.Thread(target=lambda: (sys.stdin.read(), os._exit(0)), daemon=True).start()
        else:
            print(f"memwatch on http://localhost:{PORT}")
        srv.serve_forever()
