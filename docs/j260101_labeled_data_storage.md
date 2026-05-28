# Labeled data storage

Photos and labeled data are intentionally separated.

- Process photos only from the local processing root configured by `J260101_LOCAL_PHOTO_ROOT`.
- Do not process photos directly from Google Drive or any `CloudStorage/GoogleDrive` path.
- Do not use symlinks from the local photo root to Drive; local photo files are disposable working data.
- Preserve labels separately as durable metadata.
- Sync labeled-data metadata with `sync_j260101_labeled_data.py`.
- Optional metadata sync destination is configured with `J260101_METADATA_BACKUP_DIR`.

The backup exports metadata only: annotations, folder state, and small UI/folder memory JSON when present. It does not copy photos.

Each sync destination keeps the main latest backup at the root, plus cadence folders:

- `2-minute-sync`
- `5-minute-sync`
- `10-minute-sync`
- `30-minute-sync`
- `1-hour-sync`
- `5-hour-sync`
- `1-month-sync`
- `1-year-sync`

Each cadence folder always refreshes `J260101_labeled_data_latest.json`. Timestamped snapshots are written only when that cadence is due, and the script does not delete old timestamped snapshots.

Photo import rule:

- Use `sync_j260101_site_photos.py` to copy missing media from the read-only Drive source into the local processing root.
- The import creates real local files, never symlinks.
- Existing local files are not overwritten, except zero-byte corrupt placeholders are repaired from the Drive source.
- Import manifests are written under `photo-import-manifests/`.

## Storage policy

The operating principle is simple and easy to reason about:

- Metadata is the durable asset.
- Local photos are a replaceable speed cache.
- Google Drive photo folders are read-only source material, not processing roots.
- If the Mac has comfortable free space, import all missing photos into the local processing root.
- Existing local photos should usually be kept because deleting them saves storage but costs future download time.
- If disk space becomes tight, prefer deleting local photo cache folders only after metadata backup has succeeded.
- Recreate local photos later by rerunning `sync_j260101_site_photos.py`.

Practical threshold:

- `>25 GiB free`: import all missing photos; optimize for Aadi's labeling time.
- `10-25 GiB free`: still okay, but be aware before large imports.
- `<10 GiB free`: stop broad imports; sync metadata first; then choose specific folders to import or delete old local cache folders.

Current intent: minimize waiting during labeling. Download time can happen preemptively; interactive labeling time should not wait for photo materialization.
