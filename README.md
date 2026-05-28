# Field Photo Report Labeler

A Streamlit image-labeling workflow for turning field photo sets into structured report evidence. The app is optimized for fast visual review: keyboard navigation, persistent image labels, table-oriented presets, folder status checks, and metadata exports for downstream report automation.

## What it does

- Displays image folders as a fast review grid.
- Supports multi-select, range select, and command/control-assisted selection.
- Applies table-oriented labels with visible chips instead of hidden dropdowns.
- Persists annotation metadata separately from photos.
- Shows folder-level status such as video presence and label completeness.
- Exports labeled-data backups without copying source photos.
- Includes optional launchd automation for hourly photo-cache syncs when the Mac is plugged in.

## Why it exists

Report creation often depends on matching values in photos to the correct table position. This tool makes that mapping explicit: each image gets a durable label, and later automation can use those labels as a source-of-truth for filling reports.

## Data model

The durable metadata lives outside the photo files:

```json
{
  "/local/photo/root/folder/image.JPG": ["Table 4 Row 2 Column A Test Station 1"]
}
```

Photos are treated as a replaceable local cache. Labels and folder state are the durable assets.

## Safety model

- Process only local photo-cache folders.
- Do not annotate directly inside cloud-drive folders.
- Do not use symlinks from the local photo root into cloud storage.
- Keep runtime metadata, manifests, logs, and backups out of git.
- Public repo history intentionally contains code only, not field photos or label data.

## Configuration

The sync scripts use environment variables so private paths do not need to be committed.

```bash
export REPORT_LABELER_DRIVE_PHOTO_SOURCE="$HOME/Library/CloudStorage/GoogleDrive-ACCOUNT/Shared drives/FieldPhotoReportLabeler/Site photos"
export REPORT_LABELER_LOCAL_PHOTO_ROOT="$HOME/Downloads/MT/report-labeler local/site-photos"
export REPORT_LABELER_METADATA_BACKUP_DIR="$HOME/Library/CloudStorage/GoogleDrive-ACCOUNT/My Drive/field photo report labeled data sync"
```

## Run the app

```bash
streamlit run image_grid_app.py
```

Helper scripts are included for the local workflow:

```bash
./start_image_grid_app.sh
./restart_image_grid_app.sh
```

## Sync local photo cache

Copy missing media from the configured cloud source into the local processing root:

```bash
python3 sync_site_photos.py
```

The importer creates real local files, never symlinks, and does not overwrite existing files except zero-byte placeholders.

## Export labeled-data metadata

```bash
python3 sync_labeled_data.py
```

The export writes metadata-only snapshots. It does not copy photos.

## Optional hourly sync on macOS

Install the LaunchAgent template:

```bash
automation/install_photo_sync_launch_agent.sh
```

Policy:

- runs at agent load.
- runs every hour.
- skips when the Mac is not on AC power.
- skips when a previous sync is still running.

## Project layout

```text
image_grid_app.py                         Streamlit labeling app
sync_site_photos.py               local photo-cache importer
sync_labeled_data.py              metadata-only backup exporter
automation/                               launchd sync helper
docs/labeled_data_storage.md      storage policy notes
```

## Notes

This repository is the software layer only. Runtime files such as annotations, folder state, photo import manifests, and metadata backups are intentionally ignored.
