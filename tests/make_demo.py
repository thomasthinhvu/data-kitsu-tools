"""Build a small fake W:-style source tree from the sample CSV, for screenshots and manual tests."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import data_kitsu_tools as dkt

def build(root: Path, csv_path: Path):
    shots = dkt.load_csv(csv_path)
    for n, s in enumerate(shots[:14]):
        seq = s.name.split("_")[1]                          # e.g. SC01
        reel = "REEL_" + s.name.split("_")[0]
        base = s.frames or 10                                  # CSV without Frames: demo uses 10
        frames = base if n % 5 else max(1, base - 3)           # some shots will mismatch
        if n % 4 == 1:                                        # PL01 + PL02 variants
            for pl in ("PL01", "PL02"):
                d = root / "FILM" / reel / seq / f"{s.name}_{pl}"
                d.mkdir(parents=True, exist_ok=True)
                for i in range(1, frames + 1):
                    (d / f"{s.name}_{pl}_{i:06d}.exr").write_bytes(b"x" * 4096)
        else:
            d = root / "FILM" / reel / seq / s.name
            d.mkdir(parents=True, exist_ok=True)
            for i in range(1, frames + 1):
                (d / f"{s.name}_{i:06d}.exr").write_bytes(b"x" * 4096)
        (d / ".DS_Store").write_bytes(b"junk")

if __name__ == "__main__":
    build(Path(sys.argv[1]), Path(sys.argv[2]))
    print("demo tree built")
