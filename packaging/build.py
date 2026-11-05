"""Build a PyInstaller onedir bundle and zip it. Usage: python packaging/build.py [--zip-only] [--name NAME]"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip-only", action="store_true", help="skip PyInstaller, just zip dist/token-burn")
    ap.add_argument("--name", default="token-burn")
    args = ap.parse_args()
    os.chdir(ROOT)
    if not args.zip_only:
        subprocess.check_call([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                               os.path.join("packaging", "token-burn.spec")])
    folder = os.path.join("dist", "token-burn")
    if not os.path.isdir(folder):
        sys.exit("dist/token-burn not found")
    shutil.copy(os.path.join("packaging", "README-dist.md"), os.path.join(folder, "README.md"))
    # Double-click launcher for the install wizard (Windows double-clicks token-burn.exe itself).
    if sys.platform == "darwin":
        launcher = os.path.join("packaging", "macos", "Install Token Burn.command")
    elif sys.platform.startswith("linux"):
        launcher = os.path.join("packaging", "linux", "install.sh")
    else:
        launcher = None
    if launcher:
        target = os.path.join(folder, os.path.basename(launcher))
        shutil.copy(launcher, target)
        os.chmod(target, 0o755)
    # make_archive stores forward-slash paths. Keep the top-level token-burn/ folder inside the zip.
    out = shutil.make_archive(os.path.join("dist", args.name), "zip", root_dir="dist", base_dir="token-burn")
    print(out)


if __name__ == "__main__":
    main()
