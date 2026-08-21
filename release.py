"""Test-gated, atomic release for PoE Flipper.

    .venv\\Scripts\\python.exe release.py 1.0.17 --notes "What changed."

Refuses to publish unless the working tree is clean and the full regression
suite passes; then bumps __version__, commits, tags, builds the zip from
tracked files, publishes the GitHub release and verifies the shipped zip
carries the right version.  Auto-update clients trust whatever this
publishes — never bypass the gate with --skip-tests unless the suite itself
is what's broken.
"""
import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FLIPPER = ROOT / "flipper.py"
ZIP = ROOT / "poe-flipper.zip"


def run(args, **kw):
    print("  $", " ".join(str(a) for a in args))
    return subprocess.run([str(a) for a in args], check=True, cwd=ROOT, **kw)


def fail(msg):
    print(f"\nRELEASE ABORTED: {msg}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("version", help="new version, e.g. 1.0.17")
    ap.add_argument("--notes", required=True, help="release notes / commit body")
    ap.add_argument("--skip-tests", action="store_true",
                    help="DANGEROUS: publish without the regression gate")
    args = ap.parse_args()

    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        fail(f"version {args.version!r} is not X.Y.Z")
    tag = f"v{args.version}"

    gh = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"
    if not Path(gh).exists():
        fail("GitHub CLI not found")

    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        fail(f"working tree not clean — commit or stash first:\n{dirty}")

    src = FLIPPER.read_text(encoding="utf-8")
    m = re.search(r'__version__ = "(\d+\.\d+\.\d+)"', src)
    if not m:
        fail("__version__ not found in flipper.py")
    if m.group(1) == args.version:
        fail(f"{args.version} is already the current version")

    if args.skip_tests:
        print("!! skipping the regression gate !!")
    else:
        print("== regression gate ==")
        gate = subprocess.run([sys.executable, "-m", "unittest", "discover",
                               "-s", "tests"], cwd=ROOT)
        if gate.returncode != 0:
            fail("regression suite failed — nothing was released")
        print("== gate passed ==")

    print(f"== releasing {m.group(1)} -> {args.version} ==")
    FLIPPER.write_text(src.replace(f'__version__ = "{m.group(1)}"',
                                   f'__version__ = "{args.version}"'),
                       encoding="utf-8")

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        f.write(f"Release {tag}\n\n{args.notes}\n\n"
                "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>\n")
        msg_file = f.name

    run(["git", "add", "flipper.py"])
    run(["git", "commit", "-F", msg_file])
    run(["git", "push"])
    run(["git", "tag", tag])
    run(["git", "push", "origin", tag])
    run(["git", "archive", "-o", str(ZIP), "HEAD"])
    run([gh, "release", "create", tag,
         f"{ZIP}#poe-flipper.zip (source + setup)",
         "--title", f"PoE Flipper {args.version}", "--notes", args.notes])

    with zipfile.ZipFile(ZIP) as z:
        shipped = re.search(r'__version__ = "(\d+\.\d+\.\d+)"',
                            z.read("flipper.py").decode())
    if not shipped or shipped.group(1) != args.version:
        fail(f"zip carries wrong version {shipped and shipped.group(1)!r} — "
             f"delete release {tag} and investigate!")
    print(f"\n== released {tag}: zip verified, clients will auto-update ==")


if __name__ == "__main__":
    main()
