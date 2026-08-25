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
import os, sys, json, csv, re, io, time, shutil, hashlib, signal, argparse, tempfile, threading

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Where the catalog, thumbnails and backups live. Defaults to the script's own
# folder, which is what everyone actually wants. LIBRARY_DIR lets the test suite -
# and anyone keeping more than one library - point it elsewhere without copying
# the script. Nothing is created at import time, so importing this module is safe.
LIB = os.path.abspath(os.environ.get("LIBRARY_DIR") or SCRIPT_DIR)
CATALOG = os.path.join(LIB, "catalog.json")
CSVFILE = os.path.join(LIB, "catalog.csv")
THUMBS  = os.path.join(LIB, "thumbnails")
BACKUPS = os.path.join(LIB, "backups")

def _ensure_dirs():
    os.makedirs(THUMBS, exist_ok=True)

MODEL={".stl",".obj",".3mf",".step",".stp",".ply"}
SLICER={".gcode",".ctb",".lys",".lychee",".cbddlp",".photon",".pwmx",".pwms",".fdg",".goo",".chitubox",".zcode"}
IMG={".png",".jpg",".jpeg",".webp",".bmp",".gif",".tif",".tiff"}
ARCH={".zip",".rar",".7z"}

def stable_id(p): return hashlib.sha1(p.strip().lower().replace("/","\\").encode("utf-8","ignore")).hexdigest()[:16]

# ---------- writing the catalog: never leave a half-written file behind ----------
def atomic_write_bytes(path, data):
    """Same guarantee as atomic_write, for binary. A thumbnail matters because
    render_missing treats the mere EXISTENCE of a .webp as "already done" — so a
    half-written one from a Ctrl+C would be accepted as finished for good."""
    d=os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=d, prefix=".tmp-", suffix="-"+os.path.basename(path))
    os.close(fd)
    try:
        with open(tmp,"wb") as fp:
            fp.write(data); fp.flush(); os.fsync(fp.fileno())
        os.replace(tmp,path)
        tmp=None
    finally:
        if tmp and os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass

def atomic_write(path, text, encoding="utf-8"):
    """Write to a temp file in the same folder, then os.replace() it into position.
    os.replace is atomic on Windows and POSIX alike, so a Ctrl+C or a crash midway
    leaves the PREVIOUS file intact instead of a truncated one. The old code wrote
    straight over catalog.json, and an interrupt during a mid-render checkpoint left
    a corrupt file that every later command died on."""
    d=os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=d, prefix=".tmp-", suffix="-"+os.path.basename(path))
    os.close(fd)
    try:
        with open(tmp,"w",encoding=encoding,newline="") as fp:
            fp.write(text); fp.flush(); os.fsync(fp.fileno())
        os.replace(tmp,path)
        tmp=None
    finally:
        if tmp and os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass

_BACKED_UP=False
def backup_catalog(keep=10, force=False):
    """Copy catalog.json into backups/ once per run, before the first write.
    The menu only backed up on some options - never on the long thumbnail runs,
    which are the ones people interrupt. Returns the backup path, or None."""
    global _BACKED_UP
    if (_BACKED_UP and not force) or not os.path.exists(CATALOG): return None
    _BACKED_UP=True
    try:
        os.makedirs(BACKUPS, exist_ok=True)
        dst=os.path.join(BACKUPS,"catalog_"+time.strftime("%Y%m%d_%H%M%S")+".json")
        if os.path.exists(dst): dst=dst[:-5]+"_%d.json"%os.getpid()
        shutil.copy2(CATALOG,dst)
        old=sorted(f for f in os.listdir(BACKUPS) if f.startswith("catalog_") and f.endswith(".json"))
        for f in old[:-keep]:
            try: os.remove(os.path.join(BACKUPS,f))
            except OSError: pass
        return dst
    except OSError as e:
        print(f"  [warning] could not back up the catalog ({e}); continuing.")
        return None

def list_backups():
    if not os.path.isdir(BACKUPS): return []
    return sorted((os.path.join(BACKUPS,f) for f in os.listdir(BACKUPS)
                   if f.startswith("catalog_") and f.endswith(".json")))

def restore_backup(which=None):
    """Put a backup back in place as catalog.json. The file being replaced is itself
    backed up first, so this is reversible. The menu's advice used to be 'copy one
    over catalog.json' by hand, which is not much help on the day it is needed."""
    b=list_backups()
    if not b:
        print(f"No backups found in {BACKUPS}."); return False
    if which:
        src=which if os.path.isabs(which) else os.path.join(BACKUPS,which)
        if not os.path.exists(src):
            print(f"Not found: {src}\nAvailable:")
            for f in b[-10:]: print("   ",os.path.basename(f))
            return False
    else:
        src=b[-1]
    if os.path.exists(CATALOG):
        keep=backup_catalog(force=True)
        if keep: print(f"kept the current catalog.json as {os.path.basename(keep)}")
    with open(src,encoding="utf-8") as fp: atomic_write(CATALOG, fp.read())
    print(f"restored {os.path.basename(src)} -> catalog.json")
    print("now refresh the views:  python update_catalog.py --rebuild-views")
    return True

def load_catalog():
    """Read catalog.json into {id: item}. A truncated or corrupt file is reported
    with the backups available to restore from, instead of a traceback."""
    if not os.path.exists(CATALOG): return {}
    try:
        with open(CATALOG,encoding="utf-8") as fp: data=json.load(fp)
        items=data["items"]
        if not isinstance(items,list): raise ValueError("'items' is not a list")
    except (ValueError,KeyError,OSError,UnicodeDecodeError) as e:
        print(f"\ncatalog.json could not be read: {e}")
        print("That usually means a previous run was interrupted while writing it.")
        b=list_backups()
        if b:
            print(f"\n{len(b)} backup(s) are available. Restore the newest with:")
            print("   python update_catalog.py --restore-backup")
            print(f"\nnewest: {os.path.basename(b[-1])}")
        else:
            print("\nNo backups were found. Rebuild from your folders with:")
            print("   python update_catalog.py --rescan-all")
        sys.exit(2)
    return {it["id"]:it for it in items if isinstance(it,dict) and it.get("id")}

# ---------- classification: all keyword rules come from rules.json ----------
RULES_FILE=os.path.join(LIB,"rules.json")
LOCAL_RULES=os.path.join(LIB,"rules.local.json")
# When LIBRARY_DIR points somewhere else, the shipped rules still come from the
# script's own folder unless that library has its own copy.
_RULE_FILES=[LOCAL_RULES, RULES_FILE, os.path.join(SCRIPT_DIR,"rules.json")]
_RULES=None
def rules():
    """Load rules.local.json if present, else rules.json. Missing/broken -> {} so
    everything still runs with neutral behaviour instead of crashing."""
    global _RULES
    if _RULES is None:
        _RULES={}
        for p in _RULE_FILES:
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
    _CACHE["match_filenames"]=bool(R.get("match_filenames",True))
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
        # The scanned folder's own name. (The first subfolder under it is already
        # surfaced separately as "collection", so repeating it here just adds noise.)
        # Split on the separator we normalised to, not the platform's: os.path.basename
        # does not recognise a backslash off Windows.
        return best.split("\\")[-1] or best
    seg=[s for s in p.split("\\") if s]
    return seg[-2] if len(seg)>1 else (seg[0] if seg else "unknown")

def _first(pairs, hay):
    for label,rx in pairs:
        if rx and rx.search(hay): return label
    return None

_FILENAME_CAP=4000
def _filename_haystack(files):
    """The part filenames as one lowercase string: stems only, de-duplicated, and
    length-capped so a kit with three hundred parts does not turn every pattern
    match into a scan of half a megabyte."""
    if not files: return ""
    seen=[]; total=0
    for f in files:
        stem=os.path.splitext(str(f))[0].lower().strip()
        if not stem or stem in seen: continue
        seen.append(stem); total+=len(stem)+1
        if total>=_FILENAME_CAP: break
    return " ".join(seen)

def classify(path, name, files=None):
    """Return (category, faction, source, tags). Everything is keyword-driven; with an
    empty rules.json every item simply lands in the default category with no faction.

    The folder path decides whenever it can. The names of the parts INSIDE are a
    fallback, consulted only for what the path left blank — so a kit in a folder
    called 'KitA' full of warhound_titan_*.stl is found, while a folder that already
    says what it is cannot be overruled by one part called wall_mount.stl. Set
    "match_filenames": false in rules.json to go back to path-only matching."""
    C=_compiled()
    s=(path+" "+name).lower()
    inner=_filename_haystack(files) if C["match_filenames"] else ""
    src=source_tag(path)
    faction=_first(C["factions"], s)
    if faction is None and inner: faction=_first(C["factions"], inner)
    war=C["wargaming"]
    is_war = faction is not None or bool(war and (war.search(s) or (inner and war.search(inner))))
    if is_war:
        cat=C["wargaming_cat"]
    else:
        cat=_first(C["categories"], s)
        if cat is None and inner: cat=_first(C["categories"], inner)
    if not cat: cat=C["default_cat"]
    tags=[]
    if faction: tags.append("faction:"+faction)
    types=[t for t,rx in C["types"] if rx and rx.search(s)]
    if not types and inner:
        types=[t for t,rx in C["types"] if rx and rx.search(inner)]
    return cat, faction, src, tags+["type:"+t for t in types]


# ---------- how a rendered thumbnail looks ----------
# Every colour and light level the mesh renderer uses. Pick one with --style, or
# override any single value under "thumbnail_style" in rules.json.
THUMB_PRESETS={
 "slate":     {"background":"#20242c","model":"#c6cddf","ambient":0.34,"specular":0.10,"rim":0.05},
 "paper":     {"background":"#eeece7","model":"#a9a396","ambient":0.48,"specular":0.08,"rim":0.03},
 "blueprint": {"background":"#0d1b2a","model":"#79aed6","ambient":0.30,"specular":0.14,"rim":0.08},
 "bronze":    {"background":"#1b1712","model":"#bc8848","ambient":0.32,"specular":0.16,"rim":0.07},
 "mono":      {"background":"#111111","model":"#d0d0d0","ambient":0.30,"specular":0.09,"rim":0.06},
 "resin":     {"background":"#1a1d24","model":"#9ad3c1","ambient":0.36,"specular":0.12,"rim":0.07},
}
DEFAULT_STYLE="slate"
_STYLE_NAME=None          # set by --style; propagated to the render workers
VIEW_ELEV,VIEW_AZIM=26,-58

def _hex(c, fallback=(0.8,0.83,0.9)):
    try:
        h=str(c).strip().lstrip("#")
        if len(h)==3: h="".join(ch*2 for ch in h)
        return tuple(int(h[i:i+2],16)/255.0 for i in (0,2,4))
    except Exception:
        return fallback

def _hexstr(c, fallback="#20242c"):
    h=str(c).strip()
    return h if re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", h) else fallback

def style_names():
    return sorted(THUMB_PRESETS)

def _style():
    """Resolved look for one render: a named preset, with anything set under
    rules.json's "thumbnail_style" laid over the top."""
    R=rules().get("thumbnail_style")
    R=R if isinstance(R,dict) else {}
    name=(_STYLE_NAME or R.get("preset") or DEFAULT_STYLE)
    st=dict(THUMB_PRESETS.get(str(name).strip().lower(), THUMB_PRESETS[DEFAULT_STYLE]))
    for k in ("background","model","ambient","specular","rim","rim_color"):
        if k in R: st[k]=R[k]
    out={"background_hex":_hexstr(st.get("background")),
         "background_rgb":tuple(int(round(x*255)) for x in _hex(st.get("background"),(0.125,0.14,0.17))),
         "model":_hex(st.get("model")),
         "rim_rgb":_hex(st.get("rim_color","#ffffff"),(1.0,1.0,1.0))}
    for k in ("ambient","specular","rim"):
        try: out[k]=max(0.0,min(2.0,float(st.get(k,THUMB_PRESETS[DEFAULT_STYLE][k]))))
        except (TypeError,ValueError): out[k]=THUMB_PRESETS[DEFAULT_STYLE][k]
    return out

# ---------- rendering (host-native paths, no mount translation) ----------
class TO(Exception): pass
# Per-render timeout via SIGALRM is Unix-only; on Windows it's unavailable, so
# we degrade gracefully to no per-render timeout (face_cap keeps renders bounded).
_HAS_ALARM = hasattr(signal, "SIGALRM")
if _HAS_ALARM:
    signal.signal(signal.SIGALRM, lambda s,f:(_ for _ in ()).throw(TO()))
def _alarm(n):
    """Unix-only graceful timeout. On Windows this does nothing at all, which is why
    the parent process enforces a deadline of its own — see _render_pool()."""
    if _HAS_ALARM:
        signal.alarm(max(0,int(n)))
def _salvage_binary_stl(np, trimesh, data):
    """Some STLs trimesh will not parse but which are perfectly good binary STL:
    84-byte header then 50 bytes a triangle. Read them by hand."""
    body=data[84:]; nt=len(body)//50
    if nt<=0: return None
    a=np.frombuffer(body[:nt*50],dtype=np.uint8).reshape(nt,50)
    v=a[:,12:48].copy().view("<f4").reshape(-1,3)
    return trimesh.Trimesh(vertices=v,faces=np.arange(nt*3).reshape(nt,3),process=False)

def _load_meshes(sources):
    """sources: file paths, or (bytes, extension) pairs for geometry already in
    memory — a model inside a zip goes down exactly the same path as one on disk.
    Returns (meshes, first_error)."""
    import numpy as np, trimesh
    meshes=[]; err=None
    for src in sources:
        blob=ext=None; label=None
        if isinstance(src,tuple): blob,ext=src; label=f"<{ext} in archive>"
        else: label=os.path.basename(src); ext=os.path.splitext(src)[1]
        try:
            if (ext or "").lower() in CAD_EXT:
                # CAD is boundary representation, not triangles. cascadio hands the
                # tessellation to OpenCascade and gives back glTF, which trimesh
                # already reads — so a .step joins the normal path from here on,
                # including one inside a zip, which arrives as bytes anyway.
                cad=_cad_reader()
                if cad is None:
                    if err is None: err=f"{label}: no CAD reader — run: pip install cascadio"
                    continue
                if blob is None:
                    with open(src,"rb") as fp: blob=fp.read()
                blob=cad.load(blob, file_type="step"); ext=".glb"
            if blob is None:
                m=trimesh.load(src, force="mesh")
            else:
                m=trimesh.load(io.BytesIO(blob), file_type=ext.lstrip("."), force="mesh")
            if isinstance(m,trimesh.Scene): m=m.dump(concatenate=True)
            if not (hasattr(m,"faces") and len(m.faces)>0):
                name=(src if blob is None else "x"+ext).lower()
                if name.endswith(".stl"):
                    if blob is None:
                        with open(src,"rb") as fp: blob=fp.read()
                    sal=_salvage_binary_stl(np, trimesh, blob)
                    if sal is not None: m=sal
            if hasattr(m,"faces") and len(m.faces)>0: meshes.append(m)
        except Exception as e:
            if err is None: err=f"{label}: {type(e).__name__}: {e}"[:160]
            continue
    return meshes, err

def _render(model_paths, out_path, timeout=0):
    """Render one thumbnail from mesh files on disk. See _draw for the rest."""
    return _render_sources(list(model_paths), out_path, timeout=timeout,
                           count=len(model_paths))

def _render_sources(sources, out_path, timeout=0, count=None):
    """Returns (True, note_or_None) or (False, reason). The reason used to be
    discarded, which is why a project that never got a picture gave no clue why —
    a missing library, a corrupt mesh and an out-of-memory kill looked identical."""
    try:
        try:
            import numpy as np, matplotlib; matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            from mpl_toolkits.mplot3d.art3d import Poly3DCollection
            from PIL import Image; import trimesh
        except ImportError as e:
            return (False, "render library missing: "+(getattr(e,"name",None) or str(e)))
        _alarm(timeout)
        meshes,load_err=_load_meshes(sources)
        if not meshes:
            _alarm(0)
            return (False, load_err or f"no loadable geometry in {count or len(sources)} file(s)")
        notes=[]
        # Most downloaded kits are laid out on a print plate: every part authored
        # where it sits on the bed, nowhere near the others. Concatenating those
        # draws scattered debris rather than the model — on a real library a 316 MB
        # nine-part kit came out as a handful of specks. When the whole envelope is
        # far larger than its biggest piece, the parts are laid out rather than
        # assembled, so draw the biggest piece instead of the scatter.
        if len(meshes)>=3:
            spans=[]
            for m in meshes:
                b=np.asarray(m.bounds,dtype=float)
                spans.append((float(np.linalg.norm(b[1]-b[0])), b, m))
            lo=np.min([b[0] for _d,b,_m in spans],axis=0)
            hi=np.max([b[1] for _d,b,_m in spans],axis=0)
            widest=max(d for d,_b,_m in spans)
            spread=float(np.linalg.norm(hi-lo))/max(widest,1e-9)
            if spread>_spread_limit():
                notes.append(f"{len(meshes)} parts laid out apart ({spread:.1f}x "
                             "the largest); drew the largest")
                meshes=[max(spans,key=lambda s:s[0])[2]]
        mesh=trimesh.util.concatenate(meshes) if len(meshes)>1 else meshes[0]
        v=np.asarray(mesh.vertices,dtype=float); f=np.asarray(mesh.faces)
        # RENDER_CAP was 16,000, which was costing far more than it saved: measured
        # on a 114k-face mesh, matplotlib draws the lot in 0.66s against 0.16s for
        # 16k — under a second, on a job where loading the STL takes several. The
        # low cap was the main reason sculpts came out faceted.
        if len(f)>RENDER_CAP:
            reduced=False
            for attempt in ("trimesh","fast_simplification"):
                try:
                    if attempt=="trimesh":
                        # face_count=, NOT positional: the first parameter is
                        # `percent`, so simplify_quadric_decimation(16000) asked for
                        # a 16000x reduction and raised every single time. The bare
                        # except swallowed it, so this path had never once run.
                        dm=mesh.simplify_quadric_decimation(face_count=RENDER_CAP)
                        nv,nf=np.asarray(dm.vertices,dtype=float),np.asarray(dm.faces)
                    else:
                        import fast_simplification as _fs
                        vv,ff=_fs.simplify(np.asarray(mesh.vertices,dtype=np.float32),
                                           np.asarray(mesh.faces,dtype=np.int32),
                                           target_count=RENDER_CAP)
                        nv,nf=np.asarray(vv,dtype=float),np.asarray(ff)
                    if len(nf)>0: v,f,reduced=nv,nf,True; break
                except Exception:
                    continue
            # Striding keeps every Nth triangle, which does not simplify a surface —
            # it scatters it into loose specks. It used to run whenever no decimator
            # was installed, and since trimesh's own quadric decimation ALSO needs
            # fast-simplification, that was every high-poly model on a default
            # install. Now it is only ever used to keep a genuinely enormous mesh
            # inside memory, and it says so.
            if not reduced and len(f)>HARD_CAP:
                step=int(np.ceil(len(f)/HARD_CAP)); f=f[::step]
                notes.append("too big to reduce without fast-simplification; drawn from "
                             f"every {step}th triangle — run: pip install fast-simplification")
        # tight, robust framing: center on the bulk's bounding box, fill the frame,
        # and match the box aspect so thin/elongated parts don't render as a speck.
        lo=np.percentile(v,1,axis=0); hi=np.percentile(v,99,axis=0)
        ctr=(lo+hi)/2.0; v=v-ctr; half=(hi-lo)/2.0
        half=np.where(half<=0,1e-6,half)
        # Smooth shading. matplotlib paints each polygon ONE colour and cannot
        # interpolate across it, so the only thing that can vary smoothly is the
        # shading field — one averaged normal per face instead of its own flat one.
        # That needs consistent winding first: downloaded STLs are full of reversed
        # faces, and two opposite normals meeting at a vertex cancel to nothing.
        # (An earlier attempt oriented faces away from the centre instead, which is
        # wrong for anything that is not star-shaped — on a torus the inner surface
        # points inward, and it came out banded.)
        sn=None
        try:
            tm=trimesh.Trimesh(vertices=v, faces=f, process=False)
            tm.fix_normals()
            f=np.asarray(tm.faces)
            vnorm=np.asarray(tm.vertex_normals,dtype=float)
            if vnorm.shape==v.shape and np.isfinite(vnorm).all():
                sn=vnorm[f].mean(axis=1)
        except Exception:
            sn=None
        tri=v[f]
        fn=np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0])
        flen=np.linalg.norm(fn,axis=1)
        fn=fn/np.where(flen==0,1.0,flen)[:,None]
        if sn is None:
            sn=fn                          # no consistent winding: flat, as before
        else:
            slen=np.linalg.norm(sn,axis=1)
            sn=sn/np.where(slen==0,1.0,slen)[:,None]
            sn[slen<0.35]=fn[slen<0.35]    # cancelled out: keep the hard edge
        # Two-sided lighting: |n.L| rather than clipping at 0, so a face that is
        # still wound backwards lights like a correct one instead of going black.
        st=_style()
        L=np.array([.3,.4,.86]);   L=L/np.linalg.norm(L)
        L2=np.array([-.55,-.35,.4]); L2=L2/np.linalg.norm(L2)
        e,a=np.radians(VIEW_ELEV),np.radians(VIEW_AZIM)
        V=np.array([np.cos(e)*np.cos(a), np.cos(e)*np.sin(a), np.sin(e)])
        H=L+V; H=H/np.linalg.norm(H)
        key=np.abs(sn@L)
        diff=np.clip(st["ambient"]+0.54*key+0.18*np.abs(sn@L2),0,1)
        # Specular is gated on the key light: an unlit face must not sprout a
        # highlight, and a broad sheen just clips large flat faces to white.
        spec=st["specular"]*np.power(np.abs(sn@H),48)*key
        rim=st["rim"]*np.power(np.clip(1.0-np.abs(sn@V),0,1),4)*key
        base=np.array(st["model"]); rimc=np.array(st["rim_rgb"])
        col=np.zeros((len(f),4))
        col[:,:3]=np.clip(base[None,:]*diff[:,None]+spec[:,None]+rim[:,None]*rimc[None,:],0,1)
        col[:,3]=1
        fig=plt.figure(figsize=(5.12,5.12),dpi=100); ax=fig.add_subplot(111,projection="3d")
        ax.add_collection3d(Poly3DCollection(tri,facecolors=col,edgecolors="none",linewidths=0))
        pad=1.06
        ax.set_xlim(-half[0]*pad,half[0]*pad);ax.set_ylim(-half[1]*pad,half[1]*pad);ax.set_zlim(-half[2]*pad,half[2]*pad)
        asp=half/half.max(); asp=np.clip(asp,0.12,1.0)
        ax.set_box_aspect(tuple(asp))
        try: ax.set_proj_type("ortho")
        except Exception: pass
        ax.view_init(VIEW_ELEV,VIEW_AZIM); ax.set_axis_off()
        bg=st["background_hex"]
        fig.patch.set_facecolor(bg); ax.set_facecolor(bg); fig.subplots_adjust(0,0,1,1)
        buf=io.BytesIO(); fig.savefig(buf,format="png",facecolor=bg); plt.close(fig); buf.seek(0)
        _save_webp(Image.open(buf), out_path); _alarm(0); return (True,"; ".join(notes) or None)
    except TO:
        _alarm(0); return (False,"timeout")
    except MemoryError:
        _alarm(0); return (False,"out of memory (mesh too large to render)")
    except Exception as e:
        _alarm(0); return (False,f"{type(e).__name__}: {e}"[:160])

def _reuse(img_path, out_path, timeout=20):
    """Reuse the artist's shipped preview image. Returns (True, None) or (False, reason)."""
    try:
        from PIL import Image; _alarm(timeout)
        im=Image.open(img_path); im.load(); _save_webp(im,out_path); _alarm(0); return (True,None)
    except TO:
        _alarm(0); return (False,"timeout")
    except Exception as e:
        _alarm(0); return (False,f"{type(e).__name__}: {e}"[:160])

def _save_webp(im, out_path, edge=512, ceiling=200000):
    from PIL import Image
    im=im.convert("RGB"); w,h=im.size; sc=edge/max(w,h)
    if sc<1: im=im.resize((max(1,int(w*sc)),max(1,int(h*sc))),Image.LANCZOS)
    q=85
    while q>=35:
        b=io.BytesIO(); im.save(b,"WEBP",quality=q,method=4)
        if b.tell()<=ceiling or q==35:
            atomic_write_bytes(out_path, b.getvalue()); return
        q-=10

# Faces we will happily draw, and the point past which memory matters more than
# looks. Between the two we simply draw everything.
RENDER_CAP=120000
HARD_CAP=250000
# If a kit's whole envelope is more than this many times its largest single part,
# the parts are laid out on a print plate rather than assembled together.
# Measured on three layouts: 3.3x for a nine-part plate, 1.3x and 1.6x for kits
# that genuinely fit together. Override with "render_spread_limit" in rules.json.
SPREAD_LIMIT=2.5

def _spread_limit():
    try: return float(rules().get("render_spread_limit", SPREAD_LIMIT))
    except (TypeError,ValueError): return SPREAD_LIMIT

PREVIEW_HINT=re.compile(r"render|preview|thumb|_prev|display|promo|cover|hero|showcase",re.I)

# ---------- scan a folder into project/archive records ----------
def _short(path, keep=3):
    segs=[x for x in str(path).replace("/","\\").split("\\") if x]
    return ("..." if len(segs)>keep else "")+"\\".join(segs[-keep:])

def _iter_dirs(root):
    """Walk with os.scandir instead of os.walk + getsize. On Windows the file sizes
    come back with the directory listing that has already been read, so no extra
    stat() per file is needed — on a large cloud folder that is most of the wait.
    Unreadable folders are counted and skipped; symlinks and junctions are never
    followed, exactly as os.walk(followlinks=False) behaved.
    Yields (dirpath, [DirEntry for the files in it], unreadable_so_far)."""
    stack=[str(root)]; unreadable=0
    skip_dirs=_ignored_dirs(); skip_pre=_ignored_prefixes()
    while stack:
        d=stack.pop()
        files=[]
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir():
                            if e.name.strip().lower() in skip_dirs: continue
                            if not e.is_symlink(): stack.append(e.path)
                            continue
                    except OSError:
                        continue
                    if e.name.startswith(skip_pre): continue
                    files.append(e)
        except OSError:
            unreadable+=1
            continue
        yield d, files, unreadable

_WANTED=MODEL|IMG|ARCH
# Never real models, always noise in the catalog. __MACOSX/._* are the resource
# forks a Mac leaves inside a zip; the rest are system folders. Override with
# "ignore_folders" / "ignore_file_prefixes" in rules.json.
_IGNORE_DIRS={"__macosx",".git",".svn","$recycle.bin","system volume information",
              ".trashes","#recycle",".dropbox.cache",".tmp.driveupload"}
_IGNORE_PREFIXES=("._",)

def _ignored_dirs():
    R=rules(); v=R.get("ignore_folders")
    if isinstance(v,list): return {str(x).strip().lower() for x in v if isinstance(x,str)}
    return _IGNORE_DIRS

def _ignored_prefixes():
    R=rules(); v=R.get("ignore_file_prefixes")
    if isinstance(v,list): return tuple(str(x) for x in v if isinstance(x,str))
    return _IGNORE_PREFIXES

def _is_ignored_path(path):
    """True if any folder on the way to this path is one we never index."""
    bad=_ignored_dirs()
    return any(seg.strip().lower() in bad
               for seg in str(path).replace("/","\\").split("\\") if seg)

def drop_ignored(by_id):
    """Remove entries that live inside an ignored folder. A rescan alone cannot do
    this — merging only adds and updates — so junk catalogued before the rule
    existed would stay for good."""
    gone=[k for k,v in by_id.items() if _is_ignored_path(v.get("path",""))]
    for k in gone: by_id.pop(k,None)
    return len(gone)
_PROGRESS_EVERY=2.0     # seconds between "still going" lines
_STALL_NOTICE=60.0      # after this long with nothing finishing, say so

def scan_folder(root, progress=True):
    """Read one folder tree into project and archive records. Metadata only —
    names and sizes. Nothing is opened, so nothing is pulled out of the cloud."""
    projects={}; archives={}
    ndirs=nfiles=nseen=unreadable=0
    t0=time.time(); last=t0
    for dp,files,unreadable in _iter_dirs(root):
        ndirs+=1
        models=[]; images=[]
        for e in files:
            nseen+=1
            ext=os.path.splitext(e.name)[1].lower()
            if ext not in _WANTED:      # don't stat what we are going to ignore
                continue
            nfiles+=1
            try: sz=e.stat().st_size
            except OSError: sz=0
            f=e.name
            if ext in MODEL: models.append((f,sz))
            elif ext in IMG: images.append((f,sz))
            else:
                full=e.path
                aid=stable_id(full); cat,fac,src,tags=classify(full,os.path.splitext(f)[0])
                archives[aid]={"id":aid,"type":"archive","name":os.path.splitext(f)[0],"path":full,
                  "source":src,"category":cat,"faction":fac,"packed":True,"part_count":None,
                  "formats":[ext.lstrip(".")],"size_bytes":sz,"thumbnail":None,"thumb_status":"packed",
                  "tags":tags+["format:"+ext.lstrip("."),"source:"+src,"packed:true"],"model_files":[]}
        if models:
            pid=stable_id(dp); name=os.path.basename(dp) or dp
            cat,fac,src,tags=classify(dp,name,[m for m,_ in models])
            fmts=sorted(set(os.path.splitext(m)[1].lstrip(".") for m,_ in models))
            biggest=max(models,key=lambda x:x[1])[0]
            hinted=[i for i in images if PREVIEW_HINT.search(i[0])] or images
            prev=max(hinted,key=lambda x:x[1])[0] if images else None
            projects[pid]={"id":pid,"type":"project","name":name,"path":dp,"source":src,
              "category":cat,"faction":fac,"packed":False,"part_count":len(models),"formats":fmts,
              "size_bytes":sum(s for _,s in models),"thumbnail":None,"thumb_status":"pending",
              "primary_file":biggest,
              "has_shipped_preview":prev is not None,"preview_file":os.path.join(dp,prev) if prev else None,
              "tags":tags+["format:"+f for f in fmts]+["source:"+src,"packed:false"],
              "model_files":[m for m,_ in models]}
        # A big cloud folder can take many minutes to walk. Saying nothing for all
        # of that looks exactly like being stuck, which is what it was doing.
        if progress and time.time()-last>=_PROGRESS_EVERY:
            last=time.time()
            print(f"    {ndirs:,} folders · {nseen:,} files · {len(projects):,} projects · "
                  f"{len(archives):,} archives · {time.time()-t0:.0f}s   [{_short(dp)}]", flush=True)
    if progress:
        print(f"    done: {ndirs:,} folders and {nseen:,} files in {time.time()-t0:.0f}s — "
              f"{len(projects):,} projects, {len(archives):,} archives", flush=True)
        if unreadable:
            print(f"    ({unreadable:,} folder(s) could not be read and were skipped)", flush=True)
    return projects, archives

def _hydrated_parts(it):
    """(hydrated paths, cloud-only count, not-found count) for one project.
    Attribute reads only — asking about an online-only file never pulls it down.
    This replaces a check that only ever looked at model_files[0]: a kit whose
    first part was in the cloud was written off entirely, and one whose first part
    was local had its REMAINING parts handed to the renderer, which opened them
    and so forced exactly the download the whole design is meant to avoid."""
    hyd=[]; cloud=0; gone=0
    base=it.get("path","")
    for f in (it.get("model_files") or []):
        st=_hydrated(os.path.join(base,f))
        if st is True: hyd.append(os.path.join(base,f))
        elif st is False: cloud+=1
        else: gone+=1
    return hyd,cloud,gone

def _renderable_now(it):
    """(prev_ok, model_ok) using attribute reads only — never triggers a download."""
    prev=it.get("preview_file")
    prev_ok=bool(it.get("has_shipped_preview") and prev and _hydrated(prev) is True)
    hyd,_c,_g=_hydrated_parts(it)
    return prev_ok, bool(hyd)

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

def _shell_thumb(src_path, out_path, size=512, bg=None):
    """Render a thumbnail via Windows' own shell handler (Explorer). Fast + cached.
    Returns True on success, False if unavailable/failed (caller falls back to mesh)."""
    try:
        import pythoncom, ctypes
        from win32com.shell import shell
        from PIL import Image
        if bg is None: bg=_style()["background_rgb"]
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

# Reading an archive is the one place this tool touches an archive's bytes, so
# callers MUST check _hydrated() first or a cloud placeholder gets pulled down.
# Nothing is ever extracted to disk: single members are read into memory, under
# these caps, and the archive is opened read-only.
ZIP_IMAGE_CAP=40*1024*1024
ZIP_MODEL_CAP=96*1024*1024

def _zip_member(zf, zi, cap):
    """One member's bytes, or None if it is (or claims to be) over the cap."""
    if zi.file_size>cap: return None
    with zf.open(zi) as fp:
        data=fp.read(cap+1)          # read past the cap so a lying header is caught
    return None if len(data)>cap else data

def _zip_contents(zf):
    """(model members, image members) from the central directory, junk excluded."""
    models=[]; images=[]
    for zi in zf.infolist():
        name=zi.filename
        if name.endswith("/") or getattr(zi,"is_dir",lambda: False)(): continue
        if "__MACOSX" in name: continue
        base=name.replace("\\","/").rsplit("/",1)[-1]
        if not base or base.startswith("._"): continue
        ext=os.path.splitext(base)[1].lower()
        if ext in MODEL: models.append(zi)
        elif ext in IMG: images.append(zi)
    return models, images

def _zip_thumb(src, out_path):
    """Look inside a .zip for something to show. Returns (status, detail, extra),
    where extra is catalog fields learned from the archive's contents.
    Archives were indexed by filename alone, so every one of them was a blank card
    saying "packed .zip" — and the catalog could not say how many parts were in it
    or what formats it held."""
    import zipfile
    ext=os.path.splitext(src)[1].lower()
    if ext!=".zip":
        return ("unsupported", f"{ext} archives are indexed by name only", None)
    try:
        with zipfile.ZipFile(src) as zf:
            models,images=_zip_contents(zf)
            extra={"part_count":len(models) or None,
                   "inner_bytes":sum(zi.file_size for zi in models),
                   "model_files":[zi.filename.replace("\\","/").rsplit("/",1)[-1]
                                  for zi in sorted(models,key=lambda z:-z.file_size)[:200]],
                   "formats":sorted({os.path.splitext(zi.filename)[1].lstrip(".").lower()
                                     for zi in models} | {"zip"})}
            hinted=[zi for zi in images
                    if PREVIEW_HINT.search(zi.filename.rsplit("/",1)[-1])] or images
            for zi in sorted(hinted,key=lambda z:-z.file_size):
                data=_zip_member(zf,zi,ZIP_IMAGE_CAP)
                if data is None: continue
                try:
                    from PIL import Image
                    im=Image.open(io.BytesIO(data)); im.load()
                    _save_webp(im,out_path)
                    return ("reused",
                            "preview image from inside the zip "
                            f"({zi.filename.rsplit('/',1)[-1]})", extra)
                except Exception:
                    continue
            for zi in sorted(models,key=lambda z:-z.file_size):
                if zi.file_size>ZIP_MODEL_CAP:
                    return ("packed", f"largest model inside is {zi.file_size/1e6:.0f} MB — "
                                      "too big to render without extracting", extra)
                data=_zip_member(zf,zi,ZIP_MODEL_CAP)
                if data is None: continue
                mext=os.path.splitext(zi.filename)[1].lower()
                ok,why=_render_sources([(data,mext)], out_path, timeout=_TIMEOUT, count=1)
                if ok:
                    return ("mesh", f"rendered from {zi.filename.rsplit('/',1)[-1]} "
                                    "inside the zip", extra)
                return ("packed", why, extra)
            return ("packed", "no preview image or model file inside", extra)
    except zipfile.BadZipFile:
        return ("packed", "not a readable zip", None)
    except RuntimeError as e:
        return ("packed", f"{e}"[:120], None)          # encrypted archives land here
    except MemoryError:
        return ("packed", "not enough memory to read this archive", None)
    except Exception as e:
        return ("packed", f"{type(e).__name__}: {e}"[:120], None)

CAD_EXT={".step",".stp"}
_CAD=None
def _cad_reader():
    """cascadio — OpenCascade's STEP reader as a half-megabyte wheel, rather than
    the several hundred megabytes a full CAD kernel costs. Optional: without it
    CAD files are reported as unsupported instead of failing, with the fix named."""
    global _CAD
    if _CAD is None:
        try:
            import cascadio; _CAD=cascadio
        except Exception:
            _CAD=False
    return _CAD or None

def _unrenderable():
    """Extensions we cannot draw right now — empty once the CAD reader is present."""
    return set() if _cad_reader() else CAD_EXT
# Every value thumb_status can hold. OK_STATUS means a picture exists.
OK_STATUS={"reused","shell","mesh","existing"}
STATUS_LABEL={
 "reused":"used the artist's own preview image","shell":"rendered by the Windows handler",
 "mesh":"rendered from the mesh","existing":"already on disk from an earlier run",
 "packed":"packed .zip — nothing extracted",
 "pending":"not attempted yet","cloud_only":"online-only, not downloaded yet",
 "missing":"files not found at the recorded path","too_big":"skipped — over the --max-mb limit",
 "no_model_file":"no model file recorded","unsupported":"CAD (.step/.stp) — no converter bundled",
 "timeout":"the renderer timed out","failed":"the renderer ran and produced nothing"}

def _thumb_for(it, out_path, engine="shell"):
    """Produce one thumbnail: reuse shipped preview, else shell handler, else mesh render.
    Returns (status, detail, extra) — extra being catalog fields learned along the
    way, which only archives currently produce. Every outcome names itself; the old
    version returned the bare string "pending" for a file still in the cloud, a
    missing render library, a corrupt mesh and an out-of-memory kill alike."""
    if it.get("type")=="archive":
        st=_hydrated(it.get("path",""))
        if st is False: return ("cloud_only","the archive is online-only",None)
        if st is None:  return ("missing","the archive is not at the recorded path",None)
        return _zip_thumb(it["path"], out_path)
    prev=it.get("preview_file")
    if it.get("has_shipped_preview") and prev and _hydrated(prev) is True:
        ok,_why=_reuse(prev, out_path)
        if ok: return ("reused",None,None) # a bad preview file just falls through to the mesh
    if not (it.get("model_files") or []): return ("no_model_file",None,None)
    hyd,cloud,gone=_hydrated_parts(it)
    if not hyd:
        if cloud: return ("cloud_only", f"{cloud} part(s) still online-only", None)
        return ("missing", f"{gone} file(s) not found under {it.get('path','')}", None)
    blocked=_unrenderable()
    renderable=[p for p in hyd if os.path.splitext(p)[1].lower() not in blocked]
    if not renderable:
        kinds=", ".join(sorted({os.path.splitext(p)[1].lower() for p in hyd}))
        hint=" — run: pip install cascadio" if blocked else ""
        return ("unsupported", f"only {kinds}{hint}", None)
    best=None; bestsz=-1
    for p in renderable:               # largest hydrated part = most representative
        try: sz=os.path.getsize(p)
        except OSError: continue
        if sz>bestsz: bestsz=sz; best=p
    if engine!="mesh" and best and _shell_thumb(best, out_path): return ("shell",None,None)
    ok,why=_render(renderable, out_path, timeout=_TIMEOUT)
    if ok:
        # rendered — but say so if it is only part of the kit, or was reduced crudely
        bits=[]
        if cloud or gone:
            bits.append(f"rendered from {len(renderable)} of {len(renderable)+cloud+gone} part(s)")
        if why: bits.append(why)
        return ("mesh", "; ".join(bits) or None, None)
    return ("timeout" if why=="timeout" else "failed", why, None)

_ENGINE="shell"
_TIMEOUT=0.0            # seconds a single render may take; 0 = no limit
def _init_worker(engine, timeout=0.0, style=None):
    global _ENGINE,_TIMEOUT,_STYLE_NAME
    _ENGINE=engine; _TIMEOUT=timeout; _STYLE_NAME=style

def render_one(it):
    """Pool worker. Uses the module-global engine (set via pool initializer). Never raises."""
    try:
        st,detail,extra=_thumb_for(it, os.path.join(THUMBS, it["id"]+".webp"), _ENGINE)
        return (it["id"], st, detail, extra)
    except Exception as e:
        return (it["id"], "failed", f"{type(e).__name__}: {e}"[:160], None)

def _skip_status(it):
    """Why this item cannot be rendered right now, or (None, None) if it can.
    Attribute reads only — nothing is opened, so nothing is pulled out of the cloud."""
    if it.get("type")=="archive":
        st=_hydrated(it.get("path",""))
        if st is True: return (None,None)
        if st is False: return ("cloud_only","the archive is online-only")
        return ("missing","the archive is not at the recorded path")
    if not (it.get("model_files") or []): return ("no_model_file",None)
    hyd,cloud,gone=_hydrated_parts(it)
    if hyd: return (None,None)
    if cloud: return ("cloud_only", f"{cloud} part(s) still online-only")
    return ("missing", f"{gone} file(s) not found under {it.get('path','')}")

def _mesh_libs_missing():
    """The first render library that will not import, or None."""
    for mod in ("numpy","trimesh","matplotlib","PIL"):
        try: __import__(mod)
        except Exception: return mod
    return None

RENDERABLE_TYPES=("project","archive")

def status_counts(items):
    from collections import Counter
    return Counter((it.get("thumb_status") or "pending")
                   for it in items if it.get("type") in RENDERABLE_TYPES)

def print_status_summary(items, prefix="  ", examples=3):
    """What happened to every project, and for anything without a picture, why.
    Before this, a project that failed to render was indistinguishable from one
    nobody had got to yet: both said 'pending' and neither said anything at all."""
    c=status_counts(items); ex={}
    for it in items:
        if it.get("type") not in RENDERABLE_TYPES: continue
        st=it.get("thumb_status") or "pending"
        if st in OK_STATUS: continue
        ex.setdefault(st,[])
        if len(ex[st])<examples: ex[st].append((it.get("path",""), it.get("thumb_error") or ""))
    order=[k for k in ("reused","shell","mesh","existing") if c.get(k)]+ \
          [k for k in sorted(c) if k not in OK_STATUS]
    print(f"\n{prefix}Thumbnails, by outcome:")
    for st in order:
        print(f"{prefix}  {c[st]:>6,}  {st:<14} {STATUS_LABEL.get(st,'')}")
    for st in order:
        if st in OK_STATUS or not ex.get(st): continue
        print(f"\n{prefix}{st} — examples:")
        for path,why in ex[st]:
            print(f"{prefix}  {path}")
            if why: print(f"{prefix}    {why}")

def _dur(sec):
    """Coarse enough not to twitch: seconds below 90, then whole minutes, then hours."""
    sec=max(0,int(sec))
    if sec<90: return f"{sec}s"
    m,rem=divmod(sec,60)
    if m<90: return f"{m}m"
    h,m=divmod(m,60)
    return f"{h}h{m:02d}m"

class _Eta:
    """A time-left estimate that is neither jumpy nor quietly wrong.

    The first version divided elapsed time by the number of items finished. Render
    cost is not one-per-item though: it is a fixed overhead plus something roughly
    proportional to mesh size, and the queue is deliberately sorted smallest-first.
    So a per-item mean spends the whole run chasing a rising average — smooth, but
    on a simulated 400-item run it sat wrong by 83% of the total runtime and had to
    keep revising upward. Measuring bytes per second instead is worse: the fixed
    overhead dominates the small items at the front and it overshoots wildly.

    So fit both terms. elapsed ~= a*(items done) + b*(bytes done), solved by least
    squares from running sums, then applied to what is left. Same simulation: mean
    error 2.7% of the run instead of 83%. It also absorbs the worker count on its
    own, since more workers simply make a and b smaller.

    Falls back to the per-item mean if the fit is degenerate, and says nothing at
    all until there is enough to be worth printing."""
    SMOOTHING=0.25          # weight given to the newest estimate
    MIN_ITEMS=8
    MIN_SECONDS=5.0
    def __init__(self, targets):
        self.n_total=len(targets)
        self.b_total=sum(max(int(it.get("size_bytes") or 0),0) for it in targets)
        self.n=0; self.b=0; self.t0=time.time(); self.eta=None
        self.Snn=self.SnB=self.SBB=self.Snt=self.SBt=0.0

    def add(self, it):
        """One item finished. Updates the fit and the smoothed estimate."""
        self.n+=1; self.b+=max(int(it.get("size_bytes") or 0),0)
        t=time.time()-self.t0
        # bytes in millions keeps the normal equations away from 1e18 magnitudes
        n=float(self.n); B=self.b/1e6
        self.Snn+=n*n; self.SnB+=n*B; self.SBB+=B*B; self.Snt+=n*t; self.SBt+=B*t
        if self.n>=self.n_total: self.eta=0.0; return   # finished is finished
        if self.n<self.MIN_ITEMS or t<self.MIN_SECONDS: return
        fresh=self._predict(t)
        if fresh is None: return
        self.eta=fresh if self.eta is None else (1-self.SMOOTHING)*self.eta+self.SMOOTHING*fresh

    def _predict(self, elapsed):
        n_left=self.n_total-self.n; b_left=(self.b_total-self.b)/1e6
        det=self.Snn*self.SBB-self.SnB*self.SnB
        if det>0:
            a=( self.SBB*self.Snt-self.SnB*self.SBt)/det
            c=(-self.SnB*self.Snt+self.Snn*self.SBt)/det
            if a>=0 and c>=0 and (a or c):
                return max(0.0, a*n_left+c*b_left)
        if self.n<=0: return None
        return max(0.0, n_left*(elapsed/self.n))    # degenerate fit: per-item mean

    def percent(self):
        """Share of the WORK, not of the item count — on a smallest-first queue those
        are very different numbers. Only meaningful once the fit exists."""
        if self.n_total<=0: return 100.0
        if self.eta is None: return 100.0*self.n/self.n_total
        el=time.time()-self.t0
        done=el/(el+self.eta) if el+self.eta>0 else self.n/self.n_total
        return max(0.0,min(100.0,100.0*done))

    def summary(self):
        """The progress tail, or nothing at all while there is nothing worth saying.
        Repeating 'estimating...' on every line is its own kind of noise."""
        if self.eta is None: return ""
        return f" · {self.percent():.0f}% · ~{_dur(self.eta)} left"

class _Ticker:
    """Calls a function on a timer until stopped.

    Progress used to be printed every 5 completions, which means it can only speak
    when something finishes — so slow renders put minutes between lines, and one
    enormous mesh produced total silence for as long as it took. A timer does not
    care whether anything has finished."""
    def __init__(self, fn, every=None):
        self.fn=fn; self.every=every
        self._stop=threading.Event(); self.lock=threading.Lock(); self._thread=None
    def _interval(self):
        return self.every if self.every is not None else _PROGRESS_EVERY
    def start(self):
        self._thread=threading.Thread(target=self._run, daemon=True)
        self._thread.start(); return self
    def _run(self):
        while not self._stop.wait(max(self._interval(),0.01)):
            try: self.emit()
            except Exception: pass       # a progress line must never kill a run
    def emit(self):
        with self.lock: self.fn()
    def stop(self):
        self._stop.set()
        if self._thread: self._thread.join(timeout=1.0)
    def __enter__(self): return self.start()
    def __exit__(self, *exc): self.stop()

_POOL_POLL=0.05

def _render_pool(targets, jobs, engine, timeout, on_done, on_progress=None, pool_factory=None):
    """Render in a worker pool with a deadline the PARENT enforces.

    A per-render timeout used to be signal.SIGALRM, which does not exist on Windows,
    so on the machine this tool is actually for there was no timeout at all: one
    corrupt or enormous mesh stalled a worker for good, and with it the whole run.

    multiprocessing.Pool cannot cancel a task once a worker has started it, so the
    only way to end one is to kill the pool. That is what happens here: each item is
    submitted individually with at most one in flight per worker (so submission time
    really is start time), and if one passes its deadline the pool is terminated, the
    offender is recorded as a timeout, whatever else was in flight goes back on the
    queue, and a fresh pool picks up where this one left off.

    Workers also get an alarm of their own set slightly earlier, so on Unix a render
    bows out cleanly and the parent never has to swing the hammer.

    Returns (timed_out, restarted_renders). pool_factory exists so the tests can
    drive this loop without real processes."""
    import multiprocessing as mp
    def default_factory():
        return mp.Pool(jobs, initializer=_init_worker,
                       initargs=(engine, max(5.0, timeout*0.9) if timeout else 0.0, _STYLE_NAME))
    make=pool_factory or default_factory
    pool=make(); pending=list(targets); inflight={}
    finished=0; timed_out=0; restarted=0
    try:
        while pending or inflight:
            while pending and len(inflight)<jobs:
                it=pending.pop(0)
                inflight[pool.apply_async(render_one,(it,))]=(it,time.time())
            moved=False
            for ar in list(inflight):
                it,started=inflight[ar]
                if ar.ready():
                    del inflight[ar]
                    try: pid,status,detail,extra=ar.get(0)
                    except Exception as e:
                        pid,status,detail,extra=(it["id"],"failed",
                                                 f"{type(e).__name__}: {e}"[:160],None)
                    finished+=1; moved=True
                    on_done(pid,status,detail,extra)
                    if on_progress: on_progress(finished)
                elif timeout and time.time()-started>timeout:
                    waited=time.time()-started
                    others=[i for (i,_s) in inflight.values() if i is not it]
                    try:
                        pool.terminate(); pool.join()
                    except Exception: pass
                    inflight.clear()
                    finished+=1; timed_out+=1; restarted+=len(others); moved=True
                    on_done(it["id"],"timeout",
                            f"no result after {_dur(waited)}; the worker was killed", None)
                    note=(f" {len(others)} other render(s) in progress were restarted."
                          if others else "")
                    print(f"  [!] gave up on {_short(it.get('path',''),2)} after "
                          f"{_dur(waited)}.{note}", flush=True)
                    pending=others+pending
                    pool=make()
                    if on_progress: on_progress(finished)
                    break
            if not moved: time.sleep(_POOL_POLL)
    finally:
        try:
            pool.terminate(); pool.join()
        except Exception: pass
    return timed_out, restarted

def render_missing(items, checkpoint=None, force=False, jobs=1, engine="shell", max_mb=1500,
                   timeout=300.0):
    _ensure_dirs()
    global _ENGINE,_TIMEOUT          # used by the in-process path (render_one reads them)
    _ENGINE=engine; _TIMEOUT=max(5.0,timeout*0.9) if timeout else 0.0
    idmap={it["id"]:it for it in items if it.get("type") in ("project","archive")}
    targets=[]; pending_total=0
    for it in items:
        if it.get("type") not in ("project","archive"): continue
        if not force and os.path.exists(os.path.join(THUMBS,it["id"]+".webp")):
            it["thumbnail"]=it["id"]+".webp"
            if it.get("thumb_status") not in OK_STATUS: it["thumb_status"]="existing"
            it.pop("thumb_error",None)
            continue
        pending_total+=1
        st,why=_skip_status(it)
        if st is None: targets.append(it)
        else:
            it["thumb_status"]=st
            if why: it["thumb_error"]=why
            else: it.pop("thumb_error",None)
    # Smallest first: the gallery fills up fast and you can look at real results
    # long before the giant outliers are reached.
    targets.sort(key=lambda it: it.get("size_bytes",0))
    deferred=[it for it in targets if it.get("size_bytes",0)>max_mb*1e6]
    targets=[it for it in targets if it.get("size_bytes",0)<=max_mb*1e6]
    for it in deferred:
        it["thumb_status"]="too_big"
        it["thumb_error"]=f"{it.get('size_bytes',0)/1e6:.0f} MB, over the {max_mb:.0f} MB limit"
    total=len(targets); cloud=pending_total-total-len(deferred)
    jobs=max(1,min(int(jobs),total or 1))
    missing_lib=_mesh_libs_missing()
    if missing_lib:
        # The old code rendered nothing and printed "0 ok" without ever saying why.
        print(f"\n  [!] {missing_lib} is not installed, so the mesh renderer cannot run.")
        print( "      Every render will fail until you run:  pip install -r requirements.txt\n", flush=True)
    if deferred:
        print(f"  deferring {len(deferred)} huge project(s) over {max_mb:.0f} MB "
              f"(run with --max-mb 99999 to include them).", flush=True)
    print(f"  {pending_total} item(s) need a thumbnail; {total} downloaded & renderable now, "
          f"{cloud} not renderable yet{' (FORCE re-render)' if force else ''}.", flush=True)
    print(f"  Rendering with {jobs} core(s), engine='{engine}', "
          f"{('giving up on any one model after '+_dur(timeout)) if timeout else 'no time limit per model'}.",
          flush=True)
    print("  Safe to press Ctrl+C anytime — finished thumbnails are kept and", flush=True)
    print("  re-running resumes where it stopped.\n", flush=True)
    done=0; failed=0; t0=time.time(); seen=set(); eta=_Eta(targets)
    last_finish=[t0]
    def report():
        quiet=time.time()-last_finish[0]
        stalled=f" · nothing finished for {_dur(quiet)}" if quiet>=_STALL_NOTICE else ""
        print(f"  {len(seen)}/{total} done ({done} ok, {failed} failed) · "
              f"{_dur(time.time()-t0)} elapsed{eta.summary()}{stalled}", flush=True)
    ticker=_Ticker(report)
    def apply(pid,status,detail=None,extra=None):
        nonlocal done,failed
        it=idmap.get(pid)
        if it is None: return
        with ticker.lock:                # don't let a progress line print mid-update
            seen.add(pid); eta.add(it); last_finish[0]=time.time()
            it["thumb_status"]=status
            if detail: it["thumb_error"]=detail
            else: it.pop("thumb_error",None)
            for k,v in (extra or {}).items():   # what looking inside the zip taught us
                if v not in (None,[],""): it[k]=v
            if status in OK_STATUS: it["thumbnail"]=pid+".webp"; done+=1
            else: failed+=1
    def _ck(k):
        if checkpoint and (k%150==0 or k==50):
            checkpoint()
            print("   [gallery.html refreshed — you can open it now]", flush=True)
    timed_out=restarted=0
    ticker.start()
    try:
        if total:
            # One worker or six, it goes through the pool: that is the only place a
            # render can be given a deadline the parent can actually enforce.
            try:
                timed_out,restarted=_render_pool(targets, jobs, engine, timeout, apply, _ck)
            except Exception as e:
                print(f"  [no worker pool available ({type(e).__name__}: {e}); rendering in "
                      f"this process instead]", flush=True)
                if timeout and not _HAS_ALARM:
                    print("      NOTE: the per-model time limit cannot be enforced this way on "
                          "Windows.\n      A stuck model will stall the run; Ctrl+C and lower "
                          "--max-mb if that happens.", flush=True)
                for it in [i for i in targets if i["id"] not in seen]:
                    pid,status,detail,extra=render_one(it)
                    apply(pid,status,detail,extra); _ck(len(seen))
    finally:
        ticker.stop()
    if total: ticker.emit()
    if timed_out:
        print(f"\n  {timed_out} model(s) hit the {_dur(timeout)} limit and were abandoned"
              + (f"; {restarted} other render(s) were restarted as a result" if restarted else "")
              + ".\n  Raise it with --timeout, or skip the big ones with --max-mb.", flush=True)
    print(f"\n  finished: {done}/{total} rendered in {time.time()-t0:.0f}s"
          f"{f', {failed} could not be rendered' if failed else ''}", flush=True)
    print_status_summary(items)
    return done

SOURCES=os.path.join(LIB,"sources.txt")

_SOURCES_CACHE=None
def load_sources():
    """Cached: _display_fields() and source_tag() ask for this once PER ITEM, and
    write_catalog/write_gallery/write_import each walk every item."""
    global _SOURCES_CACHE
    if _SOURCES_CACHE is not None: return list(_SOURCES_CACHE)
    if not os.path.exists(SOURCES):
        _SOURCES_CACHE=[]; return []
    out=[]
    with open(SOURCES,encoding="utf-8") as fp:
        for line in fp:
            s=line.strip()
            if s and not s.startswith("#"): out.append(s.rstrip("\\/"))
    _SOURCES_CACHE=out
    return list(out)

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
    global _SOURCES_CACHE; _SOURCES_CACHE=list(seen)
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

def primary_file(it):
    """The filename to paste into Windows search when hunting for this item: the
    largest part, recorded during the scan from sizes we already had. Entries from
    a catalog written before that field existed fall back to the first part."""
    mfs=it.get("model_files") or []
    return it.get("primary_file") or (mfs[0] if mfs else os.path.basename(it.get("path","")))

def _norm(p):
    """Path in the same shape stable_id() sees it, for comparing two entries."""
    return str(p or "").strip().lower().replace("/","\\").rstrip("\\")

def _source_root_for(path):
    """The scanned folder from sources.txt that contains this path, or None."""
    p=_norm(path); best=None
    for root in load_sources():
        r=_norm(root)
        if p==r or p.startswith(r+"\\"):
            if best is None or len(r)>len(_norm(best)): best=root
    return best

def _root_unavailable(path):
    """True when the folder this entry lives under is not reachable AT ALL — an
    unplugged drive, a network share that is down, a cloud folder that has not
    mounted yet. That is not the same as the files having been deleted, and it is
    the case where pruning destroys a perfectly good catalog: running
    --relocate --prune with the models drive disconnected used to empty it."""
    root=_source_root_for(path)
    if root is not None: return not os.path.isdir(root)
    anchor=os.path.splitdrive(str(path))[0]
    if anchor: return not os.path.isdir(anchor+os.sep)
    return False

def _move_thumb(old_id, new_id):
    src=os.path.join(THUMBS,old_id+".webp"); dst=os.path.join(THUMBS,new_id+".webp")
    if old_id==new_id or not os.path.exists(src): return False
    try:
        os.replace(src,dst); return True
    except OSError:
        return False

def _rekey(by_id, it, new_path):
    """Move an entry to the id its new path implies, carrying its thumbnail file.
    Ids are sha1(path). The old relocate kept the ORIGINAL id after adopting a new
    path, so the entry and its location disagreed for good: the next scan of that
    folder minted a second entry under the path-derived id, both folders existed,
    and no later relocate could ever merge them. Four commands produced a permanent
    duplicate — one card with the picture, one without."""
    old_id=it["id"]; new_id=stable_id(new_path)
    it["path"]=new_path
    if new_id==old_id: return old_id
    _move_thumb(old_id,new_id)
    it["id"]=new_id
    if it.get("thumbnail"): it["thumbnail"]=new_id+".webp"
    by_id.pop(old_id,None); by_id[new_id]=it
    return new_id

def _dedupe_paths(by_id):
    """Collapse entries that point at the same folder. Heals catalogs that already
    carry duplicates from the old relocate. Keeps the entry whose id matches its
    path, and carries the other one's thumbnail across rather than re-rendering."""
    seen={}; dropped=0
    for it in sorted((i for i in by_id.values() if i.get("type")=="project"),
                     key=lambda i: i.get("id","")):
        k=_norm(it.get("path",""))
        if not k: continue
        other=seen.get(k)
        if other is None: seen[k]=it; continue
        canon=stable_id(it.get("path",""))
        if it["id"]==canon and other["id"]!=canon: keep,drop=it,other
        elif other["id"]==canon and it["id"]!=canon: keep,drop=other,it
        elif other.get("thumbnail") and not it.get("thumbnail"): keep,drop=other,it
        else: keep,drop=it,other
        if not keep.get("thumbnail") and drop.get("thumbnail"):
            if _move_thumb(drop["id"],keep["id"]) or os.path.exists(os.path.join(THUMBS,keep["id"]+".webp")):
                keep["thumbnail"]=keep["id"]+".webp"
                keep["thumb_status"]=drop.get("thumb_status") or "existing"
        by_id.pop(drop["id"],None); seen[k]=keep; dropped+=1
    return dropped

def relocate_and_prune(by_id, prune=False):
    """Detect entries whose folder moved: match a missing old path to a present new
    path by fingerprint, then update in place (same thumbnail, same tags).
    Returns (moved, dropped, deduped, kept_unreachable)."""
    missing=[]; present={}
    for it in list(by_id.values()):
        if it.get("type")!="project": continue
        fp=_fingerprint(it)
        if not fp: continue
        if os.path.isdir(it.get("path","")): present.setdefault(fp,[]).append(it)
        else: missing.append((fp,it))
    moved=0; dropped=0
    for fp,old in missing:
        cands=[c for c in present.get(fp,[]) if c is not old and c.get("id") in by_id]
        if len(cands)!=1: continue
        new=cands[0]; newpath=new["path"]
        # keep the ORIGINAL entry's history; adopt the new location and its id
        old["model_files"]=new.get("model_files",old.get("model_files"))
        old["preview_file"]=new.get("preview_file")
        if new.get("primary_file"): old["primary_file"]=new["primary_file"]
        old["thumb_status"]=old.get("thumb_status","pending")
        by_id.pop(new["id"],None)
        _rekey(by_id, old, newpath)
        moved+=1
    deduped=_dedupe_paths(by_id)
    kept=0
    if prune:
        doomed=[]
        for k,v in list(by_id.items()):
            path=v.get("path","")
            here=os.path.isdir(path) if v.get("type")=="project" else os.path.isfile(path)
            if here: continue
            if _root_unavailable(path): kept+=1; continue
            doomed.append(k)
        for k in doomed:
            by_id.pop(k,None); dropped+=1
    return moved,dropped,deduped,kept

def reclassify(by_id, dry_run=False):
    """Recompute category / faction / source / labels for every entry from the rules
    file as it stands NOW. Catalog only: no folder is walked, no model file is
    opened, no thumbnail is touched. Editing rules.json used to require a full
    rescan, because classification only ever happened at scan time.
    Returns a list of (item, before, after) for everything that changed."""
    changed=[]
    for it in by_id.values():
        cat,fac,src,tags=classify(it.get("path",""), it.get("name",""), it.get("model_files"))
        newtags=(tags
                 +["format:"+f for f in (it.get("formats") or [])]
                 +["source:"+src,"packed:"+("true" if it.get("packed") else "false")])
        before=(it.get("category"),it.get("faction"),it.get("source"),list(it.get("tags") or []))
        after=(cat,fac,src,newtags)
        if before!=after: changed.append((it,before,after))
        if not dry_run:
            it["category"]=cat; it["faction"]=fac; it["source"]=src; it["tags"]=newtags
    return changed

DUPES_CSV=os.path.join(LIB,"potential_duplicates.csv")

def _loose_fingerprint(it):
    """Same model filenames and count, ignoring size — catches the same kit
    re-downloaded or re-exported at a different quality."""
    names=sorted((n or "").strip().lower() for n in (it.get("model_files") or []))
    if not names: return None
    return hashlib.md5(("|".join(names)+f"::{len(names)}").encode("utf-8","ignore")).hexdigest()[:16]

def find_duplicates(by_id):
    """Kits whose contents match: same model filenames, same count, same total size.
    This reuses the fingerprint the move-detector already computes, so it costs one
    pass over the catalog — no folder is walked and no file is opened.
    Returns (exact, near) as lists of (wasted_bytes, [items]), biggest waste first."""
    exact={}; loose={}
    for it in by_id.values():
        if it.get("type")!="project": continue
        fp=_fingerprint(it)
        if fp: exact.setdefault(fp,[]).append(it)
        lf=_loose_fingerprint(it)
        if lf: loose.setdefault(lf,[]).append(it)
    def rank(groups):
        out=[]
        for g in groups.values():
            if len(g)<2: continue
            g=sorted(g, key=lambda i: (i.get("path") or "").lower())
            # keeping one copy, the rest is what you would get back
            out.append((sum(i.get("size_bytes",0) for i in g[1:]), g))
        out.sort(key=lambda t: -t[0])
        return out
    ex=rank(exact)
    covered={i["id"] for _w,g in ex for i in g}
    near=[(w,g) for w,g in rank(loose) if not all(i["id"] in covered for i in g)]
    return ex, near

def show_duplicates(by_id, limit=20):
    """Report duplicate kits and write potential_duplicates.csv. Reports only —
    nothing is deleted and nothing in your model folders is touched, ever."""
    ex,near=find_duplicates(by_id)
    if not ex and not near:
        print("\nNo duplicate kits found."); return
    waste=sum(w for w,_g in ex)
    copies=sum(len(g)-1 for _w,g in ex)
    print(f"\n{len(ex):,} kit(s) are catalogued more than once — {copies:,} extra "
          f"cop(ies), {waste/1e9:.2f} GB you would get back by keeping one of each.")
    for i,(w,g) in enumerate(ex[:limit],1):
        dn,_c,_bc=_display_fields(g[0].get("path",""))
        mb=(g[0].get("size_bytes") or 0)/1e6
        print(f"\n  {i}. {dn}  —  {len(g)} copies, {mb:,.0f} MB each "
              f"({g[0].get('part_count') or '?'} parts)")
        for it in g: print(f"       {it.get('path','')}")
    if len(ex)>limit:
        print(f"\n  ...and {len(ex)-limit:,} more. The full list is in the CSV below.")
    if near:
        nw=sum(w for w,_g in near)
        print(f"\n{len(near):,} more group(s) have the SAME FILENAMES but different "
              f"sizes — the same kit re-exported or re-downloaded ({nw/1e9:.2f} GB).")
        for w,g in near[:5]:
            dn,_c,_bc=_display_fields(g[0].get("path",""))
            print(f"\n  {dn}  —  {len(g)} versions")
            for it in g:
                print(f"       {(it.get('size_bytes') or 0)/1e6:>8,.0f} MB   {it.get('path','')}")
    rows=[]
    for kind,groups in (("exact",ex),("same-names",near)):
        for gi,(w,g) in enumerate(groups,1):
            for it in g:
                dn,col,_bc=_display_fields(it.get("path",""))
                rows.append([kind,f"{kind}-{gi}",dn,col,len(g),
                             round((it.get("size_bytes") or 0)/1e6,1),
                             it.get("part_count") or "", it.get("path","")])
    buf=io.StringIO(); w_=csv.writer(buf)
    w_.writerow(["match","group","name","collection","copies","size_mb","parts","path"])
    w_.writerows(rows)
    atomic_write(DUPES_CSV, buf.getvalue())
    print(f"\nFull list: {DUPES_CSV}")
    print("  That file contains absolute paths — it is gitignored, keep it that way.")
    print("  Nothing was deleted. This only ever reads the catalog; deciding which")
    print("  copy to keep, and removing it, is yours to do in Explorer.")

def show_unmatched(by_id, n=25):
    """What the rules did not recognise: entries sitting in the default category with
    no faction, and the names of the parts inside them. This is the raw material for
    writing better patterns. It prints folder and file NAMES only — never absolute
    paths — so it can be pasted somewhere without handing over a map of the drive."""
    default=_compiled()["default_cat"]
    rows=[it for it in by_id.values()
          if it.get("category")==default and not it.get("faction")]
    print(f"\n{len(rows):,} of {len(by_id):,} entries matched no rule "
          f"(category '{default}', no faction).")
    if not rows:
        print("Everything is classified. Nothing to tune."); return
    rows.sort(key=lambda it: -(it.get("size_bytes") or 0))
    print(f"the {min(n,len(rows))} largest:\n")
    for it in rows[:n]:
        dn,col,bc=_display_fields(it.get("path",""))
        # don't echo the item's own name back at it as its location
        col="" if (col=="(none)" or col.lower()==dn.lower()) else col
        where=" › ".join(x for x in (col, bc) if x)
        print(f"  {dn}")
        if where: print(f"      in: {where}")
        mfs=it.get("model_files") or []
        if mfs:
            print("      files: "+", ".join(mfs[:6])
                  +(f"  (+{len(mfs)-6} more)" if len(mfs)>6 else ""))
    if len(rows)>n:
        print(f"\n  ...and {len(rows)-n:,} more. Show more with:  --unmatched {min(len(rows),200)}")
    print("\nAdd patterns for what you see to rules.json (or rules.local.json), then:")
    print("  python update_catalog.py --reclassify --dry-run")

def print_reclassify_report(changed, total, examples=12):
    fields=("category","faction","source","labels")
    hits={f:0 for f in fields}
    for _it,b,a in changed:
        for i,f in enumerate(fields):
            if b[i]!=a[i]: hits[f]+=1
    print(f"\n{len(changed):,} of {total:,} entries change under the current rules:")
    for f in fields: print(f"  {hits[f]:>7,}  {f}")
    if changed:
        print("\nexamples:")
        for it,b,a in changed[:examples]:
            dn,_c,_bc=_display_fields(it.get("path",""))
            bits=[]
            for i,f in enumerate(("category","faction","source")):
                if b[i]!=a[i]: bits.append(f"{f}: {b[i] or '—'} -> {a[i] or '—'}")
            if b[3]!=a[3]:
                gone=[t for t in b[3] if t not in a[3]]; new=[t for t in a[3] if t not in b[3]]
                if gone: bits.append("-"+", -".join(gone))
                if new: bits.append("+"+", +".join(new))
            print(f"  {dn}")
            for x in bits: print(f"      {x}")
        if len(changed)>examples: print(f"  ... and {len(changed)-examples:,} more")

def write_catalog(by_id):
    items=list(by_id.values())
    cat={"generated":"updated","schema":"3dprintlibrary-1",
         "counts":{"total":len(items),
                   "projects":sum(1 for i in items if i["type"]=="project"),
                   "archives":sum(1 for i in items if i["type"]=="archive"),
                   "thumbnails_rendered":sum(1 for i in items if i.get("thumbnail"))},
         "items":items}
    backup_catalog()
    atomic_write(CATALOG, json.dumps(cat, indent=1))
    cols=["id","type","name","display_name","collection","breadcrumb","primary_file","all_files","fingerprint","category","faction","source","packed","part_count","formats","size_bytes","size_mb","path","thumbnail","thumb_status","thumb_error","tags"]
    buf=io.StringIO(); w=csv.writer(buf); w.writerow(cols)   # built in memory, written once
    for it in items:
        dn,col,bc=_display_fields(it["path"])
        mfs=it.get("model_files") or []
        w.writerow([it["id"],it["type"],it["name"],dn,col,bc,primary_file(it),"|".join(mfs[:25]),
                    _fingerprint(it) or "",it["category"],it.get("faction") or "",
                    it["source"],it["packed"],it.get("part_count") if it.get("part_count") is not None else "",
                    "|".join(it.get("formats",[])),it["size_bytes"],round(it["size_bytes"]/1e6,2),
                    it["path"],it.get("thumbnail") or "",it.get("thumb_status",""),
                    it.get("thumb_error") or "","|".join(it.get("tags",[]))])
    atomic_write(CSVFILE, buf.getvalue())

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
    crumb=[]
    for s in segs[start:len(segs)-1]:
        low=s.strip().lower()
        if low==display.strip().lower(): continue          # don't echo the title back
        if crumb and low==crumb[-1].strip().lower(): continue   # collapse repeats
        crumb.append(s)
    return (display, col or "(none)", " › ".join(crumb[-3:]))

def write_gallery(by_id):
    slim=[]
    for it in by_id.values():
        dn,col,bc=_display_fields(it["path"])
        mfs=it.get("model_files") or []
        pf=primary_file(it)
        slim.append({"n":dn,"c":it["category"],"f":it.get("faction") or "","s":it["source"],
            "col":col,"bc":bc,"p":1 if it.get("packed") else 0,"pc":it.get("part_count") or 0,
            "mb":round(it["size_bytes"]/1e6,1),"t":it.get("thumbnail") or "","path":it["path"],
            "ts":it.get("thumb_status") or "","te":it.get("thumb_error") or "",
            "pf":pf,"nf":len(mfs),
            "tags":[x for x in it.get("tags",[]) if x.startswith("type:")]})
    # "</" cannot appear in a Windows path, but it can on other platforms, and it
    # would close the <script> element early. Escaping it keeps the page valid.
    data=json.dumps(slim,separators=(",",":")).replace("</","<\\/")
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
const esc=t=>String(t==null?"":t).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
// why a card has no picture — the same vocabulary the CSV and --diagnose use
const TSLABEL={cloud_only:"online-only, not downloaded",missing:"files not found",
 too_big:"skipped — too large",no_model_file:"no model file",unsupported:"CAD file — no converter",
 timeout:"render timed out",failed:"render failed",pending:"not rendered yet",
 packed:"packed .zip",reused:"artist's preview",shell:"Windows handler",mesh:"rendered mesh",
 existing:"rendered earlier"};
const tsText=d=>TSLABEL[d.ts]||"no thumbnail yet";
function opts(sel,vals){vals=[...new Set(vals)].filter(Boolean).sort();for(const v of vals){const o=document.createElement("option");o.value=v;o.textContent=v;sel.appendChild(o)}}
opts($("#cat"),DATA.map(d=>d.c));
opts($("#fac"),DATA.map(d=>d.f));
opts($("#src"),DATA.map(d=>d.s));
opts($("#col"),DATA.map(d=>d.col));
// one extra entry in the thumbnail filter for each problem actually present
for(const v of [...new Set(DATA.filter(d=>!d.t&&!d.p).map(d=>d.ts).filter(Boolean))].sort()){
 const o=document.createElement("option");o.value="st:"+v;o.textContent="\u2937 "+(TSLABEL[v]||v);$("#th").appendChild(o);}
let filtered=[],shown=0,PAGE=120,lastG=null,gkey="";
const GLABEL={col:"collection",f:"faction",c:"category",s:"source"};
function apply(){
 const q=$("#q").value.toLowerCase(),c=$("#cat").value,f=$("#fac").value,s=$("#src").value,
   col=$("#col").value,pk=$("#pk").value,th=$("#th").value; gkey=$("#gb").value;
 filtered=DATA.filter(d=>{
   if(c&&d.c!==c)return false; if(f&&d.f!==f)return false; if(s&&d.s!==s)return false;
   if(col&&d.col!==col)return false; if(pk!==""&&String(d.p)!==pk)return false;
   if(th==="1"&&!d.t)return false; if(th==="0"&&d.t)return false;
   if(th.slice(0,3)==="st:"&&d.ts!==th.slice(3))return false;
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
   const thumb=d.t?`<img loading="lazy" src="thumbnails/${d.t}">`:`<div class="ph">${d.p?'\\u{1F4E6} packed .zip':esc(tsText(d))}</div>`;
   const fac=d.f?`<span class="tag fac">${esc(d.f)}</span>`:"";
   const types=(d.tags||[]).map(t=>`<span class="tag">${esc(t.replace('type:',''))}</span>`).join("");
   // only show the collection if it isn't just the item's own name repeated
   const colTxt=(d.col&&d.col!=="(none)"&&d.col.toLowerCase()!==d.n.toLowerCase())?d.col:"";
   const crumbTxt=[colTxt,d.bc].filter(Boolean).join(" › ");
   const crumb=crumbTxt?`<div class="crumb" title="${esc(d.path)}">${esc(crumbTxt)}</div>`:"";
   c.innerHTML=`<div class="thumb">${thumb}<span class="badge">${d.p?(d.pc?'zip · '+d.pc+'p':'zip'):d.pc+'p'}</span></div>
   <div class="body"><div class="nm" title="${esc(d.path)}">${esc(d.n)}</div>${crumb}
   <div class="meta"><span class="tag">${esc(d.c)}</span>${fac}<span class="tag">${esc(d.s)}</span>${types}<span class="tag">${d.mb}MB</span></div>
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
   ["Kind",d.p?"packed .zip (nothing extracted)":"extracted files"],
   ["Parts",d.pc||"—"],["Size",d.mb+" MB"],["Main file",d.pf||"—"],
   ["Picture",tsText(d)+(d.te?" — "+d.te:"")],
   ["Labels",(d.tags||[]).map(t=>t.replace("type:","")).join(", ")||"—"]];
 $("#lbtab").innerHTML=rows.map(r=>`<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td></tr>`).join("");
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
    atomic_write(os.path.join(LIB,"gallery.html"), html)

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
                with open(tp,"rb") as fp: blob=fp.read()
                images.append({"id":imgid,"modelId":mid,"data":base64.b64encode(blob).decode()})
                m["imageId"]=imgid
        models.append(m)
    backup={"schema":2,"exportedAt":"updated","models":models,"images":images,"spools":[],"jobs":[]}
    atomic_write(os.path.join(LIB,"print-tracker-import.json"), json.dumps(backup))

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
          and _hydrated_parts(it)[0]]
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
        hyd,_c,_g=_hydrated_parts(it)     # downloaded parts only — never pull one down
        renderable=[q for q in hyd if os.path.splitext(q)[1].lower() not in _unrenderable()]
        for eng in ("shell","mesh"):
            p=os.path.join(outdir, f"{it['id']}_{eng}.webp")
            ts=time.time(); why=None
            if eng=="shell":
                best=None;bestsz=-1
                for fp in renderable:
                    try: sz=os.path.getsize(fp)
                    except OSError: continue
                    if sz>bestsz: bestsz=sz; best=fp
                ok=bool(best and _shell_thumb(best,p))
                if not ok: why="no Windows thumbnail handler for this file"
            else:
                ok,why=_render(renderable, p)
            dt=time.time()-ts
            res[eng]=(ok,dt,os.path.basename(p) if ok else None)
            print(f"        {eng:5} {'ok  ' if ok else 'FAIL'} {dt:5.1f}s"
                  f"{'' if ok else '   '+str(why or '')}", flush=True)
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
    _ensure_dirs()
    outdir=os.path.join(LIB,"_render_test"); os.makedirs(outdir,exist_ok=True)
    cand=[it for it in items if it.get("type")=="project" and it.get("model_files")]
    cand.sort(key=lambda it:-it.get("size_bytes",0))
    hyd=[it for it in cand if _hydrated_parts(it)[0]]
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
        status,detail,_x=_thumb_for(it, newp, engine)
        dt=time.time()-ts; times.append(dt); avg=sum(times)/len(times); left=(len(picked)-i)*avg
        print(f"        {status} in {dt:.1f}s   (avg {avg:.1f}s/file, ~{left:.0f}s left)"
              f"{chr(10)+'        '+detail if detail else ''}", flush=True)
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
        if not (it.get("model_files") or []): nofile+=1; continue
        parts,cl,gone=_hydrated_parts(it)
        if parts: hyd+=1
        elif cl:
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
    print_status_summary(items, prefix="")
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

def build_parser():
    """Every flag the tool accepts. Separate from main() so the tests can check
    that LIBRARY.bat and library.sh only ever call flags that actually exist."""
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
    ap.add_argument("--timeout",type=float,default=300,metavar="SECONDS",help="give up on any single model after this long and move on (default 300; 0 = no limit)")
    ap.add_argument("--style",choices=style_names(),default=None,help="colour scheme for rendered thumbnails (default: slate, or whatever rules.json sets)")
    ap.add_argument("--compare-engines",type=int,default=0,metavar="N",help="render N SMALL models with BOTH engines side by side into _engine_test/ (fast; changes nothing else)")
    ap.add_argument("--prune",action="store_true",help="remove catalog entries whose files no longer exist (after move-detection runs)")
    ap.add_argument("--relocate",action="store_true",help="re-check every entry's path and fix ones that moved (no scan, no rendering)")
    ap.add_argument("--sources",action="store_true",help="list the folders this library scans")
    ap.add_argument("--add-source",metavar="PATH",help="add a folder to the scan list")
    ap.add_argument("--remove-source",metavar="PATH",help="remove a folder from the scan list (does not delete files or entries)")
    ap.add_argument("--reclassify",action="store_true",help="recompute category/faction/source/labels for every entry from the current rules.json - no scan, no rendering")
    ap.add_argument("--dry-run",action="store_true",help="with --reclassify: show what would change and write nothing")
    ap.add_argument("--restore-backup",nargs="?",const="",default=None,metavar="FILE",help="put a backup from backups/ back in place as catalog.json (newest if no name given)")
    ap.add_argument("--backup",action="store_true",help="copy catalog.json into backups/ right now")
    ap.add_argument("--unmatched",nargs="?",type=int,const=25,default=0,metavar="N",help="list entries no rule matched, with the part names inside them, to help write patterns")
    ap.add_argument("--duplicates",nargs="?",type=int,const=20,default=0,metavar="N",help="find kits catalogued more than once and write potential_duplicates.csv (reports only, deletes nothing)")
    return ap

def main():
    ap=build_parser()
    a=ap.parse_args()
    _ensure_dirs()
    if a.style:
        global _STYLE_NAME; _STYLE_NAME=a.style
    jobs = a.jobs if a.jobs>0 else min(6, max(1,(os.cpu_count() or 2)-1))
    if a.restore_backup is not None:
        restore_backup(a.restore_backup or None); return
    if a.backup:
        b=backup_catalog(force=True)
        print(f"backed up to {b}" if b else "nothing to back up yet (no catalog.json)")
        return
    by_id=load_catalog()
    print(f"loaded {len(by_id)} existing items")
    if a.diagnose:
        diagnose(list(by_id.values())); return
    if a.sources:
        show_sources(by_id); return
    if a.unmatched:
        show_unmatched(by_id, a.unmatched); return
    if a.duplicates:
        show_duplicates(by_id, a.duplicates); return
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
    if a.reclassify:
        changed=reclassify(by_id, dry_run=a.dry_run)
        print_reclassify_report(changed, len(by_id))
        if a.dry_run:
            print("\ndry run - nothing was written. Drop --dry-run to apply.")
        elif changed:
            write_catalog(by_id); write_gallery(by_id); write_import(by_id)
            print("\ncatalog, gallery and spreadsheet rebuilt. No files were scanned or rendered.")
        else:
            print("\nnothing to do - the catalog already matches the rules.")
        return
    if a.relocate or (a.prune and not a.folders and not a.rescan_all and not a.thumbs_only):
        moved,dropped,deduped,kept=relocate_and_prune(by_id, prune=a.prune)
        write_catalog(by_id); write_gallery(by_id); write_import(by_id)
        bits=[f"relocated {moved} moved project(s)", f"pruned {dropped}"]
        if deduped: bits.append(f"merged {deduped} duplicate entr(ies)")
        print("; ".join(bits)+"; views rebuilt.")
        if kept:
            print(f"\n  {kept} entr(ies) were NOT pruned: the folder they live under is not")
            print( "  reachable right now. An unplugged drive or a cloud folder that has not")
            print( "  mounted is not the same as deleted files. Reconnect it and run again.")
        if not (moved or dropped or deduped or kept):
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
        n=render_missing(list(by_id.values()), checkpoint=_ckpt, force=a.force, jobs=jobs,
                         engine=a.engine, max_mb=a.max_mb, timeout=a.timeout)
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
  --reclassify               re-apply rules.json to everything already catalogued
  --reclassify --dry-run     ...show what that would change, and write nothing
  --unmatched 25             what the rules missed, and the filenames inside
  --duplicates               kits you have more than one copy of (deletes nothing)
  --backup                   copy catalog.json into backups/ right now
  --restore-backup           put the newest backups/ copy back as catalog.json
  --sample 8                 preview 8 thumbnails into _render_test/
Useful extras:  --jobs N (cores)   --max-mb N (skip huge)   --engine shell|mesh
                --timeout N (give up on one model after N seconds, default 300)
                --style NAME   (slate | paper | blueprint | bronze | mono | resin)
""".strip()); return
    added=updated=0
    print("\nScanning reads names and sizes only — nothing is opened, so nothing gets\n"
          "pulled down from the cloud. Ctrl+C is safe here: the catalog is not written\n"
          "until the scan finishes.\n", flush=True)
    for folder in folders:
        if not os.path.isdir(folder): print("  skip (not found):",folder); continue
        print("scanning:",folder, flush=True)
        pj,ar=scan_folder(folder)
        for d in (pj,ar):
            for k,v in d.items():
                if k in by_id: by_id[k].update(v); updated+=1
                else: by_id[k]=v; added+=1
    print(f"merged: {added} new, {updated} updated")
    junk=drop_ignored(by_id)
    if junk: print(f"  dropped {junk} entr(ies) inside folders we never index (__MACOSX and friends)")
    print("checking for entries whose folder moved ...", flush=True)
    moved,dropped,deduped,kept=relocate_and_prune(by_id, prune=a.prune)
    if moved: print(f"  relocated {moved} moved project(s) — thumbnail kept, no re-render")
    if deduped: print(f"  merged {deduped} duplicate entr(ies) pointing at the same folder")
    if dropped: print(f"  pruned {dropped} entr(ies) whose files no longer exist")
    if kept: print(f"  left {kept} entr(ies) alone — the folder they live under is not reachable")
    n=render_missing(list(by_id.values()), checkpoint=lambda: write_catalog(by_id),
                     jobs=jobs, engine=a.engine, max_mb=a.max_mb, timeout=a.timeout)
    write_catalog(by_id)
    print("refreshing gallery.html and print-tracker-import.json ...", flush=True)
    write_gallery(by_id); write_import(by_id)
    print(f"rendered {n} new thumbnails; catalog now {len(by_id)} items; gallery + import updated")

if __name__=="__main__":
    main()
