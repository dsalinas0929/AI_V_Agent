"""Build a source-free client ZIP from an explicit file allowlist."""

import hashlib
import os
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    name = f"agent-v-{version}-client"
    destination = ROOT / "dist"
    destination.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agent-v-release-") as temporary:
        temporary = Path(temporary)
        wheels = temporary / "wheels"
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(wheels)], cwd=ROOT, check=True)
        (wheel,) = wheels.glob("*.whl")
        bundle = temporary / name
        bundle.mkdir()
        for source in [
            wheel,
            ROOT / "INSTALL.md",
            ROOT / "release/install.sh",
            ROOT / "release/install.ps1",
        ]:
            shutil.copy2(source, bundle / source.name)
        os.chmod(bundle / "install.sh", 0o755)
        checksums = "".join(
            f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {file.name}\n"
            for file in sorted(bundle.iterdir())
        )
        (bundle / "SHA256SUMS").write_text(checksums, encoding="utf-8")
        archive = destination / f"{name}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            for file in sorted(bundle.iterdir()):
                output.write(file, f"{name}/{file.name}")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (destination / f"{name}.zip.sha256").write_text(
            f"{digest}  {archive.name}\n", encoding="utf-8"
        )
        shutil.copy2(wheel, destination / wheel.name)
        print(f"Release ready: {archive}")
        print("No upload or publication performed.")


if __name__ == "__main__":
    main()
