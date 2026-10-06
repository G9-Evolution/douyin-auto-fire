"""交付白名单不得把账号私有文件带进压缩包。"""
from __future__ import annotations

from zipfile import ZipFile

from scripts.build_release import build_release, verify_release


def test_release_uses_only_allowlisted_files(tmp_path):
    archive = build_release(tmp_path)
    manifest = verify_release(archive)
    with ZipFile(archive) as bundle:
        names = bundle.namelist()
    assert "web_server.py" in manifest["files"]
    assert "app/validation.py" in manifest["files"]
    assert "requirements.lock.txt" in manifest["files"]
    assert not any(name.endswith("/.env") or "storage-state" in name
                   or name.startswith("douyin-auto-fire/artifacts/")
                   or name.startswith("douyin-auto-fire/config/tasks/")
                   or name.startswith("douyin-auto-fire/.venv/") for name in names)
