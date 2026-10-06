"""用固定白名单生成不含账号私有数据的 Windows 交付包。"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path(r"E:\Codex项目总\自动续火花交付")
PREFIX = "douyin-auto-fire"

FILES = (
    ".env.example", ".env.account.example", ".gitignore", "LICENSE", "README.md",
    "DELIVERY.md", "VERSION.txt", "config.example.json", "pytest.ini", "run.py",
    "web_server.py", "requirements.txt", "requirements-dev.txt", "requirements.lock.txt",
    "启动控制台.bat", "web/index.html", "config/accounts.example.json",
    "config/tasks.example.json", "config/stickers.json", "config/stickers_catalog.json",
    "scripts/build_release.py", "scripts/catchup.py", "scripts/login.py", "scripts/login_auto.py",
    "scripts/run-windows.ps1",
)
PATTERNS = ("app/*.py", "tests/test_*.py", "assets/stickers/*.webp")


def collect_files(root: Path = ROOT) -> list[Path]:
    names = set(FILES)
    for pattern in PATTERNS:
        names.update(path.relative_to(root).as_posix() for path in root.glob(pattern) if path.is_file())
    paths = [root / name for name in sorted(names)]
    for path in paths:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"交付白名单文件缺失或路径异常: {path}")
    return paths


def build_release(output_dir: Path = DEFAULT_OUTPUT, root: Path = ROOT) -> Path:
    version = (root / "VERSION.txt").read_text(encoding="utf-8").strip()
    if not version or any(ch not in "0123456789." for ch in version):
        raise ValueError("VERSION.txt 格式不正确")
    paths = collect_files(root)
    built_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"douyin-auto-fire-v{version}-safe-{built_at}.zip"
    hashes = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    manifest = {"version": version, "built_at_utc": built_at, "files": hashes}
    with ZipFile(archive, mode="x", compression=ZIP_DEFLATED) as bundle:
        for path in paths:
            relative = path.relative_to(root).as_posix()
            bundle.write(path, f"{PREFIX}/{relative}")
        bundle.writestr(f"{PREFIX}/RELEASE-MANIFEST.json",
                        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    verify_release(archive)
    return archive


def verify_release(archive: Path) -> dict:
    with ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read(f"{PREFIX}/RELEASE-MANIFEST.json"))
        expected = {f"{PREFIX}/{name}" for name in manifest["files"]}
        expected.add(f"{PREFIX}/RELEASE-MANIFEST.json")
        actual = set(bundle.namelist())
        if actual != expected:
            raise ValueError("交付包文件清单与白名单不一致")
        for name, wanted in manifest["files"].items():
            if hashlib.sha256(bundle.read(f"{PREFIX}/{name}")).hexdigest() != wanted:
                raise ValueError(f"交付包文件校验失败: {name}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="生成不含账号凭证的安全交付包")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    archive = build_release(args.output_dir)
    print(archive)
    print(hashlib.sha256(archive.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
