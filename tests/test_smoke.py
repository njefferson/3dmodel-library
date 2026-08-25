"""Smoke tests for update_catalog.py.

Runs anywhere Python does — no trimesh, no matplotlib, no Windows. The point is
not to check render quality; it is to check that a catalog can be built from a
folder of files, that every view it writes is valid, that nothing fails silently,
and that the paths nobody had ever executed do what they claim.

    python -m unittest discover -s tests -v

The library the tests build lives in a temp folder (LIBRARY_DIR), so nothing is
written next to the script and no real catalog is ever touched.
"""
import contextlib, importlib, io, json, os, random, re, shutil, struct, sys, tempfile, time, unittest
from unittest import mock

def read(path):
    with open(path, encoding="utf-8") as fp: return fp.read()


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path: sys.path.insert(0, ROOT)
import update_catalog as uc            # noqa: E402  (path set above)


def write_stl(path, tris=2):
    """A small but genuinely valid binary STL: 80-byte header, count, 50 bytes each."""
    body = b"smoke-test stl".ljust(80, b"\0") + struct.pack("<I", tris)
    for i in range(tris):
        body += struct.pack("<12f",
                            0, 0, 1,
                            0, 0, float(i), 1, 0, float(i), 0, 1, float(i)) + b"\0\0"
    with open(path, "wb") as fp: fp.write(body)


class LibraryCase(unittest.TestCase):
    """A throwaway library plus a small folder of models, rebuilt for every test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="3dlib-test-")
        self.lib = os.path.join(self.tmp, "library")
        self.models = os.path.join(self.tmp, "models")
        os.makedirs(self.lib); os.makedirs(self.models)
        self.kits = {}
        self.add_kit("Warhound Titan", ["body.stl", "leg_left.stl", "leg_right.stl"], sub="files")
        self.add_kit("Ork Nob", ["nob.stl"])
        os.environ["LIBRARY_DIR"] = self.lib
        importlib.reload(uc)            # LIB and every cache are module-level
        shutil.copy2(os.path.join(ROOT, "rules.json"), os.path.join(self.lib, "rules.json"))

    def tearDown(self):
        os.environ.pop("LIBRARY_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers -------------------------------------------------------
    def add_kit(self, name, files, sub=None, root=None):
        d = os.path.join(root or self.models, name, *( [sub] if sub else [] ))
        os.makedirs(d, exist_ok=True)
        for i, f in enumerate(files): write_stl(os.path.join(d, f), tris=2 + i)
        self.kits[name] = d
        return d

    def run_cli(self, *args, expect_exit=None):
        """Invoke main() in-process. Returns its captured output."""
        buf = io.StringIO(); code = None
        with mock.patch.object(sys, "argv", ["update_catalog.py", *args]):
            with contextlib.redirect_stdout(buf):
                try: uc.main()
                except SystemExit as e: code = e.code
        out = buf.getvalue()
        self.assertEqual(code, expect_exit, "exit code differed\n" + out)
        return out

    def catalog(self):
        return json.loads(read(os.path.join(self.lib, "catalog.json")))

    def projects(self):
        return [i for i in self.catalog()["items"] if i["type"] == "project"]

    def scan(self, *extra):
        return self.run_cli(self.models, "--jobs", "1", *extra)


class TestOutputs(LibraryCase):

    def test_scan_writes_every_view_and_they_are_all_valid(self):
        self.scan()
        cat = self.catalog()
        self.assertEqual(cat["schema"], "3dprintlibrary-1")
        self.assertEqual(cat["counts"]["projects"], 2)
        self.assertEqual(cat["counts"]["total"], len(cat["items"]))

        import csv
        with open(os.path.join(self.lib, "catalog.csv"), encoding="utf-8") as fp:
            rows = list(csv.DictReader(fp))
        self.assertEqual(len(rows), len(cat["items"]))
        self.assertIn("thumb_error", rows[0])
        self.assertTrue(all(r["primary_file"] for r in rows))

        html = read(os.path.join(self.lib, "gallery.html"))
        self.assertIn("const DATA=", html)
        self.assertIn("Warhound Titan", html)
        self.assertEqual(html.count("</script>"), 1)

        imp = json.loads(read(os.path.join(self.lib, "print-tracker-import.json")))
        self.assertEqual(imp["schema"], 2)
        self.assertEqual(len(imp["models"]), len(cat["items"]))
        self.assertTrue(all(m["sources"][0]["url"].startswith("file:///") for m in imp["models"]))

    def test_primary_file_is_the_largest_part(self):
        self.scan()
        titan = [p for p in self.projects() if "Warhound" in p["path"]][0]
        self.assertEqual(titan["primary_file"], "leg_right.stl")   # most triangles

    def test_a_second_scan_changes_nothing(self):
        self.scan()
        before = {i["id"]: i["path"] for i in self.catalog()["items"]}
        out = self.scan()
        self.assertIn("0 new", out)
        self.assertEqual(before, {i["id"]: i["path"] for i in self.catalog()["items"]})

    def test_mac_resource_forks_are_not_catalogued_as_models(self):
        """A zip made on a Mac carries __MACOSX/._name.stl alongside every real file.
        Those were being indexed as projects and then failing to render."""
        junk = os.path.join(self.models, "Some Kit", "__MACOSX")
        os.makedirs(junk)
        write_stl(os.path.join(junk, "._body.stl"))
        self.add_kit("Some Kit", ["body.stl"])
        self.add_kit("Another Kit", ["._sneaky.stl", "real.stl"])
        self.scan()
        paths = [p["path"] for p in self.projects()]
        self.assertFalse([p for p in paths if "__MACOSX" in p], "indexed a resource-fork folder")
        another = [p for p in self.projects() if p["path"].endswith("Another Kit")][0]
        self.assertEqual(another["model_files"], ["real.stl"], "indexed a ._ resource fork")

    def test_junk_already_in_the_catalog_is_dropped_on_the_next_scan(self):
        self.scan()
        cat = json.loads(read(os.path.join(self.lib, "catalog.json")))
        stale = dict(cat["items"][0])
        stale["id"] = "deadbeefdeadbeef"
        stale["path"] = os.path.join(self.models, "Old Kit", "__MACOSX")
        cat["items"].append(stale)
        with open(os.path.join(self.lib, "catalog.json"), "w", encoding="utf-8") as fp:
            json.dump(cat, fp)
        out = self.scan()
        self.assertIn("never index", out)
        self.assertNotIn("deadbeefdeadbeef", read(os.path.join(self.lib, "catalog.json")))

    @unittest.skipIf(sys.platform == "win32", "these characters are illegal in Windows paths")
    def test_markup_in_a_folder_name_cannot_break_the_gallery(self):
        """The names come off somebody's disk, so the page has to survive them.
        The data is JSON inside a <script>, and the cards escape on the way into
        innerHTML — this checks both, since a browser is not available here."""
        self.add_kit('Bits <b>"and"</b> & bobs', ["x.stl"])
        self.scan()
        html = read(os.path.join(self.lib, "gallery.html"))
        self.assertEqual(html.count("</script>"), 1, "a name closed the script element early")
        data = json.loads(html.split("const DATA=", 1)[1].split(";\nconst $", 1)[0].replace("<\\/", "</"))
        self.assertTrue(any('Bits <b>"and"</b> & bobs' in d["path"] for d in data))
        for fragment in ("${esc(d.n)}", "${esc(d.path)}", "${esc(d.c)}", "esc(tsText(d))"):
            # assertTrue, not assertIn: a failed assertIn prints the whole page
            self.assertTrue(fragment in html, f"card field no longer escaped: {fragment}")


class TestScanProgress(LibraryCase):

    def test_a_long_scan_says_what_it_is_doing(self):
        """A big cloud folder takes minutes to walk. Saying nothing for all of it
        is indistinguishable from being stuck."""
        uc._PROGRESS_EVERY = 0        # every folder, so a tiny fixture shows it
        try:
            out = self.scan()
        finally:
            uc._PROGRESS_EVERY = 2.0
        self.assertIn("folders \u00b7", out)
        self.assertIn("projects \u00b7", out)
        self.assertIn("done:", out)
        self.assertIn("Ctrl+C is safe here", out)

    def test_an_unreadable_folder_is_skipped_and_counted(self):
        blocked = os.path.join(self.models, "locked")
        os.makedirs(blocked)
        os.chmod(blocked, 0o000)
        try:
            out = self.scan()
        finally:
            os.chmod(blocked, 0o755)
        self.assertEqual(len(self.projects()), 2, "the readable kits should still be there")
        if os.geteuid() != 0:          # root can read it anyway
            self.assertIn("could not be read", out)


class TestTimeRemaining(unittest.TestCase):
    """The estimate used to divide elapsed time by items finished. Render cost is a
    fixed overhead plus something proportional to mesh size, and the queue is sorted
    smallest-first, so that estimate chased a rising average all run and kept being
    revised upward."""

    def setUp(self):
        self.real_time = uc.time.time
        self.now = 0.0
        uc.time.time = lambda: self.now

    def tearDown(self):
        uc.time.time = self.real_time

    def synthetic_run(self, overhead, per_mb, n=300, seed=7, biggest_first=False):
        rnd = random.Random(seed)
        sizes = sorted(int(10 ** rnd.uniform(4.5, 8.5)) for _ in range(n))
        costs = [overhead + (s / 1e6) * per_mb * rnd.uniform(0.8, 1.3) for s in sizes]
        if biggest_first: sizes, costs = sizes[::-1], costs[::-1]
        return [{"size_bytes": s} for s in sizes], costs

    def test_it_says_nothing_until_it_knows_something(self):
        targets, costs = self.synthetic_run(0.4, 0.05)
        eta = uc._Eta(targets)
        self.assertEqual(eta.summary(), "")
        for it, c in list(zip(targets, costs))[:3]:
            self.now += c; eta.add(it)
        self.assertEqual(eta.summary(), "", "guessed from three items")

    def test_the_estimate_tracks_the_truth_on_a_smallest_first_queue(self):
        targets, costs = self.synthetic_run(0.4, 0.05)
        total = sum(costs)
        eta = uc._Eta(targets)
        errors = []
        for k, (it, c) in enumerate(zip(targets, costs), 1):
            self.now += c; eta.add(it)
            if eta.eta is not None and k <= len(targets) * 0.9:
                errors.append(abs(eta.eta - (total - self.now)))
        self.assertTrue(errors, "never produced an estimate at all")
        mean = sum(errors) / len(errors)
        # the per-item estimator this replaced averaged >80% of total runtime out
        self.assertLess(mean, total * 0.15, f"mean error {mean:.0f}s of a {total:.0f}s run")

    def test_it_never_reports_something_impossible(self):
        targets, costs = self.synthetic_run(0.4, 0.05, n=60, biggest_first=True)
        eta = uc._Eta(targets)
        for it, c in zip(targets, costs):
            self.now += c; eta.add(it)
            self.assertGreaterEqual(eta.eta if eta.eta is not None else 0, 0)
            self.assertGreaterEqual(eta.percent(), 0)
            self.assertLessEqual(eta.percent(), 100)

    def test_it_reads_100_percent_when_it_is_actually_finished(self):
        targets, costs = self.synthetic_run(0.4, 0.05, n=40)
        eta = uc._Eta(targets)
        for it, c in zip(targets, costs):
            self.now += c; eta.add(it)
        self.assertEqual(eta.eta, 0.0)
        self.assertEqual(round(eta.percent()), 100)

    def test_durations_are_coarse_enough_not_to_twitch(self):
        self.assertEqual(uc._dur(0), "0s")
        self.assertEqual(uc._dur(89), "89s")
        self.assertEqual(uc._dur(90), "1m")
        self.assertEqual(uc._dur(3600), "60m")
        self.assertEqual(uc._dur(5400), "1h30m")


class TestProgressKeepsTalking(LibraryCase):
    """Progress used to print every 5 completions, so it could only speak when
    something finished. One enormous mesh meant silence for as long as it took."""

    def slow_render(self, seconds):
        def render(paths, out_path, timeout=25):
            time.sleep(seconds)
            with open(out_path, "wb") as fp: fp.write(b"x")
            return (True, None)
        return render

    def test_a_single_slow_render_still_reports_while_it_runs(self):
        self.scan()
        items = self.projects()
        for it in items:                      # start from nothing rendered
            t = os.path.join(self.lib, "thumbnails", it["id"] + ".webp")
            if os.path.exists(t): os.remove(t)
        uc._PROGRESS_EVERY = 0.05
        buf = io.StringIO()
        try:
            with mock.patch.object(uc, "_render", self.slow_render(0.4)), \
                 mock.patch.object(uc, "_shell_thumb", return_value=False), \
                 contextlib.redirect_stdout(buf):
                uc.render_missing(items, jobs=1, engine="mesh")
        finally:
            uc._PROGRESS_EVERY = 2.0
        lines = [l for l in buf.getvalue().splitlines() if "done (" in l]
        self.assertGreater(len(lines), len(items),
                           "fewer progress lines than items — still reporting per completion")

    def test_it_says_when_nothing_has_finished_for_a_while(self):
        self.scan()
        items = self.projects()
        for it in items:
            t = os.path.join(self.lib, "thumbnails", it["id"] + ".webp")
            if os.path.exists(t): os.remove(t)
        uc._PROGRESS_EVERY, uc._STALL_NOTICE = 0.05, 0.2
        buf = io.StringIO()
        try:
            with mock.patch.object(uc, "_render", self.slow_render(0.5)), \
                 mock.patch.object(uc, "_shell_thumb", return_value=False), \
                 contextlib.redirect_stdout(buf):
                uc.render_missing(items, jobs=1, engine="mesh")
        finally:
            uc._PROGRESS_EVERY, uc._STALL_NOTICE = 2.0, 60.0
        self.assertIn("nothing finished for", buf.getvalue())


class FakeResult:
    """A pool result that becomes ready after a set number of polls — or never."""
    def __init__(self, value=None, ready_after=0, never=False):
        self.value = value; self.left = ready_after; self.never = never
    def ready(self):
        if self.never: return False
        if self.left > 0: self.left -= 1; return False
        return True
    def get(self, timeout=None): return self.value


class FakePool:
    """hang_ids never finish. slow_ids do not finish on their first attempt but do on
    the retry, which is what an item that was in flight when the pool died looks
    like. attempts is shared across pools so the retry can be told apart."""
    def __init__(self, hang_ids, slow_ids, attempts):
        self.hang = hang_ids; self.slow = slow_ids; self.attempts = attempts
        self.terminated = False; self.submitted = []
    def apply_async(self, fn, args):
        it = args[0]; self.submitted.append(it["id"])
        self.attempts[it["id"]] = self.attempts.get(it["id"], 0) + 1
        if it["id"] in self.hang: return FakeResult(never=True)
        if it["id"] in self.slow and self.attempts[it["id"]] == 1:
            return FakeResult(never=True)
        return FakeResult((it["id"], "mesh", None, None))
    def terminate(self): self.terminated = True
    def join(self): pass


class TestRenderTimeout(unittest.TestCase):
    """SIGALRM is Unix-only, so on Windows there was no per-render timeout at all and
    one corrupt mesh stalled the run for good. Pool cannot cancel a running task, so
    the parent enforces the deadline and restarts the pool."""

    def items(self, n):
        return [{"id": f"{i:016x}", "type": "project", "size_bytes": 1000 * (i + 1),
                 "path": f"/kits/kit{i}", "model_files": ["a.stl"]} for i in range(n)]

    def run_pool(self, items, hang, slow, jobs=3, timeout=0.15):
        pools = []; attempts = {}
        def factory():
            p = FakePool(hang, slow, attempts); pools.append(p); return p
        seen = {}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            timed_out, restarted = uc._render_pool(
                items, jobs, "mesh", timeout,
                lambda pid, st, detail, extra=None: seen.__setitem__(pid, (st, detail)),
                pool_factory=factory)
        return seen, pools, timed_out, restarted, buf.getvalue()

    def test_a_stuck_render_is_abandoned_and_the_rest_still_finish(self):
        items = self.items(5)
        stuck = items[2]["id"]
        seen, pools, timed_out, restarted, out = self.run_pool(items, {stuck}, set())
        self.assertEqual(len(seen), 5, "not every item was accounted for")
        self.assertEqual(seen[stuck][0], "timeout")
        self.assertIn("worker was killed", seen[stuck][1])
        for it in items:
            if it["id"] != stuck:
                self.assertEqual(seen[it["id"]][0], "mesh")
        self.assertEqual(timed_out, 1)
        self.assertGreaterEqual(len(pools), 2, "the pool was never restarted")
        self.assertTrue(pools[0].terminated, "the stuck pool was not killed")
        self.assertIn("gave up on", out)

    def test_renders_in_flight_alongside_it_are_requeued_not_lost(self):
        """Killing the pool takes the innocent with it, so they go back on the queue."""
        items = self.items(4)
        stuck = items[0]["id"]
        caught = [items[1]["id"], items[2]["id"]]          # in flight when it is killed
        seen, pools, timed_out, restarted, out = self.run_pool(items, {stuck}, set(caught))
        self.assertEqual(seen[stuck][0], "timeout")
        self.assertGreaterEqual(len(pools), 2)
        for cid in caught:
            self.assertIn(cid, pools[1].submitted, "an in-flight render was dropped")
            self.assertEqual(seen[cid][0], "mesh", "a requeued render never completed")
        self.assertEqual(len(seen), 4, "not every item was accounted for")
        self.assertEqual(restarted, len(caught))
        self.assertIn("restarted", out)

    def test_no_deadline_means_no_killing(self):
        """--timeout 0 restores the old behaviour: wait as long as it takes. (Which
        is why this one is given nothing that hangs — with no deadline, nothing
        would ever end it.)"""
        items = self.items(3)
        seen, pools, timed_out, restarted, out = self.run_pool(items, set(), set(), timeout=0)
        self.assertEqual(len(seen), 3)
        self.assertEqual(timed_out, 0)
        self.assertEqual(len(pools), 1)
        self.assertNotIn("gave up", out)

    def test_workers_are_given_an_alarm_that_fires_before_the_parent_gives_up(self):
        """On Unix the render should bow out cleanly rather than be killed."""
        uc._init_worker("mesh", 90.0)
        self.assertEqual(uc._TIMEOUT, 90.0)
        uc._init_worker("mesh", 0.0)
        self.assertEqual(uc._TIMEOUT, 0.0)


try:
    import numpy, trimesh          # noqa: F401
    import matplotlib; matplotlib.use("Agg")
    HAVE_RENDER = True
except Exception:
    HAVE_RENDER = False


class TestThumbnailStyle(unittest.TestCase):

    def tearDown(self):
        uc._STYLE_NAME = None

    def test_colours_parse_in_both_notations(self):
        self.assertEqual(uc._hex("#ffffff"), (1.0, 1.0, 1.0))
        self.assertEqual(uc._hex("#000"), (0.0, 0.0, 0.0))
        self.assertEqual(uc._hex("nonsense"), (0.8, 0.83, 0.9))      # falls back
        self.assertEqual(uc._hexstr("javascript:x"), "#20242c")

    def test_every_preset_is_complete_and_sane(self):
        for name in uc.style_names():
            uc._STYLE_NAME = name
            st = uc._style()
            for key in ("background_hex", "model", "ambient", "specular", "rim", "rim_rgb"):
                self.assertIn(key, st, f"{name} is missing {key}")
            self.assertRegex(st["background_hex"], r"^#[0-9a-fA-F]{6}$")
            self.assertTrue(all(0.0 <= c <= 1.0 for c in st["model"]))
            self.assertLessEqual(st["ambient"] + st["specular"] + st["rim"], 1.0,
                                 f"{name} can clip to white before diffuse is added")

    def test_the_style_flag_beats_the_rules_file(self):
        uc._STYLE_NAME = "bronze"
        self.assertEqual(uc._style()["background_hex"],
                         uc.THUMB_PRESETS["bronze"]["background"])

    def test_an_unknown_name_falls_back_rather_than_crashing(self):
        uc._STYLE_NAME = "chartreuse-nightmare"
        self.assertEqual(uc._style()["background_hex"],
                         uc.THUMB_PRESETS[uc.DEFAULT_STYLE]["background"])


@unittest.skipUnless(HAVE_RENDER, "needs numpy/trimesh/matplotlib")
class TestRendering(unittest.TestCase):
    """These actually draw. The stride fallback used to run on every high-poly model
    on a default install, and it does not simplify a surface — it scatters it."""

    def setUp(self):
        import trimesh
        self.tmp = tempfile.mkdtemp(prefix="3dlib-render-")
        s = trimesh.creation.icosphere(subdivisions=5, radius=1.0)
        self.path = os.path.join(self.tmp, "hp.stl")
        s.export(self.path)
        self.faces = len(s.faces)

    def tearDown(self):
        uc._STYLE_NAME = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_mesh_under_the_hard_cap_is_never_strided(self):
        self.assertLess(self.faces, uc.HARD_CAP)
        out = os.path.join(self.tmp, "out.webp")
        ok, note = uc._render([self.path], out)
        self.assertTrue(ok)
        self.assertIsNone(note, "fell back to striding when it did not need to")
        self.assertGreater(os.path.getsize(out), 0)

    def test_an_oversized_mesh_is_really_reduced_not_strided(self):
        """The trimesh reduction call took `percent` first, so passing a face count
        positionally raised every time and the bare except hid it. This asserts the
        reduction actually happens rather than that something merely rendered."""
        import trimesh, numpy as np
        big = trimesh.creation.icosphere(subdivisions=7)
        self.assertGreater(len(big.faces), uc.RENDER_CAP)
        reduced = big.simplify_quadric_decimation(face_count=uc.RENDER_CAP)
        self.assertLessEqual(len(reduced.faces), uc.RENDER_CAP)
        p = os.path.join(self.tmp, "big.stl"); big.export(p)
        ok, note = uc._render([p], os.path.join(self.tmp, "big.webp"))
        self.assertTrue(ok)
        self.assertIsNone(note, "fell back to striding despite a working decimator")

    def make_kit(self, folder, placements, radius=0.5):
        """placements: (x, y, z, scale) per part, in one shared coordinate system."""
        import trimesh
        d = os.path.join(self.tmp, folder); os.makedirs(d, exist_ok=True)
        paths = []
        for i, (x, y, z, sc) in enumerate(placements):
            m = trimesh.creation.icosphere(subdivisions=2, radius=radius * sc)
            m.apply_translation([x, y, z])
            p = os.path.join(d, f"part{i}.stl"); m.export(p); paths.append(p)
        return paths

    def test_a_kit_laid_out_on_a_print_plate_draws_its_largest_part(self):
        """Parts authored where they sit on the bed are metres apart in model space.
        Concatenating them draws scattered specks, not the model."""
        plate = self.make_kit("plate", [(i % 3 * 4.0, i // 3 * 4.0, 0, 1 if i == 0 else 0.3)
                                        for i in range(9)])
        ok, note = uc._render(plate, os.path.join(self.tmp, "plate.webp"))
        self.assertTrue(ok)
        self.assertIn("laid out apart", note or "")

    def test_a_kit_that_actually_assembles_is_drawn_whole(self):
        asm = self.make_kit("asm", [(0, 0, 0, 1.0), (0, 0, 0.55, 0.45), (0, 0, -0.5, 0.9)])
        ok, note = uc._render(asm, os.path.join(self.tmp, "asm.webp"))
        self.assertTrue(ok)
        self.assertIsNone(note, "split a kit that fits together")

    def test_two_part_kits_are_never_split_up(self):
        pair = self.make_kit("pair", [(0, 0, 0, 1.0), (30.0, 0, 0, 0.9)])
        ok, note = uc._render(pair, os.path.join(self.tmp, "pair.webp"))
        self.assertTrue(ok)
        self.assertIsNone(note, "dropped half of a two-part kit")

    def test_the_spread_limit_is_configurable(self):
        self.assertEqual(uc._spread_limit(), uc.SPREAD_LIMIT)
        with mock.patch.object(uc, "rules", lambda: {"render_spread_limit": 9.9}):
            self.assertEqual(uc._spread_limit(), 9.9)
        with mock.patch.object(uc, "rules", lambda: {"render_spread_limit": "nonsense"}):
            self.assertEqual(uc._spread_limit(), uc.SPREAD_LIMIT)

    def test_each_style_actually_changes_the_picture(self):
        shots = {}
        for name in ("slate", "paper", "bronze"):
            uc._STYLE_NAME = name
            out = os.path.join(self.tmp, name + ".webp")
            ok, _ = uc._render([self.path], out)
            self.assertTrue(ok)
            with open(out, "rb") as fp: shots[name] = fp.read()
        self.assertEqual(len(set(shots.values())), 3, "styles produced identical images")

    def test_inconsistent_winding_still_renders(self):
        """Downloaded STLs are full of reversed faces; that must not black them out."""
        import trimesh, numpy as np
        m = trimesh.load(self.path, force="mesh")
        f = np.asarray(m.faces).copy()
        rng = np.random.default_rng(1); bad = rng.random(len(f)) < 0.4
        f[bad] = f[bad][:, ::-1]
        p = os.path.join(self.tmp, "scrambled.stl")
        trimesh.Trimesh(vertices=m.vertices, faces=f, process=False).export(p)
        ok, note = uc._render([p], os.path.join(self.tmp, "scrambled.webp"))
        self.assertTrue(ok)


class TestMenusMatchTheTool(unittest.TestCase):
    """LIBRARY.bat and library.sh are the same menu for two platforms. Nothing stops
    one gaining an option the other never gets, or either calling a flag that was
    renamed — so this checks both against the real argument parser."""

    BAT = os.path.join(ROOT, "LIBRARY.bat")
    SH = os.path.join(ROOT, "library.sh")

    def flags(self):
        return {o for a in uc.build_parser()._actions for o in a.option_strings}

    def invocations(self, path):
        """Every --flag either menu passes to the tool. The shell menu goes through
        a run() wrapper rather than naming the script on every line."""
        used = set()
        for line in read(path).splitlines():
            st = line.strip()
            if "update_catalog.py" in st:
                tail = st.split("update_catalog.py", 1)[1]
            elif st.startswith("run "):
                tail = st[4:]
            else:
                continue
            used.update(re.findall(r"--[a-z][a-z0-9-]*", tail))
        return used

    def options(self, path, pattern):
        return {m.group(1).strip().lower()
                for m in re.finditer(pattern, read(path), re.M)}

    def test_both_menus_exist_and_the_shell_one_is_executable(self):
        self.assertTrue(os.path.isfile(self.BAT))
        self.assertTrue(os.path.isfile(self.SH))
        if os.name != "nt":
            self.assertTrue(os.access(self.SH, os.X_OK), "library.sh is not executable")

    def test_every_flag_the_windows_menu_uses_really_exists(self):
        unknown = self.invocations(self.BAT) - self.flags()
        self.assertFalse(unknown, f"LIBRARY.bat calls flags that do not exist: {unknown}")

    def test_every_flag_the_shell_menu_uses_really_exists(self):
        unknown = self.invocations(self.SH) - self.flags()
        self.assertFalse(unknown, f"library.sh calls flags that do not exist: {unknown}")

    def test_the_two_menus_offer_the_same_things(self):
        bat = self.invocations(self.BAT)
        sh = self.invocations(self.SH)
        self.assertFalse(bat - sh, f"only on Windows: {bat - sh}")
        self.assertFalse(sh - bat, f"only in the shell menu: {sh - bat}")

    def test_the_numbered_choices_are_the_same_on_both(self):
        bat = self.options(self.BAT, r'if(?: /i)? "%c%"=="(\w+)"')   # letters use /i
        sh = self.options(self.SH, r"^\s{4}([0-9]+|[a-zA-Z]\|[a-zA-Z])\)")
        sh = {o.split("|")[0].lower() for o in sh}
        bat = {o.lower() for o in bat}
        self.assertTrue(bat, "found no choices in LIBRARY.bat")
        self.assertTrue(sh, "found no choices in library.sh")
        self.assertEqual(bat - {"q"}, sh - {"q"},
                         "the two menus no longer offer the same numbered options")

    @unittest.skipIf(os.name == "nt", "POSIX shell only")
    def test_the_shell_menu_parses_and_quits_cleanly(self):
        import subprocess
        chk = subprocess.run(["sh", "-n", self.SH], capture_output=True, text=True)
        self.assertEqual(chk.returncode, 0, chk.stderr)
        run = subprocess.run(["sh", self.SH], input="Q\n", capture_output=True,
                             text=True, timeout=30, cwd=ROOT)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("3D PRINT LIBRARY", run.stdout)


STEP_FIXTURE = os.path.join(ROOT, "tests", "fixtures", "bracket.step")
try:
    import cascadio            # noqa: F401
    HAVE_CAD = True
except Exception:
    HAVE_CAD = False


@unittest.skipUnless(HAVE_RENDER and HAVE_CAD, "needs trimesh and cascadio")
class TestCadFiles(unittest.TestCase):
    """CAD is boundary representation, not triangles, so it needs a kernel to
    tessellate. cascadio is OpenCascade's reader as a half-megabyte wheel."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="3dlib-cad-")
        self.assertTrue(os.path.isfile(STEP_FIXTURE), "the STEP fixture is missing")

    def tearDown(self):
        uc._STYLE_NAME = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_step_file_renders(self):
        out = os.path.join(self.tmp, "cad.webp")
        ok, note = uc._render([STEP_FIXTURE], out)
        self.assertTrue(ok, note)
        self.assertGreater(os.path.getsize(out), 0)

    def test_a_step_file_inside_a_zip_renders(self):
        import zipfile
        z = os.path.join(self.tmp, "cad.zip")
        with zipfile.ZipFile(z, "w") as zf:
            zf.write(STEP_FIXTURE, "parts/bracket.step")
        status, detail, extra = uc._zip_thumb(z, os.path.join(self.tmp, "z.webp"))
        self.assertEqual(status, "mesh", detail)
        self.assertEqual(extra["part_count"], 1)

    def test_a_file_that_only_pretends_to_be_step_fails_with_a_reason(self):
        bogus = os.path.join(self.tmp, "not-really.step")
        with open(bogus, "wb") as fp: fp.write(b"this is not a STEP file")
        ok, why = uc._render([bogus], os.path.join(self.tmp, "x.webp"))
        self.assertFalse(ok)
        self.assertTrue(why)

    def test_cad_is_no_longer_listed_as_unrenderable(self):
        self.assertEqual(uc._unrenderable(), set())
        with mock.patch.object(uc, "_cad_reader", lambda: None):
            self.assertEqual(uc._unrenderable(), uc.CAD_EXT)


class TestRequirements(unittest.TestCase):
    """A package the tool needs but does not declare is found only by hitting the
    failure it causes. networkx and charset-normalizer were both discovered that
    way, on a real 2,600-item run, as bare ModuleNotFoundErrors."""

    REQ = os.path.join(ROOT, "requirements.txt")

    # import name -> the name pip installs it under
    NEEDED = {"numpy": "numpy", "trimesh": "trimesh", "matplotlib": "matplotlib",
              "PIL": "pillow", "networkx": "networkx",
              "charset_normalizer": "charset-normalizer",
              "fast_simplification": "fast-simplification", "cascadio": "cascadio"}

    def lines(self):
        return [l.strip() for l in read(self.REQ).splitlines()
                if l.strip() and not l.strip().startswith("#")]

    def declared(self):
        return {re.split(r"[<>=;\[ ]", l, 1)[0].strip().lower() for l in self.lines()}

    def test_everything_the_tool_imports_is_declared(self):
        missing = {mod: dist for mod, dist in self.NEEDED.items()
                   if dist.lower() not in self.declared()}
        self.assertFalse(missing, f"imported at runtime but not in requirements.txt: {missing}")

    def test_no_requirement_uses_a_substring_platform_test(self):
        """`platform_machine in "x86_64 AMD64"` is a SUBSTRING test: 32-bit Windows
        reports x86, which matches inside x86_64, and pip would then try to build
        from source on a machine with no wheel."""
        for line in self.lines():
            self.assertNotRegex(line, r'platform_machine\s+in\s',
                                f"substring platform test in: {line}")

    def test_every_line_is_a_valid_requirement(self):
        try:
            from packaging.requirements import Requirement
        except ImportError:
            self.skipTest("packaging not available")
        for line in self.lines():
            Requirement(line)          # raises if the line or its marker is malformed

    def test_the_cad_reader_is_installed_where_a_wheel_exists(self):
        try:
            from packaging.requirements import Requirement
        except ImportError:
            self.skipTest("packaging not available")
        cad = [Requirement(l) for l in self.lines()
               if l.lower().startswith("cascadio")]
        self.assertEqual(len(cad), 1, "cascadio is not declared exactly once")
        marker = cad[0].marker
        self.assertTrue(marker, "cascadio has no platform marker")
        for machine, expected in (("AMD64", True), ("x86_64", True), ("arm64", True),
                                  ("aarch64", True), ("x86", False), ("i686", False)):
            got = marker.evaluate({"platform_machine": machine, "sys_platform": "win32"})
            self.assertEqual(got, expected, f"{machine} should be {expected}")


class TestTicker(unittest.TestCase):

    def test_it_fires_on_a_timer_with_nothing_happening(self):
        calls = []
        t = uc._Ticker(lambda: calls.append(1), every=0.02).start()
        time.sleep(0.2)
        t.stop()
        self.assertGreaterEqual(len(calls), 3)
        after = len(calls)
        time.sleep(0.1)
        self.assertEqual(len(calls), after, "kept firing after stop()")

    def test_a_broken_progress_line_cannot_kill_the_run(self):
        def boom(): raise RuntimeError("no")
        t = uc._Ticker(boom, every=0.02).start()
        time.sleep(0.1)
        t.stop()          # the thread must still be alive to be stopped cleanly
        self.assertFalse(t._thread.is_alive())


class TestNothingFailsSilently(LibraryCase):

    def test_every_project_ends_with_a_status_that_explains_itself(self):
        out = self.scan()
        self.assertIn("Thumbnails, by outcome:", out)
        for p in self.projects():
            st = p.get("thumb_status")
            self.assertIn(st, uc.STATUS_LABEL, f"unknown status {st!r}")
            # "pending" means nobody ever said what happened. That was the bug.
            self.assertNotEqual(st, "pending", f"{p['path']} was skipped without a reason")
            if st not in uc.OK_STATUS:
                self.assertTrue(p.get("thumb_error"), f"{st} recorded with no reason")

    def test_a_missing_render_library_is_announced(self):
        with mock.patch.object(uc, "_mesh_libs_missing", return_value="trimesh"):
            out = self.scan()
        self.assertIn("trimesh is not installed", out)

    def test_too_big_is_recorded_rather_than_quietly_skipped(self):
        self.scan("--max-mb", "0.000001")
        self.assertTrue(any(p["thumb_status"] == "too_big" for p in self.projects()))

    def test_a_step_file_nothing_can_read_says_how_to_fix_it(self):
        """Without a CAD reader installed, .step is named as unsupported and the
        message says what to install — not left as an unexplained blank."""
        self.add_kit("Bracket CAD", ["bracket.step"])
        with mock.patch.object(uc, "_cad_reader", lambda: None):
            self.scan()
        cad = [p for p in self.projects() if "Bracket" in p["path"]][0]
        self.assertEqual(cad["thumb_status"], "unsupported")
        self.assertIn(".step", cad["thumb_error"])
        self.assertIn("cascadio", cad["thumb_error"])


class TestCloudSafety(LibraryCase):

    def test_online_only_parts_are_never_handed_to_the_renderer(self):
        """The whole point of the tool: asking about a file is safe, opening one is
        not. A kit with one downloaded part and two placeholders must render from
        the one, and must not so much as name the other two."""
        self.scan()
        titan = [p for p in self.projects() if "Warhound" in p["path"]][0]
        cloudy = {os.path.join(titan["path"], f) for f in ("leg_left.stl", "leg_right.stl")}
        handed = []

        def fake_render(paths, out_path, timeout=25):
            handed.extend(paths); return (True, None)

        with mock.patch.object(uc, "_hydrated", lambda p: p not in cloudy), \
             mock.patch.object(uc, "_render", fake_render), \
             mock.patch.object(uc, "_shell_thumb", return_value=False):
            status, detail, _x = uc._thumb_for(titan, os.path.join(self.lib, "t.webp"), engine="mesh")

        self.assertEqual(status, "mesh")
        self.assertEqual(handed, [os.path.join(titan["path"], "body.stl")])
        self.assertIn("1 of 3", detail)

    def test_a_fully_cloud_kit_is_reported_not_attempted(self):
        self.scan()
        titan = [p for p in self.projects() if "Warhound" in p["path"]][0]
        with mock.patch.object(uc, "_hydrated", lambda p: False), \
             mock.patch.object(uc, "_render", side_effect=AssertionError("must not render")):
            status, detail, _x = uc._thumb_for(titan, os.path.join(self.lib, "t.webp"), engine="mesh")
        self.assertEqual(status, "cloud_only")
        self.assertIn("3 part(s)", detail)


class TestCatalogSurvivesInterruption(LibraryCase):

    def test_a_failed_write_leaves_the_previous_file_intact(self):
        self.scan()
        path = os.path.join(self.lib, "catalog.json")
        before = read(path)
        with mock.patch("os.replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                uc.atomic_write(path, "half a file")
        self.assertEqual(read(path), before)
        self.assertFalse([f for f in os.listdir(self.lib) if f.startswith(".tmp-")])

    def test_an_interrupted_thumbnail_write_leaves_no_half_file(self):
        """render_missing treats the existence of a .webp as proof it is done, so a
        truncated one would be accepted as finished for ever."""
        from PIL import Image
        target = os.path.join(self.lib, "thumbnails", "half.webp")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with mock.patch("os.replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                uc._save_webp(Image.new("RGB", (64, 64), (10, 10, 10)), target)
        self.assertFalse(os.path.exists(target), "a partial thumbnail was left behind")
        self.assertFalse([f for f in os.listdir(os.path.dirname(target))
                          if f.startswith(".tmp-")])

    def test_a_corrupt_catalog_is_explained_and_can_be_restored(self):
        self.scan()
        self.run_cli("--rebuild-views")          # leaves a backup of the good file
        good = read(os.path.join(self.lib, "catalog.json"))
        with open(os.path.join(self.lib, "catalog.json"), "w", encoding="utf-8") as fp:
            fp.write(good[:200])                 # what an interrupted write looked like

        out = self.run_cli("--diagnose", expect_exit=2)
        self.assertIn("could not be read", out)
        self.assertIn("--restore-backup", out)

        self.run_cli("--restore-backup")
        self.assertEqual(read(os.path.join(self.lib, "catalog.json")), good)
        self.run_cli("--rebuild-views")

    def test_the_catalog_is_backed_up_before_a_thumbnail_run(self):
        self.scan()
        self.run_cli("--thumbs-only", "--jobs", "1")
        self.assertTrue(uc.list_backups(), "the long-running option left no backup")


class TestReclassify(LibraryCase):

    def test_dry_run_reports_changes_and_writes_nothing(self):
        self.scan()
        before = read(os.path.join(self.lib, "catalog.json"))
        with open(os.path.join(self.lib, "rules.local.json"), "w", encoding="utf-8") as fp:
            json.dump({"categories": {"Titans": "warhound|titan"},
                       "default_category": "Everything else"}, fp)
        importlib.reload(uc)
        out = self.run_cli("--reclassify", "--dry-run")
        self.assertIn("dry run", out)
        self.assertEqual(read(os.path.join(self.lib, "catalog.json")), before)

    def test_editing_the_rules_reclassifies_without_a_rescan(self):
        self.scan()
        with open(os.path.join(self.lib, "rules.local.json"), "w", encoding="utf-8") as fp:
            # factions and the wargaming keywords are switched off explicitly:
            # rules.local.json lays OVER rules.json, so anything not named here is
            # still the shipped rule, and a shipped faction match would otherwise
            # send these into the wargaming category before categories are consulted
            json.dump({"categories": {"Titans": "warhound|titan"},
                       "default_category": "Everything else",
                       "factions": {}, "wargaming_keywords": ""}, fp)
        importlib.reload(uc)
        self.run_cli("--reclassify")
        cats = {p["name"]: p["category"] for p in self.projects()}
        self.assertEqual(cats["files"], "Titans")           # the Warhound kit
        self.assertEqual(cats["Ork Nob"], "Everything else")
        self.assertTrue(all("faction:" not in t for p in self.projects() for t in p["tags"]))

    def test_filenames_classify_a_folder_whose_name_says_nothing(self):
        """The whole point of looking inside: the folder is called KitA, but every
        part in it is a warhound titan."""
        self.add_kit("KitA", ["warhound_titan_body.stl", "warhound_titan_leg.stl"])
        self.scan()
        kit = [p for p in self.projects() if p["path"].endswith("KitA")][0]
        self.assertEqual(kit["faction"], "Knights & Titans")
        self.assertIn("type:titan", kit["tags"])

    def test_a_filename_cannot_overrule_a_folder_that_already_matched(self):
        """A part called wall_mount.stl must not turn an Ork kit into printer bits."""
        self.add_kit("Ork Boyz", ["ork_boy.stl", "wall_mount.stl"])
        self.scan()
        kit = [p for p in self.projects() if p["path"].endswith("Ork Boyz")][0]
        self.assertEqual(kit["faction"], "Orks")
        self.assertEqual(kit["category"], "Warhammer 40k / wargaming")

    def test_filename_matching_can_be_switched_off(self):
        self.add_kit("KitA", ["warhound_titan_body.stl"])
        with open(os.path.join(self.lib, "rules.local.json"), "w", encoding="utf-8") as fp:
            rules = json.loads(read(os.path.join(self.lib, "rules.json")))
            rules["match_filenames"] = False
            json.dump(rules, fp)
        importlib.reload(uc)
        self.scan()
        kit = [p for p in self.projects() if p["path"].endswith("KitA")][0]
        self.assertIsNone(kit["faction"])

    def test_unmatched_lists_names_but_never_absolute_paths(self):
        self.add_kit("Something Nobody Named", ["widget.stl"])
        self.scan()
        out = self.run_cli("--unmatched")
        self.assertIn("Something Nobody Named", out)
        self.assertIn("widget.stl", out)
        self.assertNotIn(self.models, out, "--unmatched leaked an absolute path")

    def test_reclassifying_against_unchanged_rules_is_a_no_op(self):
        self.scan()
        out = self.run_cli("--reclassify")
        self.assertIn("nothing to do", out)


class TestPackedArchives(LibraryCase):
    """Archives were indexed by filename alone, so every one was a blank card."""

    def make_zip(self, name, members):
        import zipfile
        p = os.path.join(self.models, name)
        with zipfile.ZipFile(p, "w") as zf:
            for member, data in members.items(): zf.writestr(member, data)
        return p

    def stl_bytes(self, tris=4):
        import io as _io
        buf = os.path.join(self.tmp, "tmp.stl"); write_stl(buf, tris)
        with open(buf, "rb") as fp: return fp.read()

    def png_bytes(self, colour=(200, 40, 40)):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("needs pillow")
        import io as _io
        b = _io.BytesIO(); Image.new("RGB", (64, 64), colour).save(b, "PNG")
        return b.getvalue()

    def test_a_preview_image_inside_the_zip_becomes_the_card(self):
        self.make_zip("Kit.zip", {"parts/body.stl": self.stl_bytes(),
                                  "renders/preview.png": self.png_bytes()})
        self.scan()
        z = [i for i in self.catalog()["items"] if i["type"] == "archive"][0]
        self.assertEqual(z["thumb_status"], "reused")
        self.assertIn("inside the zip", z["thumb_error"])
        self.assertTrue(os.path.exists(os.path.join(self.lib, "thumbnails", z["id"] + ".webp")))

    def test_the_contents_are_recorded_even_without_a_picture(self):
        self.make_zip("Bits.zip", {"a.stl": self.stl_bytes(), "b.stl": self.stl_bytes(6),
                                   "c.obj": b"o x\n", "notes.txt": b"hi"})
        self.scan()
        z = [i for i in self.catalog()["items"] if i["type"] == "archive"][0]
        self.assertEqual(z["part_count"], 3)
        self.assertIn("stl", z["formats"])
        self.assertIn("zip", z["formats"])
        self.assertIn("a.stl", z["model_files"])
        self.assertNotIn("notes.txt", z["model_files"])

    def test_mac_junk_inside_a_zip_is_ignored(self):
        self.make_zip("Mac.zip", {"real.stl": self.stl_bytes(),
                                  "__MACOSX/._real.stl": b"junk",
                                  "._sneaky.stl": b"junk"})
        self.scan()
        z = [i for i in self.catalog()["items"] if i["type"] == "archive"][0]
        self.assertEqual(z["model_files"], ["real.stl"])
        self.assertEqual(z["part_count"], 1)

    def test_a_corrupt_archive_says_so_instead_of_crashing(self):
        with open(os.path.join(self.models, "Broken.zip"), "wb") as fp:
            fp.write(b"this is not a zip file at all")
        self.scan()
        z = [i for i in self.catalog()["items"] if i["type"] == "archive"][0]
        self.assertEqual(z["thumb_status"], "packed")
        self.assertIn("not a readable zip", z["thumb_error"])

    def test_other_archive_formats_are_named_not_silently_skipped(self):
        with open(os.path.join(self.models, "Old.rar"), "wb") as fp: fp.write(b"Rar!\x1a\x07\x00")
        self.scan()
        z = [i for i in self.catalog()["items"] if i["type"] == "archive"][0]
        self.assertEqual(z["thumb_status"], "unsupported")
        self.assertIn(".rar", z["thumb_error"])

    def test_an_online_only_archive_is_never_opened(self):
        self.make_zip("Cloud.zip", {"a.stl": self.stl_bytes()})
        self.scan()
        z = [i for i in self.catalog()["items"] if i["type"] == "archive"][0]
        import zipfile
        with mock.patch.object(uc, "_hydrated", lambda p: False), \
             mock.patch.object(zipfile, "ZipFile", side_effect=AssertionError("opened it")):
            st, detail, extra = uc._thumb_for(z, os.path.join(self.lib, "x.webp"))
        self.assertEqual(st, "cloud_only")


class TestDuplicates(LibraryCase):

    def test_identical_kits_are_grouped_and_written_to_the_csv(self):
        # three copies of one kit, the way Windows names a repeated download
        for name in ("Tyrant Guard", "Tyrant Guard (1)", "Tyrant Guard (2)"):
            self.add_kit(name, ["body.stl", "arm.stl"])
        self.scan()
        out = self.run_cli("--duplicates")
        self.assertIn("3 copies", out)
        self.assertIn("Nothing was deleted", out)
        for name in ("Tyrant Guard", "Tyrant Guard (1)", "Tyrant Guard (2)"):
            self.assertIn(name, out)
        import csv as _csv
        with open(os.path.join(self.lib, "potential_duplicates.csv"), encoding="utf-8") as fp:
            rows = list(_csv.DictReader(fp))
        self.assertEqual(len([r for r in rows if r["match"] == "exact"]), 3)
        self.assertTrue(all(r["group"] == rows[0]["group"] for r in rows if r["match"] == "exact"))

    def test_reporting_duplicates_removes_nothing(self):
        for name in ("Kit A", "Kit A copy"):
            self.add_kit(name, ["body.stl"])
        self.scan()
        before = len(self.projects())
        self.run_cli("--duplicates")
        self.assertEqual(len(self.projects()), before, "the report changed the catalog")
        for name in ("Kit A", "Kit A copy"):
            self.assertTrue(os.path.isdir(os.path.join(self.models, name)),
                            "the report touched a source folder")

    def test_the_same_kit_at_a_different_size_is_reported_separately(self):
        self.add_kit("Bust", ["head.stl"])
        d = os.path.join(self.models, "Bust hires"); os.makedirs(d)
        write_stl(os.path.join(d, "head.stl"), tris=40)      # same name, bigger file
        self.scan()
        out = self.run_cli("--duplicates")
        self.assertIn("SAME FILENAMES but different", out)

    def test_distinct_kits_are_not_reported(self):
        self.scan()
        out = self.run_cli("--duplicates")
        self.assertIn("No duplicate kits found", out)


class TestThemeChoice(LibraryCase):
    """A theme a user can only set by reading the README is a theme most users
    never find. This is the menu path: pick, look, keep."""

    def local_rules(self):
        return json.loads(read(os.path.join(self.lib, "rules.local.json")))

    def test_setting_a_scheme_writes_it_where_the_next_run_will_read_it(self):
        out = self.run_cli("--set-style", "blueprint")
        self.assertIn("blueprint", out)
        self.assertIn("--thumbs-only --force", out)          # says how to apply it
        self.assertEqual(self.local_rules()["thumbnail_style"]["preset"], "blueprint")
        importlib.reload(uc)
        self.assertEqual(uc._style()["background_hex"],
                         uc.THUMB_PRESETS["blueprint"]["background"])

    def test_a_theme_choice_does_not_switch_off_the_classification_rules(self):
        """rules.local.json lays over rules.json. It used to replace it outright, so
        a local file holding nothing but a colour would have silently disabled every
        category and faction rule — and nothing would have said so."""
        self.scan()
        before = {p["name"]: p["category"] for p in self.projects()}
        self.assertIn("Warhammer 40k / wargaming", before.values())
        self.run_cli("--set-style", "bronze")
        importlib.reload(uc)
        self.run_cli("--reclassify")
        after = {p["name"]: p["category"] for p in self.projects()}
        self.assertEqual(before, after, "picking a colour changed how things classify")

    def test_it_keeps_whatever_else_is_already_in_the_local_file(self):
        with open(os.path.join(self.lib, "rules.local.json"), "w", encoding="utf-8") as fp:
            json.dump({"default_category": "Mine", "thumbnail_style": {"ambient": 0.9}}, fp)
        self.run_cli("--set-style", "mono")
        local = self.local_rules()
        self.assertEqual(local["default_category"], "Mine")
        self.assertEqual(local["thumbnail_style"]["ambient"], 0.9)   # kept
        self.assertEqual(local["thumbnail_style"]["preset"], "mono")  # and added

    def test_a_broken_local_file_is_not_overwritten_blind(self):
        path = os.path.join(self.lib, "rules.local.json")
        with open(path, "w", encoding="utf-8") as fp: fp.write("{ this is not json")
        out = self.run_cli("--set-style", "paper")
        self.assertIn("not going to overwrite it blind", out)
        self.assertEqual(read(path), "{ this is not json")

    def test_an_unknown_scheme_is_refused(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.run_cli("--set-style", "chartreuse", expect_exit=2)   # argparse says no
        self.assertIn("invalid choice", err.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.lib, "rules.local.json")),
                         "a rejected name still wrote a rules file")


class TestRelocateAndPrune(LibraryCase):

    def move_titan(self):
        src = os.path.join(self.models, "Warhound Titan")
        dst = os.path.join(self.models, "Reorganised", "Warhound Titan")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        return os.path.join(dst, "files")

    def fake_thumb(self, item_id):
        os.makedirs(os.path.join(self.lib, "thumbnails"), exist_ok=True)
        p = os.path.join(self.lib, "thumbnails", item_id + ".webp")
        with open(p, "wb") as fp: fp.write(b"not really a webp")
        return p

    def test_a_moved_kit_keeps_its_thumbnail_and_its_id_still_matches_its_path(self):
        self.scan()
        titan = [p for p in self.projects() if "Warhound" in p["path"]][0]
        self.fake_thumb(titan["id"])
        new_dir = self.move_titan()
        self.scan()                                  # sees the new location
        self.run_cli("--relocate")

        rows = [p for p in self.projects() if "Warhound" in p["path"]]
        self.assertEqual(len(rows), 1, "a move should not leave two entries")
        moved = rows[0]
        self.assertEqual(moved["path"], new_dir)
        self.assertEqual(moved["id"], uc.stable_id(new_dir),
                         "the id must match the path, or the next scan mints a twin")
        self.assertTrue(os.path.exists(os.path.join(self.lib, "thumbnails", moved["id"] + ".webp")),
                        "the thumbnail should have moved with the entry, not been re-rendered")

    def test_relocate_then_rescan_does_not_duplicate_the_kit(self):
        self.scan()
        self.move_titan()
        self.scan()
        self.run_cli("--relocate")
        self.run_cli("--rescan-all", "--jobs", "1")
        rows = [p for p in self.projects() if "Warhound" in p["path"]]
        self.assertEqual(len(rows), 1, "rescanning after a relocate duplicated the entry")

    def test_prune_removes_entries_whose_files_really_are_gone(self):
        self.scan()
        shutil.rmtree(os.path.join(self.models, "Ork Nob"))
        out = self.run_cli("--relocate", "--prune")
        self.assertIn("pruned 1", out)
        self.assertEqual(len(self.projects()), 1)

    def test_prune_spares_everything_when_the_drive_is_not_plugged_in(self):
        self.scan()
        offline = self.models + "-offline"
        shutil.move(self.models, offline)            # the whole source root vanishes
        try:
            out = self.run_cli("--relocate", "--prune")
            self.assertIn("pruned 0", out)
            self.assertIn("not", out)
            self.assertEqual(len(self.projects()), 2, "an unplugged drive emptied the catalog")
        finally:
            shutil.move(offline, self.models)


class TestSources(LibraryCase):

    def test_add_list_and_remove_a_folder(self):
        out = self.run_cli("--add-source", self.models)
        self.assertIn("Added:", out)
        self.assertEqual(uc.load_sources(), [self.models])
        self.assertIn(self.models, self.run_cli("--sources"))
        self.run_cli("--remove-source", self.models)
        self.assertEqual(uc.load_sources(), [])

    def test_adding_a_folder_that_does_not_exist_is_refused(self):
        out = self.run_cli("--add-source", os.path.join(self.tmp, "nope"))
        self.assertIn("Not a folder", out)
        self.assertEqual(uc.load_sources(), [])

    def test_rescan_all_with_no_sources_says_so(self):
        out = self.run_cli("--rescan-all")
        self.assertIn("No folders listed", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
