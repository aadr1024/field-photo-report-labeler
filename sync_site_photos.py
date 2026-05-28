#!/usr/bin/env python3
"""Copy missing field photo report media from Drive source into local processing cache.

Drive is read-only source. Local site-photos is the only app processing root.
This script creates real local files, never symlinks, and never overwrites by default.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

PROJECT = "FieldPhotoReportLabeler"


def env_path(name: str, fallback: str) -> Path:
    return Path(os.environ.get(name, fallback)).expanduser()


DRIVE_PHOTO_SOURCE = env_path(
    "REPORT_LABELER_DRIVE_PHOTO_SOURCE",
    "~/Library/CloudStorage/GoogleDrive-ACCOUNT/Shared drives/FieldPhotoReportLabeler/Site photos",
)
LOCAL_PHOTO_ROOT = env_path("REPORT_LABELER_LOCAL_PHOTO_ROOT", "~/Downloads/MT/report-labeler local/site-photos")
APP_DIR = Path(__file__).resolve().parent
MANIFEST_DIR = APP_DIR / "photo-import-manifests"
MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".bmp",
    ".mov", ".mp4", ".m4v", ".avi", ".mts", ".m2ts",
}


def is_cloud_storage(path: Path) -> bool:
    return "/Library/CloudStorage/" in str(path)


def iter_media(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        if path.name.startswith("."):
            continue
        if path.suffix.lower() in MEDIA_EXTENSIONS:
            yield path


def safe_relative(path: Path, root: Path) -> Path:
    rel = path.relative_to(root)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"Unsafe relative path: {rel}")
    return rel


def copy_media_file(source: Path, dest: Path) -> None:
    """Copy bytes first; best-effort metadata only so macOS xattr/chflags failures do not fake a failed copy."""
    shutil.copyfile(source, dest)
    try:
        shutil.copymode(source, dest)
    except OSError:
        pass
    try:
        shutil.copystat(source, dest)
    except OSError:
        pass


def copy_missing_media(source_root: Path, local_root: Path, dry_run: bool) -> dict:
    if not source_root.exists():
        raise FileNotFoundError(f"Source does not exist: {source_root}")
    if not is_cloud_storage(source_root):
        raise ValueError(f"Expected source under CloudStorage Google Drive: {source_root}")
    if is_cloud_storage(local_root):
        raise ValueError(f"Local processing root must not be CloudStorage: {local_root}")

    copied = []
    repaired_zero_byte = []
    existing = []
    skipped_symlink = []
    errors = []
    source_count = 0

    for source in iter_media(source_root):
        source_count += 1
        try:
            rel = safe_relative(source, source_root)
            dest = local_root / rel
            if dest.is_symlink():
                skipped_symlink.append(str(rel))
                continue
            if dest.exists():
                if dest.is_file() and dest.stat().st_size == 0 and source.stat().st_size > 0:
                    if not dry_run:
                        copy_media_file(source, dest)
                    repaired_zero_byte.append(str(rel))
                    continue
                existing.append(str(rel))
                continue
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                copy_media_file(source, dest)
            copied.append(str(rel))
        except Exception as exc:  # keep manifest complete instead of hiding partial failures
            errors.append({"source": str(source), "error": str(exc)})

    return {
        "project": PROJECT,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_root": str(source_root),
        "local_root": str(local_root),
        "dry_run": dry_run,
        "source_media_count": source_count,
        "copied_count": len(copied),
        "repaired_zero_byte_count": len(repaired_zero_byte),
        "existing_count": len(existing),
        "skipped_symlink_count": len(skipped_symlink),
        "error_count": len(errors),
        "copied": copied,
        "repaired_zero_byte": repaired_zero_byte,
        "existing_sample": existing[:50],
        "skipped_symlink": skipped_symlink,
        "errors": errors,
        "rules": [
            "Drive source is read-only.",
            "Local site-photos contains real copied files, not symlinks.",
            "Existing local files are not overwritten except zero-byte corrupt placeholders are repaired from Drive source.",
            "The Streamlit app should process only local_root.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Import missing field photo report media into local processing cache.")
    parser.add_argument("--source", type=Path, default=DRIVE_PHOTO_SOURCE)
    parser.add_argument("--local-root", type=Path, default=LOCAL_PHOTO_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest = copy_missing_media(args.source, args.local_root, args.dry_run)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = MANIFEST_DIR / f"field_photo_report_labeler_photo_import_{timestamp}.json"
    latest_path = MANIFEST_DIR / "field_photo_report_labeler_photo_import_latest.json"
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    manifest_path.write_text(payload)
    latest_path.write_text(payload)
    print(json.dumps({
        "project": PROJECT,
        "dry_run": args.dry_run,
        "source_media_count": manifest["source_media_count"],
        "copied_count": manifest["copied_count"],
        "existing_count": manifest["existing_count"],
        "repaired_zero_byte_count": manifest["repaired_zero_byte_count"],
        "skipped_symlink_count": manifest["skipped_symlink_count"],
        "error_count": manifest["error_count"],
        "manifest": str(manifest_path),
        "latest_manifest": str(latest_path),
    }, indent=2, sort_keys=True))
    return 1 if manifest["error_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
