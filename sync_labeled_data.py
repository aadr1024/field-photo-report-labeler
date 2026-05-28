#!/usr/bin/env python3
"""Export field photo report labeled-data metadata without touching source photos."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT = "FieldPhotoReportLabeler"
APP_DIR = Path(__file__).resolve().parent


def env_path(name: str, fallback: str) -> Path:
    return Path(os.environ.get(name, fallback)).expanduser()


LOCAL_PHOTO_ROOT = env_path("REPORT_LABELER_LOCAL_PHOTO_ROOT", "~/Downloads/MT/report-labeler local/site-photos")
DRIVE_PHOTO_SOURCE = env_path(
    "REPORT_LABELER_DRIVE_PHOTO_SOURCE",
    "~/Library/CloudStorage/GoogleDrive-ACCOUNT/Shared drives/FieldPhotoReportLabeler/Site photos",
)
DEFAULT_LOCAL_BACKUP_DIR = APP_DIR / "labeled-data-backups"
DEFAULT_DRIVE_BACKUP_DIR = env_path(
    "REPORT_LABELER_METADATA_BACKUP_DIR",
    "~/Library/CloudStorage/GoogleDrive-ACCOUNT/My Drive/field photo report labeled data sync",
)
METADATA_FILES = {
    "annotations": ".field-photo-report-labeler-annotations.json",
    "folder_state": ".field-photo-report-labeler-folder-state.json",
}
OPTIONAL_METADATA_FILES = {
    "folder_memory": ".folder_memory.json",
    "ui_state": ".ui_state.json",
}
SYNC_CADENCES = [
    ("2-minute-sync", "2 minute sync", 2 * 60),
    ("5-minute-sync", "5 minute sync", 5 * 60),
    ("10-minute-sync", "10 minute sync", 10 * 60),
    ("30-minute-sync", "30 minute sync", 30 * 60),
    ("1-hour-sync", "1 hour sync", 60 * 60),
    ("5-hour-sync", "5 hour sync", 5 * 60 * 60),
    ("1-month-sync", "1 month sync", 30 * 24 * 60 * 60),
    ("1-year-sync", "1 year sync", 365 * 24 * 60 * 60),
]


def is_cloud_storage(path: Path) -> bool:
    return "/Library/CloudStorage/" in str(path)


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def copy_if_exists(source: Path, dest: Path) -> bool:
    if not source.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return True


def build_payload() -> dict[str, Any]:
    created_at = datetime.now(timezone.utc).isoformat()
    metadata: dict[str, Any] = {}
    present_files: dict[str, str] = {}
    missing_files: dict[str, str] = {}

    for key, filename in {**METADATA_FILES, **OPTIONAL_METADATA_FILES}.items():
        path = APP_DIR / filename
        value = read_json(path)
        if value is None:
            missing_files[key] = filename
            continue
        metadata[key] = value
        present_files[key] = filename

    return {
        "project": PROJECT,
        "artifact_type": "field photo report labeled data backup",
        "created_at_utc": created_at,
        "app_metadata_source_dir": str(APP_DIR),
        "local_photo_processing_root": str(LOCAL_PHOTO_ROOT),
        "drive_photo_source_read_only_reference": str(DRIVE_PHOTO_SOURCE),
        "rules": [
            "Photos are processed only from local_photo_processing_root.",
            "Do not process, annotate, or mutate photos under CloudStorage/GoogleDrive.",
            "This export contains metadata only; it intentionally does not copy photos.",
            "Photo files are disposable working data; labels and folder state are durable labeled data.",
        ],
        "present_metadata_files": present_files,
        "missing_metadata_files": missing_files,
        "metadata": metadata,
    }


def export_to_destination(dest: Path, payload: dict[str, Any], timestamp: str) -> dict[str, Any]:
    dest.mkdir(parents=True, exist_ok=True)
    snapshot_dir = dest / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    outputs: dict[str, str] = {}
    latest_payload = dest / "field_photo_report_labeler_labeled_data_latest.json"
    snapshot_payload = snapshot_dir / f"field_photo_report_labeler_labeled_data_{timestamp}.json"
    write_json(latest_payload, payload)
    write_json(snapshot_payload, payload)
    outputs["latest_payload"] = str(latest_payload)
    outputs["snapshot_payload"] = str(snapshot_payload)

    for key, filename in METADATA_FILES.items():
        source = APP_DIR / filename
        latest = dest / f"field_photo_report_labeler_labeled_data_{key}.json"
        if copy_if_exists(source, latest):
            outputs[f"latest_{key}"] = str(latest)

    manifest = dest / "README_field_photo_report_labeler_labeled_data_sync.md"
    manifest.write_text(
        "# field photo report labeled data sync\n\n"
        "This folder stores metadata backups only. It must not be used as a photo processing root.\n\n"
        f"- Local processing root: `{LOCAL_PHOTO_ROOT}`\n"
        f"- App metadata source: `{APP_DIR}`\n"
        f"- Drive photo source is read-only reference only: `{DRIVE_PHOTO_SOURCE}`\n"
        "- Latest backup: `field_photo_report_labeler_labeled_data_latest.json`\n"
        "- Timestamped backups: `snapshots/`\n",
    )
    outputs["readme"] = str(manifest)
    outputs["cadence_snapshots"] = write_cadence_snapshots(dest, payload, timestamp)
    return outputs


def parse_snapshot_time(path: Path) -> datetime | None:
    prefix = "field_photo_report_labeler_labeled_data_"
    suffix = ".json"
    name = path.name
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    raw = name[len(prefix) : -len(suffix)]
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def latest_snapshot_time(folder: Path) -> datetime | None:
    latest: datetime | None = None
    for path in folder.glob("field_photo_report_labeler_labeled_data_*.json"):
        parsed = parse_snapshot_time(path)
        if parsed and (latest is None or parsed > latest):
            latest = parsed
    return latest


def write_cadence_snapshots(dest: Path, payload: dict[str, Any], timestamp: str) -> dict[str, Any]:
    now = datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    result: dict[str, Any] = {}
    for folder_name, label, interval_seconds in SYNC_CADENCES:
        folder = dest / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        latest = folder / "field_photo_report_labeler_labeled_data_latest.json"
        write_json(latest, payload)

        last = latest_snapshot_time(folder)
        due = last is None or (now - last).total_seconds() >= interval_seconds
        entry: dict[str, Any] = {
            "label": label,
            "interval_seconds": interval_seconds,
            "latest_payload": str(latest),
            "snapshot_written": False,
        }
        if due:
            snapshot = folder / f"field_photo_report_labeler_labeled_data_{timestamp}.json"
            write_json(snapshot, payload)
            entry["snapshot_written"] = True
            entry["snapshot_payload"] = str(snapshot)
        elif last is not None:
            entry["last_snapshot_utc"] = last.isoformat()

        readme = folder / "README.md"
        readme.write_text(
            f"# Field Photo Report Labeler {label}\\n\\n"
            "This folder stores metadata-only labeled-data snapshots. It does not store photos.\\n\\n"
            f"- Cadence: {label}\\n"
            f"- Interval seconds: {interval_seconds}\\n"
            "- `field_photo_report_labeler_labeled_data_latest.json` is refreshed every sync run.\\n"
            "- Timestamped `field_photo_report_labeler_labeled_data_*.json` files are kept and not deleted by this script.\\n"
        )
        entry["readme"] = str(readme)
        result[folder_name] = entry
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Export field photo report labeled-data metadata backups.")
    parser.add_argument("--dest", action="append", type=Path, default=[], help="Backup destination. Repeatable.")
    parser.add_argument("--no-default-local", action="store_true", help="Do not write the repo-local backup copy.")
    parser.add_argument("--allow-cloud-dest", action="store_true", help="Allow writing metadata backup files into CloudStorage.")
    args = parser.parse_args()

    destinations = [] if args.no_default_local else [DEFAULT_LOCAL_BACKUP_DIR]
    destinations.extend(args.dest)
    if not destinations:
        raise SystemExit("No destinations requested")

    for dest in destinations:
        if is_cloud_storage(dest) and not args.allow_cloud_dest:
            raise SystemExit(f"Refusing CloudStorage destination without --allow-cloud-dest: {dest}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = build_payload()
    payload["sync_destinations"] = [str(dest) for dest in destinations]

    result = {"project": PROJECT, "timestamp": timestamp, "destinations": {}}
    for dest in destinations:
        result["destinations"][str(dest)] = export_to_destination(dest, payload, timestamp)

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
