"""Core logic tests for Data Kitsu Tools (no GUI needed)."""
import sys, tempfile, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import data_kitsu_tools as dkt  # noqa: E402


def make(p: Path, n, start=1, size=2048):
    p.mkdir(parents=True, exist_ok=True)
    for i in range(start, start + n):
        (p / f"{p.name}_{i:06d}.exr").write_bytes(b"x" * size)


def build_tree(root: Path):
    # LOCAL layout: REEL2/SC07/<shot>_PL01 and _PL02 (variant folders)
    make(root / "REEL2/SC07/HHCC_SC07_010_PL01", 10)
    make(root / "REEL2/SC07/HHCC_SC07_010_PL02", 8)          # short, mismatch
    # Shot with a gap (frames 1..10 except 5)
    d = root / "REEL2/SC07/HHCC_SC07_020"
    make(d, 4, start=1)
    make(d, 5, start=6)                                       # 6..10
    # Prefix trap: 010 must not match 0100
    make(root / "REEL2/SC09/HHCC_SC09_0100", 3)
    # Publish layout: container with versions + system files
    base = root / "R1_SC01/HHCC_R1_SC01_030"
    for v in (1, 5, 6):
        make(base / f"HHCC_R1_SC01_030_Tom_comp_v{v:02d}", 6)
    (base / "._HHCC_R1_SC01_030_Tom_comp_v06").write_bytes(b"junk")
    (base / ".DS_Store").write_bytes(b"junk")
    # Empty shot folder (no files) -> yellow case
    (root / "REEL2/SC07/HHCC_SC07_030").mkdir(parents=True)


def run_tests():
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "src"; dst = tmp / "dst"
    build_tree(src)
    names = ["HHCC_SC07_010", "HHCC_SC07_020", "HHCC_SC09_0100", "HHCC_SC07_030", "HHCC_NOPE_001",
             "HHCC_R1_SC01_030"]
    matched = dkt.scan_source(src, names)

    # --- scan / prefix matching
    assert len(matched["HHCC_SC07_010"]) == 2, matched["HHCC_SC07_010"]
    assert len(matched["HHCC_SC09_0100"]) == 1
    assert matched["HHCC_NOPE_001"] == []

    # --- LOCAL analysis
    r = dkt.analyze_shot("HHCC_SC07_010", matched["HHCC_SC07_010"], dkt.MODE_LOCAL, (".exr",), True)
    assert r.found == 10, r.found                     # best variant has 10 files
    assert sorted(u.label for u in r.units) == ["PL01", "PL02"], [u.label for u in r.units]
    assert len(r.units[0].copy_files) + len(r.units[1].copy_files) == 18

    r2 = dkt.analyze_shot("HHCC_SC07_020", matched["HHCC_SC07_020"], dkt.MODE_LOCAL, (), True)
    assert r2.found == 9 and r2.missing == [5], (r2.found, r2.missing)

    r3 = dkt.analyze_shot("HHCC_SC07_030", matched["HHCC_SC07_030"], dkt.MODE_LOCAL, (), True)
    assert r3.found == 0 and r3.units and r3.units[0].found == 0

    # --- PUBLISH picks the highest version, ignores system files
    p = dkt.analyze_shot("HHCC_R1_SC01_030", matched["HHCC_R1_SC01_030"], dkt.MODE_PUBLISH,
                         (".exr",), True)
    assert p.version_text == "v06", p.version_text
    assert p.found == 6 and len(p.units[0].copy_files) == 6
    assert all("DS_Store" not in f.name and not f.name.startswith("._") for f in p.units[0].copy_files)

    # --- copy jobs keep the structure (LOCAL) and flatten (PUBLISH)
    shot = dkt.Shot("HHCC_SC07_010", 10, scan=r)
    jobs = dkt.build_jobs(shot, dkt.MODE_LOCAL, src, dst, "")
    assert all(str(d).startswith(str(dst / "REEL2/SC07")) for _, d in jobs)
    shot_p = dkt.Shot("HHCC_R1_SC01_030", 6, scan=p)
    jobs_p = dkt.build_jobs(shot_p, dkt.MODE_PUBLISH, src, dst, "261118")
    assert all(d.parent == dst / "261118" / "HHCC_R1_SC01_030_Tom_comp_v06" for _, d in jobs_p)

    # --- copy: skip mode keeps existing files, overwrite replaces them
    sample_src, sample_dst = jobs_p[0]
    ev, sev = threading.Event(), threading.Event()
    assert dkt.copy_file(sample_src, sample_dst, False, ev, sev) == "copied"
    # a short (incomplete) file is copied again, a complete one is skipped
    sample_dst.write_bytes(b"old")
    assert dkt.copy_file(sample_src, sample_dst, False, ev, sev) == "copied"
    assert sample_dst.stat().st_size == sample_src.stat().st_size
    assert dkt.copy_file(sample_src, sample_dst, False, ev, sev) == "skipped"
    sample_dst.write_bytes(b"x" * sample_src.stat().st_size)
    assert dkt.copy_file(sample_src, sample_dst, False, ev, sev) == "skipped"
    sample_dst.write_bytes(b"old")
    assert dkt.copy_file(sample_src, sample_dst, True, ev, sev) == "copied"
    assert sample_dst.stat().st_size == sample_src.stat().st_size

    # --- stop leaves no .part file behind
    stop = threading.Event(); stop.set()
    big = dst / "big.exr"
    assert dkt.copy_file(sample_src, big, True, ev, stop) == "stopped"
    assert not big.exists() and not big.with_name("big.exr.part").exists()

    # --- full worker run (sync) with skip mode, then a second run is all skipped
    plans = [(0, 10, jobs, False)]
    w = dkt.CopyWorker(plans)
    w.run()
    assert all(d.exists() for _, d in jobs), "worker did not copy all files"
    w2 = dkt.CopyWorker([(0, 10, jobs, False)])
    got = []
    w2.shot_finished.connect(lambda i, res: got.append(res))  # direct call, runs in this thread
    w2.run()
    assert got == ["done"], got

    # --- CSV loader handles ';' separator, quotes and multi-line cells
    csv_path = tmp / "shots.csv"
    csv_path.write_text('"Sequence";"Name";"Description";"Frames"\n'
                        '"R4";"HHCC_SC13_010";"Thêm sương\nkhói";"57"\n'
                        '"R4";"HHCC_SC13_0100";"x";"31"\n'
                        '"R4";"HHCC_SC13_010";"dup";"57"\n', encoding="utf-8")
    shots = dkt.load_csv(csv_path)
    assert [(s.name, s.frames) for s in shots] == [("HHCC_SC13_010", 57), ("HHCC_SC13_0100", 31)], shots

    test_naming_rules()
    test_reel_tag_and_publish()
    print("ALL CORE TESTS PASSED")


def test_naming_rules():
    """Naming database rules: reel tag, zeros, letters, decimals, hyphen variants, longest match."""
    names = ["HHCC_SC07_010", "HHCC_SC07_010_PL01"]
    assert dkt.ShotIndex(names).match_folder("HHCC_SC07_010_PL01_v02") == ["HHCC_SC07_010_PL01"]
    assert dkt.ShotIndex(names).match_folder("HHCC_SC07_010_PL02") == ["HHCC_SC07_010"]
    assert dkt.ShotIndex(names).match_folder("HHCC_SC07_0100") == []
    # letters: CT_R1_01A is a different shot from CT_R1_01, and CT_R4_01_A is the same as CT_R1_01A
    assert dkt.folder_matches("CT_R4_01_A_PL01", "CT_R1_01A")
    assert not dkt.folder_matches("CT_R1_01A", "CT_R1_01")
    assert not dkt.folder_matches("CT_R1_01_B", "CT_R1_01A")
    # decimals
    assert dkt.folder_matches("MAXO_R1_0043.5_v02", "MAXO_R1_0043.5")
    assert not dkt.folder_matches("MAXO_R1_0043.5", "MAXO_R1_0043")
    # hyphen variants: 010-add02 is its own shot, 010 alone must not take it
    hy = dkt.ShotIndex(["R1_SC03_010", "R1_SC03_010-add02"])
    assert hy.match_folder("R1_SC03_010-add02") == ["R1_SC03_010-add02"]
    assert hy.match_folder("R1_SC03_010") == ["R1_SC03_010"]
    # unpadded numbers
    assert dkt.folder_matches("THO_R1_100_PL02", "THO_R1_0100") is True
    # labels for the Version column
    assert dkt.unit_label(Path("HHCC_SC07_010-add02")) == "add02"
    assert dkt.unit_label(Path("HHCC_SC07_010_PL02")) == "PL02"
    assert dkt.unit_label(Path("HHCC_SC07_010_Tom_comp_v03")) == "v03"
    # frames missing: no mismatch colour, count still shown
    assert dkt.unit_style(3, None) == {}
    assert dkt.frames_text(None) == "?"

    tmp = Path(tempfile.mkdtemp())
    csv_path = tmp / "a_o.csv"
    csv_path.write_text('"Sequence";"Name";"Description"\n"R1_SC03";"010";""\n'
                        '"R1_SC03";"010-add02";""\n"SKIN_R3";"R3_skin_01";""\n',
                        encoding="utf-8")
    shots = dkt.load_csv(csv_path)
    assert [(s.name, s.seq, s.frames) for s in shots] == [
        ("R1_SC03_010", "R1_SC03", None),
        ("R1_SC03_010-add02", "R1_SC03", None),
        ("R3_skin_01", "SKIN_R3", None)], shots

    csv2 = tmp / "chuyen_tinh.csv"
    csv2.write_text('"Sequence";"Name";"FRAME"\n"REEL01";"CT_R1_01A";"26"\n', encoding="utf-8")
    shots2 = dkt.load_csv(csv2)
    assert [(s.name, s.seq, s.frames) for s in shots2] == [("CT_R1_01A", "REEL01", 26)]

    # find_first_folder respects the longest-match rule when all shots are passed
    src = tmp / "src"
    make(src / "R1_SC07/HHCC_SC07_010_PL01", 2)
    make(src / "R1_SC07/HHCC_SC07_010", 2)
    got = dkt.find_first_folder(src, "HHCC_SC07_010", names)
    assert got is not None and got.name == "HHCC_SC07_010", got
    assert dkt.find_first_folder(src, "HHCC_SC07_010", ["HHCC_SC07_010"]) is not None


def test_reel_tag_and_publish():
    """Folders like HHCC_R1_SC04_020_Tom_comp_v03 must match the CSV name HHCC_SC04_020."""
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "src"
    base = src / "FILM/TEASER/FILM/R1_SC04"
    for v in (1, 2, 3):
        make(base / "HHCC_R1_SC04_020" / f"HHCC_R1_SC04_020_Tom_comp_v{v:02d}", 5)
    make(base / "HHCC_SC04_010" / "HHCC_SC04_010_Tom_comp_v01", 4)
    names = ["HHCC_SC04_020", "HHCC_SC04_010", "HHCC_SC04_0200"]
    matched = dkt.scan_source(src, names)
    assert len(matched["HHCC_SC04_020"]) == 4, matched["HHCC_SC04_020"]
    assert matched["HHCC_SC04_0200"] == []          # longer shot number must not match
    assert dkt.folder_matches("HHCC_R1_SC04_020_Tom_comp_v03", "HHCC_SC04_020")
    assert not dkt.folder_matches("HHCC_R1_SC04_0200", "HHCC_SC04_020")

    r = dkt.analyze_shot("HHCC_SC04_020", matched["HHCC_SC04_020"], dkt.MODE_PUBLISH, (".exr",), True)
    assert r.version_text == "v03" and r.found == 5, (r.version_text, r.found)
    assert r.units[0].path.name == "HHCC_R1_SC04_020_Tom_comp_v03"
    shot = dkt.Shot("HHCC_SC04_020", 5, scan=r)
    jobs = dkt.build_jobs(shot, dkt.MODE_PUBLISH, src, tmp / "dst", "261118")
    assert all(d.parent == tmp / "dst/261118/HHCC_R1_SC04_020_Tom_comp_v03" for _, d in jobs)

    r2 = dkt.analyze_shot("HHCC_SC04_010", matched["HHCC_SC04_010"], dkt.MODE_PUBLISH, (".exr",), True)
    assert r2.version_text == "v01" and r2.found == 4
    print("reel tag test passed")


if __name__ == "__main__":
    run_tests()
