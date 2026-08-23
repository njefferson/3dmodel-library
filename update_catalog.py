#!/usr/bin/env python3
"""
update_catalog.py  -  Incremental 3D-print library updater.

Runs ON YOUR WINDOWS MACHINE (native paths). Re-scans one or more folders and
MERGES results into the existing catalog.json / catalog.csv in this folder,
without a full rebuild:

  * Each project/archive gets a STABLE id = sha1 of its normalized absolute path.
    So re-scanning the same folder UPDATES entries in place instead of duplicating.
  * New folders are appended. Untouched entries are left exactly as they are.
  * Thumbnails are only rendered when missing (existing .webp files are skipped),
    so re-running after Dropbox finishes downloading fills the gaps cheaply.
  * Nothing in the source folders is ever moved, renamed, or modified (read-only).

Easiest way to drive it on Windows: double-click LIBRARY.bat (a menu).

USAGE (run from inside the library folder):
    python update_catalog.py "D:\\path\\to\\your\\models"   # add/scan a folder
    python update_catalog.py --rescan-all       # re-walk every folder in sources.txt
    python update_catalog.py --thumbs-only      # just render any missing thumbnails
    python update_catalog.py --diagnose         # what's readable, is rendering working
    python update_catalog.py                    # print the full command list

Classification (categories, factions, labels) is keyword-driven and lives in
rules.json next to this script - edit it freely, or empty it out if you don't
want any of it. Nothing in your source folders is ever moved, renamed or
modified; this tool only ever reads them.

REQUIREMENTS (one-time):
    pip install -r requirements.txt
"""
import os, sys, json, csv, re, io, hashlib, signal, argparse
LIB = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(LIB, "catalog.json")
CSVFILE = os.path.join(LIB, "catalog.csv")
THUMBS  = os.path.join(LIB, "thumbnails"); os.makedirs(THUMBS, exist_ok=True)

MODEL={".stl",".obj",".3mf",".step",".stp",".ply"}
SLICER={".gcode",".ctb",".lys",".lychee",".cbddlp",".photon",".pwmx",".pwms",".fdg",".goo",".chitubox",".zcode"}
IMG={".png",".jpg",".jpeg",".webp",".bmp",".gif",".tif",".tiff"}
ARCH={".zip",".rar",".7z"}

def stable_id(p): return hashlib.sha1(p.strip().lower().replace("/","\\").encode("utf-8","ignore")).hexdigest()[:16]

# ---------- classification: all keyword rules come from rules.json ----------
RULES_FILE=os.path.join(LIB,"rules.json")
LOCAL_RULES=os.path.join(LIB,"rules.local.json")
_RULES=None
def rules():
    """Load rules.local.json if present, else rules.json. Missing/broken -> {} so
    everything still runs with neutral behaviour instead of crashing."""
    global _RULES
    if _RULES is None:
        _RULES={}
        for p in (LOCAL_RULES, RULES_FILE):
            if os.path.exists(p):
                try:
                    with open(p,encoding="utf-8") as fp: _RULES=json.load(fp)
                    break
                except Exception as e:
                    print(f"  [warning] {os.path.basename(p)} could not be read ({e}).")
                    print( "            Using neutral defaults - fix the file or delete it.")
    return _RULES

def _clean(d):
    """dict from rules.json minus the _comment helper keys"""
    return {k:v for k,v in (d or {}).items() if not k.startswith("_") and isinstance(v,str)}

def _rx(pat):
    try: return re.compile(pat, re.I) if pat else None
    except re.error as e:
        print(f"  [warning] bad pattern in rules.json ignored: {pat[:40]}... ({e})"); return None

_CACHE={}
def _compiled():
    if _CACHE: return _CACHE
    R=rules()
    _CACHE["factions"]=[(k,_rx(v)) for k,v in _clean(R.get("factions")).items()]
    _CACHE["categories"]=[(k,_rx(v)) for k,v in _clean(R.get("categories")).items()]
    _CACHE["types"]=[(k,_rx(v)) for k,v in _clean(R.get("types")).items()]
    _CACHE["sources"]=[(k,_rx(v)) for k,v in _clean(R.get("sources")).items()]
    _CACHE["wargaming"]=_rx(R.get("wargaming_keywords",""))
    _CACHE["wargaming_cat"]=R.get("wargaming_category","Wargaming")
    _CACHE["default_cat"]=R.get("default_category","Uncategorised")
    _CACHE["generic"]={s.strip().lower() for s in R.get("generic_folder_names",[]) if isinstance(s,str)}
    return _CACHE

def source_tag(path):
    """Label for where an item came from. Uses rules.json['sources'] patterns if the
    user defined any; otherwise falls back to the name of the scanned folder it sits
    under, which needs no configuration at all."""
    C=_compiled()
    for label,rx in C["sources"]:
        if rx and rx.search(path): return label
    p=path.replace("/","\\")
    best=None
    for root in load_sources():
        r=root.replace("/","\\").rstrip("\\")
        if p.lower().startswith(r.lower()+"\\") or p.lower()==r.lower():
            if best is None or len(r)>len(best): best=r
    if best:
        rest=p[len(best):].strip("\\").split("\\")
        top=os.path.basename(best) or best
        if rest and rest[0] and len(rest)>1:
            return f"{top}/{rest[0]}"[:60]
        return top
    seg=[s for s in p.split("\\") if s]
    return seg[-2] if len(seg)>1 else (seg[0] if seg else "unknown")

def classify(path, name):
    """Return (category, faction, source, tags). Everything is keyword-driven; with an
    empty rules.json every item simply lands in the default category with no faction."""
    C=_compiled()
    s=(path+" "+name).lower()
    src=source_tag(path)
    faction=None
    for fname,rx in C["factions"]:
        if rx and rx.search(s): faction=fname; break
    war=C["wargaming"]
    is_war = faction is not None or bool(war and war.search(s))
    cat=None
    if is_war:
        cat=C["wargaming_cat"]
    else:
        for cname,rx in C["categories"]:
            if rx and rx.search(s): cat=cname; break
    if not cat: cat=C["default_cat"]
    tags=[]
    if faction: tags.append("faction:"+faction)
    for tname,rx in C["types"]:
        if rx and rx.search(s): tags.append("type:"+tname)
    return cat, faction, src, tags


# ---------- rendering (host-native paths, no mount translation) ----------
class TO(Exception): pass
# Per-render timeout via SIGALRM is Unix-only; on Windows it's unavailable, so
# we degrade gracefully to no per-render timeout (face_cap keeps renders bounded).
_HAS_ALARM = hasattr(signal, "SIGALRM")
if _HAS_ALARM:
    signal.signal(signal.SIGALRM, lambda s,f:(_ for _ in ()).throw(TO()))
def _alarm(n):
    if _HAS_ALARM:
        signal.alarm(n)
def _render(model_paths, out_path, timeout=25):
    try:
        import numpy as np, matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from mpl_toolkits.mplot3d.art3d import Poly3DCollection
        from PIL import Image; import trimesh
        _alarm(timeout)
        meshes=[]
        for mp in model_paths:
            try:
                m=trimesh.load(mp, force="mesh")
                if isinstance(m,trimesh.Scene): m=m.dump(concatenate=True)
                if not (hasattr(m,"faces") and len(m.faces)>0) and mp.lower().endswith(".stl"):
                    d=open(mp,"rb").read(); body=d[84:]; nt=len(body)//50
                    if nt>0:
                        a=np.frombuffer(body[:nt*50],dtype=np.uint8).reshape(nt,50)
                        v=a[:,12:48].copy().view("<f4").reshape(-1,3)
                        m=trimesh.Trimesh(vertices=v,faces=np.arange(nt*3).reshape(nt,3),process=False)
                if hasattr(m,"faces") and len(m.faces)>0: meshes.append(m)
            except Exception: continue
        if not meshes: _alarm(0); return None
        mesh=trimesh.util.concatenate(meshes) if len(meshes)>1 else meshes[0]
        v=np.asarray(mesh.vertices,dtype=float); f=np.asarray(mesh.faces)
        CAP=16000
        if len(f)>CAP:
            reduced=False
            try:                              # best: real quadric decimation -> solid surface
                dm=mesh.simplify_quadric_decimation(CAP)
                if len(getattr(dm,"faces",[]))>0:
                    v=np.asarray(dm.vertices,dtype=float); f=np.asarray(dm.faces); reduced=True
            except Exception:
                reduced=False
            if not reduced:                   # direct fast-simplification (real decimation, solid)
                try:
                    import fast_simplification as _fs
                    vv,ff=_fs.simplify(np.asarray(mesh.vertices,dtype=np.float32),
                                       np.asarray(mesh.faces,dtype=np.int32), target_count=CAP)
                    if len(ff)>0: v=np.asarray(vv,dtype=float); f=np.asarray(ff); reduced=True
                except Exception:
                    reduced=False
            if not reduced:                   # last resort: stride (keeps adjacent faces)
                step=int(np.ceil(len(f)/CAP)); f=f[::step]
        # tight, robust framing: center on the bulk's bounding box, fill the frame,
        # and match the box aspect so thin/elongated parts don't render as a speck.
        lo=np.percentile(v,1,axis=0); hi=np.percentile(v,99,axis=0)
        ctr=(lo+hi)/2.0; v=v-ctr; half=(hi-lo)/2.0
        half=np.where(half<=0,1e-6,half)
        tri=v[f]
        n=np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]); ln=np.linalg.norm(n,axis=1); ln[ln==0]=1; n=n/ln[:,None]
        # Two-sided lighting. Downloaded STLs very often have flipped or
        # inconsistent normals; using |n.L| instead of clipping at 0 means a
        # backwards-wound face lights identically to a correct one, so those
        # patchy black facets disappear. Second light fills the shadow side.
        L=np.array([.3,.4,.86]);   L=L/np.linalg.norm(L)
        L2=np.array([-.55,-.35,.4]); L2=L2/np.linalg.norm(L2)
        shade=np.clip(0.34+0.54*np.abs(n@L)+0.18*np.abs(n@L2),0,1)
        base=np.array([.80,.83,.90]); col=np.zeros((len(f),4)); col[:,:3]=np.clip(base*shade[:,None],0,1); col[:,3]=1
        fig=plt.figure(figsize=(5.12,5.12),dpi=100); ax=fig.add_subplot(111,projection="3d")
        ax.add_collection3d(Poly3DCollection(tri,facecolors=col,edgecolors="none",linewidths=0))
        pad=1.06
        ax.set_xlim(-half[0]*pad,half[0]*pad);ax.set_ylim(-half[1]*pad,half[1]*pad);ax.set_zlim(-half[2]*pad,half[2]*pad)
        asp=half/half.max(); asp=np.clip(asp,0.12,1.0)
        ax.set_box_aspect(tuple(asp))
        try: ax.set_proj_type("ortho")
        except Exception: pass
        ax.view_init(26,-58); ax.set_axis_off()
        fig.patch.set_facecolor("#20242c"); ax.set_facecolor("#20242c"); fig.subplots_adjust(0,0,1,1)
        buf=io.BytesIO(); fig.savefig(buf,format="png",facecolor="#20242c"); plt.close(fig); buf.seek(0)
        _save_webp(Image.open(buf), out_path); _alarm(0); return True
    except TO:
        _alarm(0); return None
    except Exception:
        _alarm(0); return None

def _reuse(img_path, out_path, timeout=20):
    try:
        from PIL import Image; _alarm(timeout)
        im=Image.open(img_path); im.load(); _save_webp(im,out_path); _alarm(0); return True
    except Exception:
        _alarm(0); return None

def _save_webp(im, out_path, edge=512, ceiling=200000):
    from PIL import Image
    im=im.convert("RGB"); w,h=im.size; sc=edge/max(w,h)
    if sc<1: im=im.resize((max(1,int(w*sc)),max(1,int(h*sc))),Image.LANCZOS)
    q=85
    while q>=35:
        b=io.BytesIO(); im.save(b,"WEBP",quality=q,method=4)
        if b.tell()<=ceiling or q==35: open(out_path,"wb").write(b.getvalue()); return
        q-=10

PREVIEW_HINT=re.compile(r"render|preview|thumb|_prev|display|promo|cover|hero|showcase",re.I)

# ---------- scan a folder into project/archive records ----------
def scan_folder(root):
    projects={}; archives={}
    for dp,dns,fns in os.walk(root):
        models=[]; images=[]
        for f in fns:
            ext=os.path.splitext(f)[1].lower()
            full=os.path.join(dp,f)
            try: sz=os.path.getsize(full)
            except OSError: sz=0
            if ext in MODEL: models.append((f,sz))
            elif ext in IMG: images.append((f,sz))
            elif ext in ARCH:
                aid=stable_id(full); cat,fac,src,tags=classify(full,os.path.splitext(f)[0])
                archives[aid]={"id":aid,"type":"archive","name":os.path.splitext(f)[0],"path":full,
                  "source":src,"category":cat,"faction":fac,"packed":True,"part_count":None,
                  "formats":[ext.lstrip(".")],"size_bytes":sz,"thumbnail":None,"thumb_status":"packed",
                  "tags":tags+["format:"+ext.lstrip("."),"source:"+src,"packed:true"],"model_files":[]}
        if models:
            pid=stable_id(dp); name=os.path.basename(dp) or dp
            cat,fac,src,tags=classify(dp,name)
            fmts=sorted(set(os.path.splitext(m)[1].lstrip(".") for m,_ in models))
            hinted=[i for i in images if PREVIEW_HINT.search(i[0])] or images
            prev=max(hinted,key=lambda x:x[1])[0] if images else None
            projects[pid]={"id":pid,"type":"project","name":name,"path":dp,"source":src,
              "category":cat,"faction":fac,"packed":False,"part_count":len(models),"formats":fmts,
              "size_bytes":sum(s for _,s in models),"thumbnail":None,"thumb_status":"pending",
              "has_shipped_preview":prev is not None,"preview_file":os.path.join(dp,prev) if prev else None,
              "tags":tags+["format:"+f for f in fmts]+["source:"+src,"packed:false"],
              "model_files":[m for m,_ in models]}
    return projects, archives

def _renderable_now(it):
    """(prev_ok, model_ok) using attribute reads only — never triggers a download."""
    mfs=it.get("model_files") or []; prev=it.get("preview_file")
    prev_ok=bool(it.get("has_shipped_preview") and prev and _hydrated(prev) is True)
    model_ok=bool(mfs and _hydrated(os.path.join(it["path"],mfs[0])) is True)
    return prev_ok, model_ok

# ---------- fast path: the Windows shell thumbnail handler (what Explorer uses) ----------
def _hbitmap_to_pil(hbitmap):
    import ctypes
    from ctypes import wintypes
    from PIL import Image
    gdi32=ctypes.windll.gdi32; user32=ctypes.windll.user32
    class BITMAP(ctypes.Structure):
        _fields_=[("bmType",wintypes.LONG),("bmWidth",wintypes.LONG),("bmHeight",wintypes.LONG),
                  ("bmWidthBytes",wintypes.LONG),("bmPlanes",wintypes.WORD),
                  ("bmBitsPixel",wintypes.WORD),("bmBits",ctypes.c_void_p)]
    class BMIH(ctypes.Structure):
        _fields_=[("biSize",wintypes.DWORD),("biWidth",wintypes.LONG),("biHeight",wintypes.LONG),
                  ("biPlanes",wintypes.WORD),("biBitCount",wintypes.WORD),("biCompression",wintypes.DWORD),
                  ("biSizeImage",wintypes.DWORD),("biXPelsPerMeter",wintypes.LONG),
                  ("biYPelsPerMeter",wintypes.LONG),("biClrUsed",wintypes.DWORD),("biClrImportant",wintypes.DWORD)]
    class BMI(ctypes.Structure):
        _fields_=[("bmiHeader",BMIH),("bmiColors",wintypes.DWORD*3)]
    bm=BITMAP()
    if not gdi32.GetObjectW(int(hbitmap),ctypes.sizeof(bm),ctypes.byref(bm)): return None
    w,h=int(bm.bmWidth),int(bm.bmHeight)
    if w<=0 or h<=0: return None
    bmi=BMI(); bmi.bmiHeader.biSize=ctypes.sizeof(BMIH)
    bmi.bmiHeader.biWidth=w; bmi.bmiHeader.biHeight=-h   # negative -> top-down
    bmi.bmiHeader.biPlanes=1; bmi.bmiHeader.biBitCount=32; bmi.bmiHeader.biCompression=0
    buf=ctypes.create_string_buffer(w*h*4)
    hdc=user32.GetDC(0)
    got=gdi32.GetDIBits(hdc,int(hbitmap),0,h,buf,ctypes.byref(bmi),0)
    user32.ReleaseDC(0,hdc)
    if not got: return None
    return Image.frombuffer("RGBA",(w,h),buf.raw,"raw","BGRA",0,1)

_ICON_SEEN={}
def _icon_guard(img, repeat_limit=3):
    """True if this looks like a real per-file thumbnail. Windows returns the SAME
    generic icon for files it can't render; if one image repeats across different
    files, treat it as an icon and reject it (caller falls back to the mesh render)."""
    try:
        import hashlib
        h=hashlib.md5(img.convert("RGB").resize((32,32)).tobytes()).hexdigest()
    except Exception:
        return True
    n=_ICON_SEEN.get(h,0)+1; _ICON_SEEN[h]=n
    return n < repeat_limit

def _shell_thumb(src_path, out_path, size=512, bg=(32,36,44)):
    """Render a thumbnail via Windows' own shell handler (Explorer). Fast + cached.
    Returns True on success, False if unavailable/failed (caller falls back to mesh)."""
    try:
        import pythoncom, ctypes
        from win32com.shell import shell
        from PIL import Image
        try: pythoncom.CoInitialize()
        except Exception: pass
        sii=shell.SHCreateItemFromParsingName(src_path, None, shell.IID_IShellItemImageFactory)
        # BIGGERSIZEOK(0x1) only. THUMBNAILONLY(0x8) was rejecting every file on this
        # machine, so it's dropped — but that means the shell may hand back a GENERIC
        # FILE ICON instead of a real render. _icon_guard() below catches that.
        SIIGBF_BIGGERSIZEOK=0x1
        hbmp=sii.GetImage((size,size), SIIGBF_BIGGERSIZEOK)
        try:
            img=_hbitmap_to_pil(int(hbmp))
        finally:
            try: ctypes.windll.gdi32.DeleteObject(int(hbmp))
            except Exception: pass
        if img is None: return False
        if not _icon_guard(img): return False   # generic file icon, not a real render
        base=Image.new("RGB",img.size,bg)
        if img.mode=="RGBA":
            alpha=img.split()[3]
            if alpha.getextrema()==(255,255): base.paste(img.convert("RGB"))
            else: base.paste(img, mask=alpha)
        else:
            base.paste(img.convert("RGB"))
        _save_webp(base, out_path)
        return True
    except Exception:
        return False

def _thumb_for(it, out_path, engine="shell"):
    """Produce one thumbnail: reuse shipped preview, else shell handler, else mesh render."""
    prev_ok,model_ok=_renderable_now(it)
    if prev_ok and _reuse(it["preview_file"], out_path): return "reused"
    if not model_ok: return "pending"
    mfs=it.get("model_files") or []
    best=None; bestsz=-1
    for f in mfs:                      # largest hydrated part = most representative
        p=os.path.join(it["path"],f)
        if _hydrated(p) is not True: continue
        try: sz=os.path.getsize(p)
        except OSError: continue
        if sz>bestsz: bestsz=sz; best=p
    if engine!="mesh" and best and _shell_thumb(best, out_path): return "shell"
    mps=[os.path.join(it["path"],f) for f in mfs]
    if _render(mps, out_path): return "mesh"
    return "pending"

_ENGINE="shell"
def _init_worker(engine):
    global _ENGINE; _ENGINE=engine

def render_one(it):
    """Pool worker. Uses the module-global engine (set via pool initializer). Never raises."""
    try:
        return (it["id"], _thumb_for(it, os.path.join(THUMBS, it["id"]+".webp"), _ENGINE))
    except Exception:
        return (it["id"],"pending")

def render_missing(items, checkpoint=None, force=False, jobs=1, engine="shell", max_mb=1500):
    import time
    global _ENGINE; _ENGINE=engine   # used by the serial path (render_one reads it)
    idmap={it["id"]:it for it in items if it.get("type")=="project"}
    targets=[]; pending_total=0
    for it in items:
        if it.get("type")!="project": continue
        if not force and os.path.exists(os.path.join(THUMBS,it["id"]+".webp")):
            it["thumbnail"]=it["id"]+".webp"; continue
        pending_total+=1
        po,mo=_renderable_now(it)
        if po or mo: targets.append(it)
        else: it["thumb_status"]="pending"
    # Smallest first: the gallery fills up fast and you can look at real results
    # long before the giant outliers are reached.
    targets.sort(key=lambda it: it.get("size_bytes",0))
    deferred=[it for it in targets if it.get("size_bytes",0)>max_mb*1e6]
    targets=[it for it in targets if it.get("size_bytes",0)<=max_mb*1e6]
    total=len(targets); cloud=pending_total-total-len(deferred)
    jobs=max(1,int(jobs))
    if deferred:
        print(f"  deferring {len(deferred)} huge project(s) over {max_mb:.0f} MB "
              f"(run with --max-mb 99999 to include them).", flush=True)
    print(f"  {pending_total} projects need a thumbnail; {total} downloaded & renderable now, "
          f"{cloud} still in the cloud{' (FORCE re-render)' if force else ''}.", flush=True)
    print(f"  Rendering with {jobs} core(s), engine='{engine}'. Safe to press Ctrl+C anytime —", flush=True)
    print("  finished thumbnails are kept and re-running resumes where it stopped.\n", flush=True)
    done=0; t0=time.time()
    def report(k):
        el=time.time()-t0; rate=el/max(k,1); left=(total-k)*rate
        print(f"  {k}/{total} done ({done} ok)  {el:.0f}s elapsed  ~{int(left//60)}m{int(left%60):02d}s left", flush=True)
    def apply(pid,status):
        nonlocal done
        it=idmap.get(pid)
        if it is None: return
        it["thumb_status"]=status
        if status in ("reused","shell","mesh","rendered"): it["thumbnail"]=pid+".webp"; done+=1
    if jobs>1 and total:
        try:
            import multiprocessing as mp
            with mp.Pool(jobs, initializer=_init_worker, initargs=(engine,)) as pool:
                for k,(pid,status) in enumerate(pool.imap_unordered(render_one, targets, chunksize=1),1):
                    apply(pid,status)
                    if k%5==0 or k==total: report(k)
                    if checkpoint and (k%150==0 or k==50):
                        checkpoint()
                        print("   [gallery.html refreshed — you can open it now]", flush=True)
            print(f"\n  finished: {done}/{total} rendered in {time.time()-t0:.0f}s "
                  f"({cloud} still not downloaded)", flush=True)
            return done
        except Exception as e:
            print(f"  [multi-core unavailable ({type(e).__name__}: {e}); continuing on a single core]\n", flush=True)
    for k,it in enumerate(targets,1):
        pid,status=render_one(it); apply(pid,status)
        if k%5==0 or k==total: report(k)
        if checkpoint and done and done%100==0: checkpoint()
    print(f"\n  finished: {done}/{total} rendered in {time.time()-t0:.0f}s "
          f"({cloud} still not downloaded)", flush=True)
    return done

SOURCES=os.path.join(LIB,"sources.txt")

def load_sources():
    if not os.path.exists(SOURCES): return []
    out=[]
    for line in open(SOURCES,encoding="utf-8"):
        s=line.strip()
        if s and not s.startswith("#"): out.append(s.rstrip("\\/"))
    return out

def save_sources(paths, header=True):
    seen=[];
    for p in paths:
        p=p.strip().rstrip("\\/")
        if p and p.lower() not in [x.lower() for x in seen]: seen.append(p)
    with open(SOURCES,"w",encoding="utf-8") as fp:
        if header:
            fp.write("# Folders this library scans.\n"
                     "# One folder per line. Lines starting with # are ignored.\n"
                     "# Edit here, or use LIBRARY.bat menu options S / A / D.\n"
                     "# NOTE: never put a whole cloud root here (e.g. D:\\Dropbox) - list the\n"
                     "# specific subfolders you actually want indexed.\n\n")
        for p in seen: fp.write(p+"\n")
    return seen

def show_sources(by_id=None):
    src=load_sources()
    print(f"\nScanned folders  ({SOURCES}):\n")
    if not src:
        print("  (none listed)")
    counts={}
    if by_id:
        for it in by_id.values():
            p=(it.get("path") or "").lower()
            for s in src:
                if p.startswith(s.lower()+"\\") or p==s.lower():
                    counts[s]=counts.get(s,0)+1; break
    for s in src:
        mark="OK     " if os.path.isdir(s) else "MISSING"
        extra=f"   {counts.get(s,0):,} items" if by_id else ""
        print(f"  [{mark}] {s}{extra}")
    if by_id:
        known=sum(counts.values()); total=len(by_id)
        if total-known>0:
            print(f"\n  {total-known:,} catalog items are outside these folders "
                  f"(added ad-hoc, or the folder was removed from this list).")
    print("\n  add:     python update_catalog.py --add-source \"D:\\path\\to\\folder\"")
    print("  remove:  python update_catalog.py --remove-source \"D:\\path\\to\\folder\"")
    print("  rescan:  python update_catalog.py --rescan-all\n")

def _fingerprint(it):
    """Content identity that survives a MOVE: the set of model filenames + how many
    + total bytes. Pure metadata — no file contents read, no Dropbox download.
    Two folders with the same fingerprint are the same kit in a different place."""
    import hashlib
    names=sorted((n or "").strip().lower() for n in (it.get("model_files") or []))
    if not names: return None
    sig="|".join(names)+f"::{len(names)}::{int(it.get('size_bytes') or 0)}"
    return hashlib.md5(sig.encode("utf-8","ignore")).hexdigest()[:16]

def _primary_file(it):
    """The filename to paste into Windows search when hunting for this item."""
    mfs=it.get("model_files") or []
    if not mfs: return os.path.basename(it.get("path",""))
    best=None;bestsz=-1
    for f in mfs:                       # prefer the largest part if we can see sizes
        try: sz=os.path.getsize(os.path.join(it["path"],f))
        except OSError: sz=-1
        if sz>bestsz: bestsz=sz; best=f
    return best or mfs[0]

def relocate_and_prune(by_id, prune=False):
    """Detect entries whose folder moved: match a missing old path to a present new
    path by fingerprint, then update in place (same id, same thumbnail, same tags)."""
    missing=[]; present={}
    for it in by_id.values():
        if it.get("type")!="project": continue
        fp=_fingerprint(it)
        if not fp: continue
        if os.path.isdir(it.get("path","")): present.setdefault(fp,[]).append(it)
        else: missing.append((fp,it))
    moved=0; dropped=0; merged=[]
    for fp,old in missing:
        cands=[c for c in present.get(fp,[]) if c is not old]
        if len(cands)==1:
            new=cands[0]
            # keep the ORIGINAL id/thumbnail; adopt the new location
            old["path"]=new["path"]
            old["model_files"]=new.get("model_files",old.get("model_files"))
            old["preview_file"]=new.get("preview_file")
            old["thumb_status"]=old.get("thumb_status","pending")
            merged.append(new["id"]); moved+=1
    for mid in merged:                      # remove the duplicate new-id record
        by_id.pop(mid,None)
    if prune:
        for k in [k for k,v in by_id.items()
                  if v.get("type")=="project" and not os.path.isdir(v.get("path",""))]:
            by_id.pop(k,None); dropped+=1
        for k in [k for k,v in by_id.items()
                  if v.get("type")=="archive" and not os.path.isfile(v.get("path",""))]:
            by_id.pop(k,None); dropped+=1
    return moved, dropped

def write_catalog(by_id):
    items=list(by_id.values())
    cat={"generated":"updated","schema":"3dprintlibrary-1",
         "counts":{"total":len(items),
                   "projects":sum(1 for i in items if i["type"]=="project"),
                   "archives":sum(1 for i in items if i["type"]=="archive"),
                   "thumbnails_rendered":sum(1 for i in items if i.get("thumbnail"))},
         "items":items}
    json.dump(cat, open(CATALOG,"w"), indent=1)
    cols=["id","type","name","display_name","collection","breadcrumb","primary_file","all_files","fingerprint","category","faction","source","packed","part_count","formats","size_bytes","size_mb","path","thumbnail","thumb_status","tags"]
    with open(CSVFILE,"w",newline="",encoding="utf-8") as fp:
        w=csv.writer(fp); w.writerow(cols)
        for it in items:
            dn,col,bc=_display_fields(it["path"])
            mfs=it.get("model_files") or []
            pf=(mfs[0] if mfs else os.path.basename(it["path"]))
            w.writerow([it["id"],it["type"],it["name"],dn,col,bc,pf,"|".join(mfs[:25]),
                        _fingerprint(it) or "",it["category"],it.get("faction") or "",
                        it["source"],it["packed"],it.get("part_count") if it.get("part_count") is not None else "",
                        "|".join(it.get("formats",[])),it["size_bytes"],round(it["size_bytes"]/1e6,2),
                        it["path"],it.get("thumbnail") or "",it.get("thumb_status",""),"|".join(it.get("tags",[]))])

_FALLBACK_GENERIC={"files","file","parts","part","stl","stls","meshes","mesh","obj","output",
 "export","supported","presupported","unsupported","supports","print","prints","split","cut",
 "assembled","whole","body","bodies","misc","new folder","untitled","resin","fdm"}

def _generic_folders():
    """Folder names too generic to use as a display name (from rules.json)."""
    g=_compiled()["generic"]
    return g if g else _FALLBACK_GENERIC

def _source_root_index(segs):
    """Index of the scanned-folder root inside a split path, or -1.
    Derived from sources.txt, so it adapts to whatever anyone indexes."""
    lowered=[s.strip().lower() for s in segs]
    best=-1; best_len=-1
    for root in load_sources():
        rsegs=[s for s in root.replace("/","\\").split("\\") if s]
        if not rsegs: continue
        tail=rsegs[-1].strip().lower()
        for k,s in enumerate(lowered):
            if s==tail and len(rsegs)>best_len:
                best=k; best_len=len(rsegs)
    return best

def _display_fields(path):
    """Return (display_name, collection, breadcrumb) from a full path — presentation only.
    display_name  = deepest folder that isn't a generic container like 'files'
    collection    = first meaningful folder under the scanned root
    breadcrumb    = up to 3 folders of context above the item"""
    segs=[s for s in str(path).replace("/","\\").split("\\") if s]
    if not segs: return (str(path),"(none)","")
    gen=_generic_folders()
    i=len(segs)-1
    while i>0 and segs[i].strip().lower() in gen: i-=1
    display=segs[i]
    ri=_source_root_index(segs)
    col=""
    if ri>=0:
        j=ri+1
        # skip over generic containers to find the real collection name
        while j<len(segs)-1 and segs[j].strip().lower() in gen: j+=1
        if j<len(segs): col=segs[j]
    start=ri+1 if ri>=0 else max(1,len(segs)-3)
    crumb=segs[start:len(segs)-1]
    return (display, col or "(none)", " › ".join(crumb[-3:]))

def write_gallery(by_id):
    slim=[]
    for it in by_id.values():
        dn,col,bc=_display_fields(it["path"])
        mfs=it.get("model_files") or []
        pf=(mfs[0] if mfs else os.path.basename(it["path"]))
        slim.append({"n":dn,"c":it["category"],"f":it.get("faction") or "","s":it["source"],
            "col":col,"bc":bc,"p":1 if it.get("packed") else 0,"pc":it.get("part_count") or 0,
            "mb":round(it["size_bytes"]/1e6,1),"t":it.get("thumbnail") or "","path":it["path"],
            "pf":pf,"nf":len(mfs),
            "tags":[x for x in it.get("tags",[]) if x.startswith("type:")]})
    data=json.dumps(slim,separators=(",",":"))
    html='''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>3D Print Library</title>
<style>
:root{--bg:#14161b;--card:#1e2129;--mut:#8a90a0;--fg:#e7e9ee;--acc:#6ea8fe;--line:#2a2e39}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;background:#111318ee;backdrop-filter:blur(6px);border-bottom:1px solid var(--line);padding:12px 16px;z-index:10}
h1{margin:0 0 8px;font-size:17px}
.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input,select{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:7px;padding:7px 9px;font-size:13px}
input#q{flex:1;min-width:180px}.stat{color:var(--mut);font-size:12px;margin-left:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;padding:16px}
.ghead{grid-column:1/-1;font-size:14px;font-weight:700;color:var(--acc);border-bottom:1px solid var(--line);padding:14px 2px 6px;margin-top:6px}
.ghead .gc{color:var(--mut);font-weight:400;font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
.thumb{aspect-ratio:1;background:#0e1014;display:flex;align-items:center;justify-content:center;position:relative}
.thumb img{width:100%;height:100%;object-fit:cover}
.ph{color:#464c5c;font-size:11px;text-align:center;padding:8px}
.badge{position:absolute;top:6px;right:6px;background:#000000aa;color:#cfd3dd;font-size:10px;padding:2px 6px;border-radius:20px}
.body{padding:8px 9px}.nm{font-size:12.5px;font-weight:600;line-height:1.2;max-height:2.4em;overflow:hidden}
.crumb{color:var(--mut);font-size:10.5px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta{color:var(--mut);font-size:11px;margin-top:4px;display:flex;flex-wrap:wrap;gap:4px}
.tag{background:#262b36;border-radius:5px;padding:1px 5px}.fac{color:var(--acc)}
.find{display:flex;gap:4px;margin-top:6px}
.find button{flex:1;background:#2b3140;color:#cfd3dd;border:1px solid #39404f;border-radius:6px;
 padding:4px 6px;font-size:10.5px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.find button:hover{background:#38405266;border-color:var(--acc);color:#fff}
.find button.ok{background:#1f7a4d;border-color:#1f7a4d;color:#fff}
.more{padding:14px;text-align:center}
button.more-btn{background:var(--acc);color:#08122a;border:0;border-radius:8px;padding:9px 18px;font-weight:600;cursor:pointer}
.card{cursor:zoom-in}
#lb{position:fixed;inset:0;background:#0a0c10ee;backdrop-filter:blur(3px);z-index:50;
 display:none;align-items:center;justify-content:center;padding:24px}
#lb.on{display:flex}
#lbin{background:var(--card);border:1px solid var(--line);border-radius:14px;max-width:1000px;
 width:100%;max-height:92vh;overflow:auto;display:flex;gap:18px;padding:18px}
#lbimg{width:min(52vw,520px);aspect-ratio:1;object-fit:contain;background:#0e1014;border-radius:10px;flex:none}
#lbside{flex:1;min-width:240px}
#lbside h3{margin:2px 0 4px;font-size:19px;line-height:1.25}
#lbside .p{color:var(--mut);font-size:12px;word-break:break-all;margin-bottom:12px}
#lbside table{width:100%;border-collapse:collapse;font-size:12.5px;margin-bottom:12px}
#lbside td{padding:4px 0;border-bottom:1px solid var(--line);vertical-align:top}
#lbside td:first-child{color:var(--mut);width:92px}
#lbclose{position:absolute;top:16px;right:20px;background:#262b36;color:#e7e9ee;border:1px solid var(--line);
 border-radius:8px;padding:6px 12px;cursor:pointer;font-size:13px}
#lb .find{margin-top:10px}#lb .find button{padding:7px 10px;font-size:12px}
</style></head><body>
<header><h1>3D Print Library <span class="stat" id="cnt"></span></h1>
<div class="controls">
<input id="q" placeholder="Search name, folder or path...">
<select id="cat"><option value="">All categories</option></select>
<select id="fac"><option value="">All factions</option></select>
<select id="src"><option value="">All sources</option></select>
<select id="col"><option value="">All collections</option></select>
<select id="pk"><option value="">Packed + Extracted</option><option value="0">Extracted only</option><option value="1">Packed (zip) only</option></select>
<select id="th"><option value="">Any thumbnail</option><option value="1">Has thumbnail</option><option value="0">No thumbnail</option></select>
<select id="gb"><option value="">Group: none</option><option value="col">Group by collection</option><option value="f">Group by faction</option><option value="c">Group by category</option><option value="s">Group by source</option></select>
</div></header>
<div class="grid" id="grid"></div>
<div class="more" id="morewrap"><button class="more-btn" id="more">Load more</button></div>
<div id="lb"><button id="lbclose">Close (Esc)</button><div id="lbin">
  <img id="lbimg"><div id="lbside">
    <h3 id="lbname"></h3><div class="p" id="lbpath"></div>
    <table id="lbtab"></table>
    <div class="find">
      <button id="lbcf">copy filename</button>
      <button id="lbcp">copy path</button>
    </div>
  </div>
</div></div>
<script>
const DATA=''' + data + ''';
const $=s=>document.querySelector(s);
function opts(sel,vals){vals=[...new Set(vals)].filter(Boolean).sort();for(const v of vals){const o=document.createElement("option");o.value=v;o.textContent=v;sel.appendChild(o)}}
opts($("#cat"),DATA.map(d=>d.c));
opts($("#fac"),DATA.map(d=>d.f));
opts($("#src"),DATA.map(d=>d.s));
opts($("#col"),DATA.map(d=>d.col));
let filtered=[],shown=0,PAGE=120,lastG=null,gkey="";
const GLABEL={col:"collection",f:"faction",c:"category",s:"source"};
function apply(){
 const q=$("#q").value.toLowerCase(),c=$("#cat").value,f=$("#fac").value,s=$("#src").value,
   col=$("#col").value,pk=$("#pk").value,th=$("#th").value; gkey=$("#gb").value;
 filtered=DATA.filter(d=>{
   if(c&&d.c!==c)return false; if(f&&d.f!==f)return false; if(s&&d.s!==s)return false;
   if(col&&d.col!==col)return false; if(pk!==""&&String(d.p)!==pk)return false;
   if(th==="1"&&!d.t)return false; if(th==="0"&&d.t)return false;
   if(q&&!(d.n.toLowerCase().includes(q)||d.path.toLowerCase().includes(q)||(d.col||"").toLowerCase().includes(q)))return false;
   return true;});
 if(gkey)filtered.sort((a,b)=>String(a[gkey]).localeCompare(String(b[gkey]))||a.n.localeCompare(b.n));
 shown=0;lastG=null;$("#grid").innerHTML="";render();
 $("#cnt").textContent=filtered.length.toLocaleString()+" of "+DATA.length.toLocaleString()+" items";}
function render(){
 const g=$("#grid"),end=Math.min(shown+PAGE,filtered.length);
 for(let i=shown;i<end;i++){const d=filtered[i];
   if(gkey){const gv=d[gkey]||"(none)";if(gv!==lastG){lastG=gv;const h=document.createElement("div");h.className="ghead";
     h.innerHTML=`${gv} <span class=gc>· ${GLABEL[gkey]}</span>`;g.appendChild(h);}}
   const c=document.createElement("div");c.className="card";c.dataset.i=i;
   const thumb=d.t?`<img loading="lazy" src="thumbnails/${d.t}">`:`<div class="ph">${d.p?'\\u{1F4E6} packed .zip':'no thumbnail yet'}</div>`;
   const fac=d.f?`<span class="tag fac">${d.f}</span>`:"";
   const types=(d.tags||[]).map(t=>`<span class="tag">${t.replace('type:','')}</span>`).join("");
   const crumb=d.bc?`<div class="crumb" title="${d.path}">${d.col&&d.col!=="(none)"?d.col+" › ":""}${d.bc}</div>`:`<div class="crumb" title="${d.path}">${d.col||""}</div>`;
   c.innerHTML=`<div class="thumb">${thumb}<span class="badge">${d.p?'zip':d.pc+'p'}</span></div>
   <div class="body"><div class="nm" title="${d.path}">${d.n}</div>${crumb}
   <div class="meta"><span class="tag">${d.c}</span>${fac}<span class="tag">${d.s}</span>${types}<span class="tag">${d.mb}MB</span></div>
   <div class="find">
     <button data-cp="${encodeURIComponent(d.pf)}" title="Copy exact filename — paste into Windows/Everything search:&#10;${d.pf}">copy filename</button>
     <button data-cp="${encodeURIComponent(d.path)}" title="Copy full folder path — paste into Explorer's address bar:&#10;${d.path}">copy path</button>
   </div></div>`;
   g.appendChild(c);}
 shown=end;$("#morewrap").style.display=shown<filtered.length?"block":"none";}
$("#more").onclick=render;
for(const el of ["q","cat","fac","src","col","pk","th","gb"])$("#"+el).addEventListener("input",apply);
// copy-to-clipboard (works from file:// via textarea fallback)
function copyText(txt,b){
 const old=b.textContent;
 const done=()=>{b.textContent="copied";b.classList.add("ok");
   setTimeout(()=>{b.textContent=old;b.classList.remove("ok")},1100);};
 const fb=()=>{const t=document.createElement("textarea");t.value=txt;t.style.position="fixed";
   t.style.opacity=0;document.body.appendChild(t);t.select();
   try{document.execCommand("copy");done();}catch(_){prompt("Copy:",txt);}
   document.body.removeChild(t);};
 if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(txt).then(done,fb);}
 else fb();
}
document.addEventListener("click",e=>{
 const b=e.target.closest("button[data-cp]"); if(!b)return;
 copyText(decodeURIComponent(b.dataset.cp),b);
});
// ---- click a card to enlarge ----
let lbCur=null;
function openLB(d){
 lbCur=d;
 $("#lbimg").src=d.t?("thumbnails/"+d.t):"";
 $("#lbimg").style.visibility=d.t?"visible":"hidden";
 $("#lbname").textContent=d.n;
 $("#lbpath").textContent=d.path;
 const rows=[["Folder",d.col&&d.col!=="(none)"?d.col:"—"],["Location",d.bc||"—"],
   ["Category",d.c],["Faction",d.f||"—"],["Source",d.s],
   ["Kind",d.p?"packed .zip (not extracted)":"extracted files"],
   ["Parts",d.p?"—":d.pc],["Size",d.mb+" MB"],["Main file",d.pf||"—"],
   ["Labels",(d.tags||[]).map(t=>t.replace("type:","")).join(", ")||"—"]];
 $("#lbtab").innerHTML=rows.map(r=>`<tr><td>${r[0]}</td><td>${r[1]}</td></tr>`).join("");
 $("#lb").classList.add("on");
}
function closeLB(){$("#lb").classList.remove("on");lbCur=null;}
$("#grid").addEventListener("click",e=>{
 if(e.target.closest("button[data-cp]"))return;      // let copy buttons work
 const card=e.target.closest(".card"); if(!card)return;
 const d=filtered[+card.dataset.i]; if(d)openLB(d);
});
$("#lbclose").onclick=closeLB;
$("#lb").addEventListener("click",e=>{if(e.target.id==="lb")closeLB();});
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeLB();});
$("#lbcf").onclick=()=>{if(lbCur)copyText(lbCur.pf,$("#lbcf"));};
$("#lbcp").onclick=()=>{if(lbCur)copyText(lbCur.path,$("#lbcp"));};
apply();
</script></body></html>'''
    open(os.path.join(LIB,"gallery.html"),"w",encoding="utf-8").write(html)

def write_import(by_id):
    import base64, urllib.parse
    def file_uri(w): return "file:///"+urllib.parse.quote(w.replace("\\","/"), safe="/:")
    models=[]; images=[]; seen=set()
    for it in by_id.values():
        mid=it["id"]
        while mid in seen:
            mid=mid+"x"
        seen.add(mid)
        dn,col,bc=_display_fields(it["path"])
        tags=list(it.get("tags",[]))
        if col and col!="(none)": tags.append("collection:"+col)
        m={"id":mid,"name":dn,"tags":tags,
           "notes":f"{it['category']} | source={it['source']} | collection={col} | "
                   f"{'PACKED zip' if it.get('packed') else str(it.get('part_count') or 0)+' parts'} | {it['path']}",
           "sources":[{"label":"Local file","url":file_uri(it["path"])}]}
        thumb=it.get("thumbnail")
        if thumb:
            tp=os.path.join(THUMBS,thumb)
            if os.path.exists(tp):
                imgid="img-"+mid
                images.append({"id":imgid,"modelId":mid,"data":base64.b64encode(open(tp,"rb").read()).decode()})
                m["imageId"]=imgid
        models.append(m)
    backup={"schema":2,"exportedAt":"updated","models":models,"images":images,"spools":[],"jobs":[]}
    json.dump(backup, open(os.path.join(LIB,"print-tracker-import.json"),"w"))

# Windows cloud-file attribute flags — detect "online-only" WITHOUT opening the
# file (opening an online-only file would force Dropbox to download it).
_OFFLINE=0x1000; _RECALL_OPEN=0x00040000; _RECALL_DATA=0x00400000
def _hydrated(fp):
    try:
        a=getattr(os.stat(fp),"st_file_attributes",0)
    except OSError:
        return None  # file missing entirely
    if a & (_OFFLINE|_RECALL_OPEN|_RECALL_DATA):
        return False  # cloud-only placeholder, not on disk yet
    return True

def compare_engines(items, n=6, max_mb=60):
    """Render the SAME small models with BOTH engines, side by side. Fast on purpose:
    only small projects, so you can judge quality without waiting on giant meshes."""
    import time
    outdir=os.path.join(LIB,"_engine_test"); os.makedirs(outdir,exist_ok=True)
    cand=[it for it in items
          if it.get("type")=="project" and it.get("model_files")
          and 0 < it.get("size_bytes",0) <= max_mb*1e6
          and _hydrated(os.path.join(it["path"], it["model_files"][0])) is True]
    if not cand:
        print(f"No downloaded projects under {max_mb} MB found. Try --compare-engines {n} --max-mb 200")
        return
    cand.sort(key=lambda it: it.get("size_bytes",0))
    # spread across the SMALL range only (fast), skipping the tiniest degenerate ones
    step=max(1,len(cand)//max(n,1))
    picked=cand[::step][:n]
    print(f"comparing engines on {len(picked)} small projects (<= {max_mb:.0f} MB each) -> {outdir}")
    print("nothing else is touched; safe to Ctrl+C\n", flush=True)
    rows=[]
    for i,it in enumerate(picked,1):
        dn,_c,bc=_display_fields(it["path"]); mb=it.get("size_bytes",0)/1e6
        print(f"  [{i}/{len(picked)}] {dn}  ({it.get('part_count') or '?'} parts, {mb:.1f} MB)", flush=True)
        res={}
        for eng in ("shell","mesh"):
            p=os.path.join(outdir, f"{it['id']}_{eng}.webp")
            ts=time.time()
            if eng=="shell":
                best=None;bestsz=-1
                for f in it["model_files"]:
                    fp=os.path.join(it["path"],f)
                    if _hydrated(fp) is not True: continue
                    try: sz=os.path.getsize(fp)
                    except OSError: continue
                    if sz>bestsz: bestsz=sz; best=fp
                ok=bool(best and _shell_thumb(best,p))
            else:
                ok=bool(_render([os.path.join(it["path"],f) for f in it["model_files"]], p))
            dt=time.time()-ts
            res[eng]=(ok,dt,os.path.basename(p) if ok else None)
            print(f"        {eng:5} {'ok  ' if ok else 'FAIL'} {dt:5.1f}s", flush=True)
        rows.append((it,dn,bc,mb,res))
    cells=[]
    for it,dn,bc,mb,res in rows:
        def img(eng):
            ok,dt,fn=res[eng]
            return (f'<img src="{fn}">' if ok else '<div class=ph>failed</div>')+f'<div class=t>{eng} · {res[eng][1]:.1f}s</div>'
        cells.append('<div class=card>'
                     f'<div class=nm>{dn}</div><div class=bc>{bc} · {mb:.1f} MB · {it.get("part_count") or "?"} parts</div>'
                     f'<div class=row><div>{img("shell")}</div><div>{img("mesh")}</div></div></div>')
    html=("<!doctype html><meta charset=utf-8><title>Engine comparison</title><style>"
          "body{background:#14161b;color:#e7e9ee;font:14px system-ui;padding:18px}"
          "h2{margin:0 0 4px}p.sub{color:#8a90a0;margin:0 0 16px}"
          ".card{background:#1e2129;border:1px solid #2a2e39;border-radius:10px;padding:12px;"
          "margin:0 10px 14px 0;display:inline-block;vertical-align:top;width:360px}"
          ".row{display:flex;gap:10px}.row>div{flex:1;text-align:center}"
          ".row img{width:100%;aspect-ratio:1;object-fit:cover;background:#0e1014;border-radius:8px}"
          ".t{font-size:11px;color:#8a90a0;margin-top:4px}"
          ".nm{font-weight:700;font-size:13px}.bc{color:#8a90a0;font-size:11px;margin:2px 0 8px}"
          ".ph{aspect-ratio:1;background:#0e1014;border-radius:8px;color:#464c5c;display:flex;"
          "align-items:center;justify-content:center;font-size:12px}</style>"
          f"<h2>Engine comparison — {len(rows)} small models</h2>"
          "<p class=sub>left = <b>shell</b> (Windows handler, like Explorer) &nbsp;·&nbsp; "
          "right = <b>mesh</b> (Python renderer)</p>"+"".join(cells))
    open(os.path.join(outdir,"compare.html"),"w",encoding="utf-8").write(html)
    print(f"\nOpen:  {os.path.join(outdir,'compare.html')}")
    print("Then run the full pass with whichever you prefer:")
    print("  python update_catalog.py --thumbs-only --force --engine shell")
    print("  python update_catalog.py --thumbs-only --force --engine mesh")

def sample_render(items, n, engine="shell"):
    outdir=os.path.join(LIB,"_render_test"); os.makedirs(outdir,exist_ok=True)
    cand=[it for it in items if it.get("type")=="project" and it.get("model_files")]
    cand.sort(key=lambda it:-it.get("size_bytes",0))
    hyd=[it for it in cand if _hydrated(os.path.join(it["path"], it["model_files"][0])) is True]
    # Spread the sample across the size range (biggest -> smallest) so it's fast AND
    # representative, instead of only the slowest giant meshes.
    if len(hyd)<=n:
        picked=hyd
    else:
        idxs=sorted({round(i*(len(hyd)-1)/max(n-1,1)) for i in range(n)})
        picked=[hyd[j] for j in idxs]
    # show smallest (fastest) first so feedback is immediate; still spans the range
    picked=sorted(picked, key=lambda it: it.get("size_bytes",0))
    if not picked:
        print("No hydrated project files available to sample yet (nothing downloaded?). "
              "Run:  python update_catalog.py --diagnose"); return
    import time
    print(f"rendering {len(picked)} sample thumbnails into {outdir} with engine='{engine}' (nothing else is touched).")
    print("Sizes are spread small->large. Safe to press Ctrl+C anytime — only the sample folder is written.\n", flush=True)
    rows=[]; times=[]
    for i,it in enumerate(picked,1):
        newp=os.path.join(outdir, it["id"]+".webp")
        old_exists=os.path.exists(os.path.join(THUMBS, it["id"]+".webp"))
        mb=it.get("size_bytes",0)/1e6
        dn,_col,_bc=_display_fields(it["path"])
        print(f"  [{i}/{len(picked)}] {dn}  ({it.get('part_count') or '?'} parts, {mb:.0f} MB)...", flush=True)
        ts=time.time()
        status=_thumb_for(it, newp, engine); okr=status in ("shell","mesh","reused")
        dt=time.time()-ts; times.append(dt); avg=sum(times)/len(times); left=(len(picked)-i)*avg
        print(f"        {status} in {dt:.1f}s   (avg {avg:.1f}s/file, ~{left:.0f}s left)", flush=True)
        rows.append((it, it["id"]+".webp", old_exists))
    cells=[]
    for it,newf,has_old in rows:
        before=(f'<img src="../thumbnails/{it["id"]}.webp">' if has_old else '<div class=ph>no prior</div>')
        dn,_c,bc=_display_fields(it["path"])
        cells.append(f'<div class=card><div class=nm>{dn}</div>'
                     f'<div style="font-size:10.5px;color:#8a90a0;margin:-4px 0 6px">{bc}</div>'
                     f'<div class=row><div><div class=lbl>before</div>{before}</div>'
                     f'<div><div class=lbl>after</div><img src="{newf}"></div></div></div>')
    html=("<!doctype html><meta charset=utf-8><style>"
          "body{background:#14161b;color:#e7e9ee;font:14px system-ui;padding:16px}"
          ".card{background:#1e2129;border:1px solid #2a2e39;border-radius:10px;padding:10px;margin:0 8px 12px 0;"
          "display:inline-block;vertical-align:top;width:340px}.row{display:flex;gap:8px}"
          ".row img{width:150px;height:150px;object-fit:cover;background:#0e1014;border-radius:6px}"
          ".lbl{font-size:11px;color:#8a90a0}.nm{font-weight:600;margin-bottom:6px;font-size:12px}"
          ".ph{width:150px;height:150px;background:#0e1014;border-radius:6px;color:#464c5c;"
          "display:flex;align-items:center;justify-content:center;font-size:11px}</style>"
          f"<h2>Render test — {len(rows)} samples (new renderer)</h2>"+"".join(cells))
    open(os.path.join(outdir,"preview.html"),"w",encoding="utf-8").write(html)
    print(f"\nDone. Open this to review:\n  {os.path.join(outdir,'preview.html')}")
    print("Nothing else was changed. If it looks good, apply to all rendered thumbnails with:")
    print("  python update_catalog.py --thumbs-only --force")

def diagnose(items):
    deps={}
    for mod in ("numpy","trimesh","matplotlib","PIL"):
        try: __import__(mod); deps[mod]="OK"
        except Exception as e: deps[mod]="MISSING ("+type(e).__name__+")"
    print("\nRender libraries:")
    for k,v in deps.items(): print(f"  {k:11} {v}")
    libs_ok=all(v=="OK" for v in deps.values())
    # fast shell-thumbnail engine readiness
    shell_ok=True
    for mod in ("pythoncom","win32com.shell"):
        try: __import__(mod)
        except Exception as e: shell_ok=False; print(f"  {mod:11} MISSING ({type(e).__name__})")
    print(f"\nThumbnail engine: {'shell (fast, like Explorer) READY' if shell_ok else 'shell NOT available -> falls back to mesh (slow)'}")
    if not shell_ok:
        print("  For fast Explorer-style thumbnails:  pip install pywin32")
    projects=[it for it in items if it["type"]=="project"]
    have=sum(1 for it in projects if it.get("thumbnail") and os.path.exists(os.path.join(THUMBS,it["thumbnail"])))
    pending=[it for it in projects if not os.path.exists(os.path.join(THUMBS,it["id"]+".webp"))]
    hyd=cloud=missing=nofile=0; sample=[]
    for it in pending:
        mfs=it.get("model_files") or []
        if not mfs: nofile+=1; continue
        st=_hydrated(os.path.join(it["path"],mfs[0]))
        if st is True: hyd+=1
        elif st is False:
            cloud+=1
            if len(sample)<3: sample.append(it["path"])
        else: missing+=1
    print(f"\nProjects: {len(projects)}   with thumbnail: {have}   pending: {len(pending)}")
    print(f"  pending files hydrated on disk (ready to render): {hyd}")
    print(f"  pending files still cloud-only (not downloaded):  {cloud}")
    if missing: print(f"  pending files not found at path:                 {missing}")
    if nofile: print(f"  pending with no model file listed:               {nofile}")
    if sample:
        print("  examples still in the cloud:")
        for p in sample: print("    ",p)
    print("")
    if not libs_ok:
        print("=> ACTION: install the render libraries, then run this again:")
        print("   pip install -r requirements.txt")
    elif not load_sources():
        print("=> NOTHING ADDED YET. Point the library at a folder of models:")
        print('   python update_catalog.py "D:\\path\\to\\your\\models"')
        print("   (or use LIBRARY.bat option 8)")
    elif not projects:
        print("=> Folders are listed but nothing was found in them. Check the paths:")
        print("   python update_catalog.py --sources")
    elif hyd>0:
        print(f"=> READY: {hyd} model file(s) are on disk. Run:")
        print("   python update_catalog.py --thumbs-only")
    elif cloud>0:
        print(f"=> WAITING ON YOUR CLOUD SYNC: {cloud} item(s) are online-only placeholders,")
        print("   so their contents can't be read yet. In Dropbox/OneDrive/etc, mark those")
        print("   folders 'Always keep on this device' and wait for them to finish")
        print("   downloading, then run:  python update_catalog.py --thumbs-only")
    else:
        print("=> Everything that can be rendered already has a thumbnail.")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("folders",nargs="*")
    ap.add_argument("--rescan-all",action="store_true")
    ap.add_argument("--thumbs-only",action="store_true")
    ap.add_argument("--diagnose",action="store_true")
    ap.add_argument("--force",action="store_true",help="re-render thumbnails even if they already exist (hydrated files only)")
    ap.add_argument("--sample",type=int,default=0,metavar="N",help="render N sample thumbnails into _render_test/ for review; changes nothing else")
    ap.add_argument("--jobs",type=int,default=0,metavar="N",help="parallel render workers (default: auto = ~half your CPU cores; use 1 to force single core)")
    ap.add_argument("--engine",choices=["shell","mesh"],default="shell",help="thumbnail engine: 'shell' = fast Windows handler like Explorer (default), 'mesh' = slow Python renderer")
    ap.add_argument("--rebuild-views",action="store_true",help="regenerate catalog.csv/json, gallery.html and the import file from existing data — no rendering")
    ap.add_argument("--max-mb",type=float,default=1500,metavar="MB",help="skip projects larger than this (default 1500). Use a huge number to include everything.")
    ap.add_argument("--compare-engines",type=int,default=0,metavar="N",help="render N SMALL models with BOTH engines side by side into _engine_test/ (fast; changes nothing else)")
    ap.add_argument("--prune",action="store_true",help="remove catalog entries whose files no longer exist (after move-detection runs)")
    ap.add_argument("--relocate",action="store_true",help="re-check every entry's path and fix ones that moved (no scan, no rendering)")
    ap.add_argument("--sources",action="store_true",help="list the folders this library scans")
    ap.add_argument("--add-source",metavar="PATH",help="add a folder to the scan list")
    ap.add_argument("--remove-source",metavar="PATH",help="remove a folder from the scan list (does not delete files or entries)")
    a=ap.parse_args()
    jobs = a.jobs if a.jobs>0 else min(6, max(1,(os.cpu_count() or 2)-1))
    by_id={}
    if os.path.exists(CATALOG):
        for it in json.load(open(CATALOG))["items"]: by_id[it["id"]]=it
    print(f"loaded {len(by_id)} existing items")
    if a.diagnose:
        diagnose(list(by_id.values())); return
    if a.sources:
        show_sources(by_id); return
    if a.add_source:
        p=a.add_source.strip().rstrip("\\/")
        if not os.path.isdir(p):
            print(f"Not a folder (nothing added): {p}"); return
        cur=load_sources()
        if p.lower() in [c.lower() for c in cur]:
            print(f"Already in the list: {p}")
        else:
            save_sources(cur+[p]); print(f"Added: {p}")
            print("Now scan it:  python update_catalog.py \"%s\"" % p)
        show_sources(by_id); return
    if a.remove_source:
        p=a.remove_source.strip().rstrip("\\/")
        cur=load_sources(); new=[c for c in cur if c.lower()!=p.lower()]
        if len(new)==len(cur): print(f"Not in the list: {p}")
        else:
            save_sources(new)
            print(f"Removed from scan list: {p}")
            print("Existing catalog entries were kept. To drop them too, use option 9 / --relocate --prune")
        show_sources(by_id); return
    if a.relocate or (a.prune and not a.folders and not a.rescan_all and not a.thumbs_only):
        moved,dropped=relocate_and_prune(by_id, prune=a.prune)
        write_catalog(by_id); write_gallery(by_id); write_import(by_id)
        print(f"relocated {moved} moved project(s); pruned {dropped}; views rebuilt.")
        if not moved and not dropped:
            print("nothing moved that I could match. If a folder moved, scan its NEW location first:")
            print('   python update_catalog.py "D:\\path\\to\\new\\location"')
        return
    if getattr(a,"rebuild_views",False):
        write_catalog(by_id); write_gallery(by_id); write_import(by_id)
        print("rebuilt catalog.csv/json, gallery.html and print-tracker-import.json (no rendering)."); return
    if a.compare_engines:
        compare_engines(list(by_id.values()), a.compare_engines,
                        max_mb=(a.max_mb if a.max_mb<1500 else 60)); return
    if a.sample:
        sample_render(list(by_id.values()), a.sample, a.engine); return
    if a.thumbs_only:
        def _ckpt():
            write_catalog(by_id); write_gallery(by_id)   # gallery is browsable mid-run
        n=render_missing(list(by_id.values()), checkpoint=_ckpt, force=a.force, jobs=jobs, engine=a.engine, max_mb=a.max_mb)
        write_catalog(by_id)
        print("refreshing gallery.html and print-tracker-import.json ...", flush=True)
        write_gallery(by_id); write_import(by_id)
        total=sum(1 for it in by_id.values() if it.get("thumbnail"))
        print(f"rendered {n} new thumbnails; {total} total; catalog + gallery + import updated"); return
    folders=list(a.folders)
    if a.rescan_all:
        # Use the explicit sources list. (Never derive roots from item paths —
        # that resolved to things like D:\Dropbox and scanned the whole cloud root.)
        folders=load_sources()
        if not folders:
            print(f"No folders listed in {SOURCES}. Add one:")
            print('   python update_catalog.py --add-source "D:\\path\\to\\folder"'); return
        print("rescanning the folders in sources.txt:")
        for f in folders: print("   ",f)
    # scanning a folder directly also registers it for next time
    for f in folders:
        if os.path.isdir(f) and f.rstrip("\\/").lower() not in [s.lower() for s in load_sources()]:
            save_sources(load_sources()+[f]); print(f"  (added to sources.txt: {f})")
    if not folders:
        print("""
Easiest option: double-click  LIBRARY.bat  (menu, no flags to remember).

Or use these directly:
  --diagnose                 what's downloaded, what's left, is the renderer OK
  --compare-engines 6        6 small models, both engines, side by side
  --thumbs-only              make MISSING thumbnails      (add --engine mesh)
  --thumbs-only --force      redo ALL thumbnails
  --rebuild-views            refresh gallery + csv only, no rendering (fast)
  "D:\\some\\folder"          add/rescan a folder into the library
  --relocate                 fix entries whose files moved (keeps thumbnails)
  --relocate --prune         also delete entries whose files are gone
  --sample 8                 preview 8 thumbnails into _render_test/
Useful extras:  --jobs N (cores)   --max-mb N (skip huge)   --engine shell|mesh
""".strip()); return
    added=updated=0
    for folder in folders:
        if not os.path.isdir(folder): print("  skip (not found):",folder); continue
        print("scanning:",folder)
        pj,ar=scan_folder(folder)
        for d in (pj,ar):
            for k,v in d.items():
                if k in by_id: by_id[k].update(v); updated+=1
                else: by_id[k]=v; added+=1
    print(f"merged: {added} new, {updated} updated")
    moved,dropped=relocate_and_prune(by_id, prune=a.prune)
    if moved: print(f"  relocated {moved} moved project(s) — same id, thumbnail kept, no re-render")
    if dropped: print(f"  pruned {dropped} entr(ies) whose files no longer exist")
    n=render_missing(list(by_id.values()), checkpoint=lambda: write_catalog(by_id), jobs=jobs, engine=a.engine)
    write_catalog(by_id)
    print("refreshing gallery.html and print-tracker-import.json ...", flush=True)
    write_gallery(by_id); write_import(by_id)
    print(f"rendered {n} new thumbnails; catalog now {len(by_id)} items; gallery + import updated")

if __name__=="__main__":
    main()
