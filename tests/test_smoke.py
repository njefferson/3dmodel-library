"""Smoke tests for update_catalog.py.

Runs anywhere Python does — no trimesh, no matplotlib, no Windows. The point is
not to check render quality; it is to check that a catalog can be built from a
folder of files, that every view it writes is valid, that nothing fails silently,
and that the paths nobody had ever executed do what they claim.

    python -m unittest discover -s tests -v

The library the tests build lives in a temp folder (LIBRARY_DIR), so nothing is
written next to the script and no real catalog is ever touched.
"""
import contextlib, importlib, io, json, os, shutil, struct, sys, tempfile, unittest
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

    def test_step_files_are_catalogued_and_reported_as_unconvertible(self):
        self.add_kit("Bracket CAD", ["bracket.step"])
        self.scan()
        cad = [p for p in self.projects() if "Bracket" in p["path"]][0]
        self.assertEqual(cad["thumb_status"], "unsupported")
        self.assertIn("step", cad["thumb_error"])


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
            status, detail = uc._thumb_for(titan, os.path.join(self.lib, "t.webp"), engine="mesh")

        self.assertEqual(status, "mesh")
        self.assertEqual(handed, [os.path.join(titan["path"], "body.stl")])
        self.assertIn("1 of 3", detail)

    def test_a_fully_cloud_kit_is_reported_not_attempted(self):
        self.scan()
        titan = [p for p in self.projects() if "Warhound" in p["path"]][0]
        with mock.patch.object(uc, "_hydrated", lambda p: False), \
             mock.patch.object(uc, "_render", side_effect=AssertionError("must not render")):
            status, detail = uc._thumb_for(titan, os.path.join(self.lib, "t.webp"), engine="mesh")
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
            json.dump({"categories": {"Titans": "warhound|titan"},
                       "default_category": "Everything else"}, fp)
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
