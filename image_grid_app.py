#!/usr/bin/env -S uv run streamlit run
# /// script
# requires-python = ">=3.10"
# dependencies = ["streamlit>=1.52.2", "pillow>=10.0.0"]
# ///
"""
Image Grid Viewer - Browse folders of images with thumbnails.

Run with:
    uv run streamlit run image_grid_app.py

Or directly (if shebang works):
    ./image_grid_app.py
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import platform
import re
import difflib
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import List

import streamlit as st
import streamlit.components.v1 as components
from PIL import Image
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, unquote, urlparse
import socket
from errno import EADDRINUSE


def _read_env_int(name: str, default: int) -> int:
    """Read an integer environment variable safely."""
    value = os.getenv(name)
    try:
        return int(value) if value else default
    except (TypeError, ValueError):
        return default


def _is_port_available(port: int) -> bool:
    """Return True if this local TCP port is available."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.close()
        return True
    except Exception:
        return False


def _pick_clipboard_port(requested_port: int, scan_count: int) -> tuple[int, int, list[int], str]:
    """Pick a helper port, preferring requested and scanning upward on collisions."""
    scan_count = max(1, scan_count)
    attempted: list[int] = []
    for offset in range(scan_count):
        candidate = requested_port + offset
        attempted.append(candidate)
        if _is_port_available(candidate):
            if candidate != requested_port:
                return candidate, requested_port, attempted, "fallback"
            return candidate, requested_port, attempted, "requested"
    return requested_port, requested_port, attempted, "scan_exhausted"


_CLIPBOARD_PORT_REQUESTED_FROM_ENV = _read_env_int("STREAMLIT_CLIPBOARD_PORT", 8503)
_CLIPBOARD_PORT_SCAN = _read_env_int("STREAMLIT_CLIPBOARD_PORT_SCAN", 8)
CLIPBOARD_PORT, _CLIPBOARD_PORT_REQUESTED, _CLIPBOARD_PORT_SCAN_LIST, _CLIPBOARD_PORT_REASON = _pick_clipboard_port(
    _CLIPBOARD_PORT_REQUESTED_FROM_ENV,
    _CLIPBOARD_PORT_SCAN
)
if CLIPBOARD_PORT != _CLIPBOARD_PORT_REQUESTED:
    print(f"Using fallback clipboard port {CLIPBOARD_PORT} (requested {_CLIPBOARD_PORT_REQUESTED})", file=sys.stderr)

LAST_HOVER_FILE = Path(__file__).parent / ".last_hover.txt"
IMAGE_INDEX_FILE = Path(__file__).parent / ".image_index.json"
INTERACTION_LOG_FILE = Path(__file__).parent / ".interaction_log.json"
UI_STATE_FILE = Path(__file__).parent / ".ui_state.json"
ANNOTATION_METADATA_FILE = Path(__file__).parent / ".field-photo-report-labeler-annotations.json"
FOLDER_STATE_FILE = Path(__file__).parent / ".field-photo-report-labeler-folder-state.json"
REPORT_LABELER_TABLES = ("3", "4", "5", "6")
REPORT_LABELER_TABLE_PRESET_ROWS = {
    "3": ("Row 1", "Row 2", "Row 3", "Row 4"),
    "4": ("Row 2 Column A", "Row 3 Column A", "Row 2 Column B", "Row 3 Column B"),
    "5": ("MG 1", "MG 2", "MG 3", "MG 4", "MG 5", "MG 6", "MG 7"),
    "6": ("MG 1", "MG 2", "MG 3", "MG 4", "MG 5", "MG 6", "MG 7"),
}
REPORT_LABELER_TABLE_STATION_ROW_PRESETS = {
    "4": (
        ("Test Station 1", "Row 2 Column A"),
        ("Test Station 1", "Row 3 Column A"),
        ("Test Station 2", "Row 2 Column B"),
        ("Test Station 2", "Row 3 Column B"),
    ),
}
REPORT_LABELER_TABLE_STATION_SUFFIXES = {
    "4": ("Test Station 1", "Test Station 2"),
    "5": ("Test Station 1", "Test Station 2"),
    "6": ("Test Station 1", "Test Station 2"),
}
REPORT_LABELER_INSTANT_OFF_STATUS_LABELS = (
    "Yes Video Exists",
    "No Video Exists",
)
REPORT_LABELER_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".m4v", ".wmv", ".flv", ".mkv", ".webm", ".mpeg", ".mpg", ".m4v"}
ADJACENT_FOLDER_PRELOAD_RADIUS = 2
ADJACENT_FOLDER_PRELOAD_IMAGE_LIMIT = 80


def _build_table_label_variants(table: str) -> tuple[str, ...]:
    station_rows = REPORT_LABELER_TABLE_STATION_ROW_PRESETS.get(table)
    if station_rows:
        return tuple(
            f"Table {table} {row} {station}"
            for station, row in station_rows
        )
    rows = REPORT_LABELER_TABLE_PRESET_ROWS.get(table, ())
    stations = REPORT_LABELER_TABLE_STATION_SUFFIXES.get(table)
    if not stations:
        return tuple(f"Table {table} {row}" for row in rows)
    return tuple(
        f"Table {table} {row} {station}"
        for station in stations
        for row in rows
    )


REPORT_LABELER_CUSTOM_LABEL_PRESETS: list[str] = []
REPORT_LABELER_LABEL_PRESETS = [
    preset
    for table in REPORT_LABELER_TABLES
    for preset in _build_table_label_variants(table)
] + REPORT_LABELER_CUSTOM_LABEL_PRESETS


def _build_table_preset_group(table: str) -> tuple[str, ...]:
    station_rows = REPORT_LABELER_TABLE_STATION_ROW_PRESETS.get(table)
    if station_rows:
        return tuple(
            f"Table {table} {row} {station}"
            for station, row in station_rows
        )
    rows = REPORT_LABELER_TABLE_PRESET_ROWS.get(table, ())
    stations = REPORT_LABELER_TABLE_STATION_SUFFIXES.get(table, ())
    if stations:
        return tuple(
            f"Table {table} {row} {station}"
            for station in stations
            for row in rows
        )
    return tuple(f"Table {table} {row}" for row in rows)


REPORT_LABELER_LABEL_PRESET_GROUPS = {
    f"Table {table}": list(_build_table_preset_group(table))
    for table in REPORT_LABELER_TABLES
}
if REPORT_LABELER_CUSTOM_LABEL_PRESETS:
    REPORT_LABELER_LABEL_PRESET_GROUPS["Other"] = REPORT_LABELER_CUSTOM_LABEL_PRESETS
REPORT_LABELER_PRESET_LOOKUP = {label.lower(): label for label in REPORT_LABELER_LABEL_PRESETS}
def _normalize_annotation_single(values):
    """Keep a single, deterministic label for one-to-one label mapping."""
    raw_values = values if isinstance(values, (list, tuple, set)) else [values]
    for raw_value in raw_values:
        raw_label = str(raw_value or "").strip()
        canonical = REPORT_LABELER_PRESET_LOOKUP.get(raw_label.lower())
        if canonical:
            return [canonical]
    normalized = _normalize_annotation_labels(values)
    return normalized[:1]


def _normalize_annotation_stored(values) -> list[str]:
    """Normalize persisted labels without collapsing overlaps."""
    raw_values = values if isinstance(values, (list, tuple, set)) else [values]
    output: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        raw_label = str(raw_value or "").strip()
        if not raw_label:
            continue
        normalized = REPORT_LABELER_PRESET_LOOKUP.get(raw_label.lower())
        if not normalized:
            normalized = raw_label
        if normalized and normalized not in seen:
            seen.add(normalized)
            output.append(normalized)
    return output


def _current_grid_annotation_paths() -> set[str]:
    """Return current rendered image paths for view-scoped annotation counts."""
    try:
        index_data = json.loads(IMAGE_INDEX_FILE.read_text())
        images = index_data.get("images", [])
    except (json.JSONDecodeError, OSError):
        return set()
    return {
        _normalize_annotation_key(str(path))
        for path in images
        if isinstance(path, str) and path
    }


def _annotation_state_payload() -> dict:
    annotations = load_image_annotations()
    scoped_paths = _current_grid_annotation_paths()
    if scoped_paths:
        annotations = {
            path: labels
            for path, labels in annotations.items()
            if path in scoped_paths
        }
    label_counts: dict[str, int] = {}
    for labels in annotations.values():
        for label in _normalize_annotation_stored(labels):
            label_counts[label] = label_counts.get(label, 0) + 1
    return {
        "success": True,
        "annotations": annotations,
        "label_counts": label_counts,
    }


REPORT_LABELER_LEGACY_PAIR_MAP = {
    "a": "1",
    "b": "2",
    "c": "3",
    "d": "4",
}
REPORT_LABELER_ROW_LABEL_COLOR_PALETTE = {
    "1": "hsl(0, 0%, 12%)",    # black
    "2": "hsl(0, 84%, 56%)",   # red
    "3": "hsl(24, 94%, 53%)",  # orange
    "4": "hsl(48, 96%, 53%)",  # yellow
    "5": "hsl(142, 70%, 45%)", # green
    "6": "hsl(30, 45%, 38%)",  # brown
    "7": "hsl(267, 84%, 51%)", # purple
}
REPORT_LABELER_TABLE_WORDS = {
    "one": "3",
    "won": "3",
    "two": "4",
    "too": "4",
    "first": "3",
    "second": "4",
    "three": "3",
    "tree": "3",
    "free": "3",
    "for": "4",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
}

def log_interaction(interaction_type: str, details: dict = None):
    """Log an interaction, sorted by frequency (less frequent at top)."""
    from datetime import datetime
    try:
        if INTERACTION_LOG_FILE.exists():
            data = json.loads(INTERACTION_LOG_FILE.read_text())
        else:
            data = {"interactions": {}, "session_start": datetime.now().isoformat()}

        interactions = data.get("interactions", {})
        now = datetime.now().isoformat()

        if interaction_type not in interactions:
            interactions[interaction_type] = {"count": 0, "last": now, "recent_details": []}

        interactions[interaction_type]["count"] += 1
        interactions[interaction_type]["last"] = now

        # Keep last 5 details for context
        if details:
            recent = interactions[interaction_type].get("recent_details", [])
            recent.insert(0, {"time": now, **details})
            interactions[interaction_type]["recent_details"] = recent[:5]

        # Sort by count ascending (less frequent at top)
        sorted_interactions = dict(sorted(interactions.items(), key=lambda x: x[1]["count"]))

        data["interactions"] = sorted_interactions
        data["last_updated"] = now

        INTERACTION_LOG_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        pass  # Don't break app on logging errors


if _CLIPBOARD_PORT_REASON == "fallback":
    log_interaction("clipboard_port_resolved", {
        "reason": "preflight_fallback",
        "requested_port": _CLIPBOARD_PORT_REQUESTED,
        "selected_port": CLIPBOARD_PORT,
        "attempted_ports": _CLIPBOARD_PORT_SCAN_LIST,
    })
elif _CLIPBOARD_PORT_REASON == "scan_exhausted":
    log_interaction("clipboard_port_resolved", {
        "reason": "scan_exhausted",
        "requested_port": _CLIPBOARD_PORT_REQUESTED,
        "selected_port": CLIPBOARD_PORT,
        "attempted_ports": _CLIPBOARD_PORT_SCAN_LIST,
    })


def _extract_paths_from_request(path: str) -> list[str]:
    """Extract `paths`/`path` list from an HTTPServer request URL."""
    try:
        query = parse_qs(urlparse(path).query)
        raw = None
        if "paths" in query:
            raw = query["paths"][0] if query["paths"] else ""
        elif "path" in query:
            raw = query["path"][0] if query["path"] else ""
        if not raw:
            return []
        return [p.strip() for p in unquote(str(raw)).split("|") if p.strip()]
    except Exception:
        return []


def _normalize_existing_paths(paths: list) -> tuple[list[str], list[str]]:
    """Normalize to absolute paths and split into existing vs invalid."""
    valid_paths: list[str] = []
    invalid_paths: list[str] = []
    for raw in paths:
        if not raw:
            continue
        try:
            if not isinstance(raw, str):
                raw = str(raw)
            normalized = str(Path(raw).expanduser().resolve())
        except Exception as exc:
            invalid_paths.append(str(raw))
            continue
        if Path(normalized).exists():
            valid_paths.append(normalized)
        else:
            invalid_paths.append(normalized)

    # Preserve order and dedupe without importing OrderedDict
    def _dedupe(values: list[str]) -> list[str]:
        seen = set()
        out: list[str] = []
        for item in values:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    return _dedupe(valid_paths), _dedupe(invalid_paths)

class ClipboardHandler(BaseHTTPRequestHandler):
    _CORS_HEADER_KEYS = {
        "access-control-allow-origin",
        "access-control-allow-methods",
        "access-control-allow-headers",
        "access-control-allow-private-network",
    }

    def send_header(self, keyword, value):
        if not hasattr(self, "_sent_cors_headers"):
            self._sent_cors_headers = set()

        key = keyword.lower()
        if key in self._CORS_HEADER_KEYS:
            if key in self._sent_cors_headers:
                return
            self._sent_cors_headers.add(key)
        return super().send_header(keyword, value)

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        debug = False
        if self.path.startswith('/log?'):
            query = parse_qs(self.path.split('?')[1])
            itype = query.get('type', ['unknown'])[0]
            detail = query.get('detail', [None])[0]
            details = {"detail": detail} if detail else None
            log_interaction(itype, details)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        elif self.path.startswith('/copy?path='):
            # Extract path and source from: /copy?path=<encoded>&source=<type>
            query_part = self.path.split('path=', 1)[1]
            # Split off source parameter if present
            if '&source=' in query_part:
                path_encoded, source = query_part.split('&source=', 1)
            else:
                path_encoded, source = query_part, 'unknown'
            path = unquote(path_encoded)
            success = copy_image_to_clipboard(Path(path))
            log_interaction(f"copy_{source}", {"path": path, "name": Path(path).name, "success": success})

            # Update current index so browser highlights this image
            try:
                index_data = json.loads(IMAGE_INDEX_FILE.read_text())
                images = index_data.get('images', [])
                if path in images:
                    index_data['current'] = images.index(path)
                    IMAGE_INDEX_FILE.write_text(json.dumps(index_data))
            except:
                pass

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(f'{{"success": {str(success).lower()}, "name": "{Path(path).name}"}}'.encode())
        elif self.path.startswith('/annotation-state'):
            try:
                payload = _annotation_state_payload()
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
            except Exception as exc:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }).encode())
        elif self.path.startswith('/rename-label'):
            try:
                query = parse_qs(urlparse(self.path).query)
                old_label = str(query.get("old_label", [""])[0] or "").strip()
                canonical_label = str(query.get("canonical_label", [""])[0] or "").strip()
                raw_old_labels = str(query.get("old_labels", [""])[0] or "").strip()
                new_label = str(query.get("new_label", [""])[0] or "").strip()
                old_labels = [old_label, canonical_label]
                if raw_old_labels:
                    old_labels.extend([part.strip() for part in raw_old_labels.split("|") if part.strip()])
                updated = rename_image_annotation_label(old_labels, new_label)
                state_payload = _annotation_state_payload()
                log_interaction("rename_label", {
                    "old_label": old_label,
                    "canonical_label": canonical_label,
                    "new_label": new_label,
                    "updated": len(updated),
                })
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": True,
                    "updated_count": len(updated),
                    "updated_paths": list(updated.keys()),
                    "annotations": state_payload.get("annotations", {}),
                    "label_counts": state_payload.get("label_counts", {}),
                }).encode())
            except Exception as exc:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }).encode())
        elif self.path.startswith('/folder-state'):
            try:
                query = parse_qs(urlparse(self.path).query)
                folder_raw = str(query.get("folder", [""])[0] or "").strip()
                action = str(query.get("action", ["get"])[0] or "get").strip().lower()
                if not folder_raw:
                    self.send_response(400)
                    self.send_header('Content-type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "success": False,
                        "error": "missing_folder",
                    }).encode())
                    return

                if action == "set-station-anodes":
                    raw_counts = str(query.get("station_counts", ["{}"])[0] or "{}")
                    try:
                        station_counts = json.loads(raw_counts)
                    except json.JSONDecodeError:
                        station_counts = {}
                    entry = update_folder_station_anode_counts(folder_raw, station_counts)
                elif action == "set-empty-slot":
                    slot_key = str(query.get("slot_key", [""])[0] or "").strip()
                    label = str(query.get("label", [""])[0] or "").strip()
                    empty_raw = str(query.get("empty", ["1"])[0] or "1").strip().lower()
                    empty = empty_raw in {"1", "true", "yes", "on", "empty"}
                    entry = update_folder_empty_slot(folder_raw, slot_key, label, empty)
                else:
                    entry = get_folder_processing_state(folder_raw, refresh_instant_off=True)

                log_interaction("folder_state", {
                    "action": action,
                    "folder": folder_raw,
                    "station_anode_counts": entry.get("station_anode_counts", {}),
                    "instant_off": entry.get("instant_off", {}),
                    "empty_slots": entry.get("empty_slots", {}),
                })
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": True,
                    "folder": _normalize_folder_state_key(folder_raw),
                    "state": entry,
                }).encode())
            except Exception as exc:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }).encode())
        elif self.path.startswith('/annotations'):
            try:
                query = parse_qs(urlparse(self.path).query)
                action = (query.get("action", ["add"])[0] or query.get("mode", ["add"])[0]).strip().lower()
                raw_labels = query.get("labels", [""])[0]
                label_value = query.get("label", [""])[0]
                labels = _normalize_annotation_labels(raw_labels or label_value)
                paths = _extract_paths_from_request(self.path)

                if action not in {"set", "add", "remove", "clear", "replace", "set-only"}:
                    self.send_response(400)
                    self.send_header('Content-type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "success": False,
                        "error": f"unknown_action:{action}",
                    }).encode())
                    return

                if action == "clear":
                    labels = []
                elif action in {"set", "add", "remove", "replace", "set-only"} and not labels:
                    labels = []

                if not paths:
                    self.send_response(400)
                    self.send_header('Content-type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "success": False,
                        "error": "missing_paths"
                    }).encode())
                    return

                updated = update_image_annotations(paths, labels, action)
                all_annotations = load_image_annotations()
                state_payload = _annotation_state_payload()
                annotations = {}
                for requested_path in paths:
                    norm_requested = _normalize_annotation_path(requested_path)
                    annotations[norm_requested] = all_annotations.get(norm_requested, [])
                normalized_action = "set" if action in {"set", "replace", "set-only"} else action
                log_interaction("annotations", {
                    "action": normalized_action,
                    "paths": paths,
                    "labels": labels,
                    "updated": len(updated),
                })

                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": True,
                    "action": normalized_action,
                    "updated_count": len(updated),
                    "annotations": annotations,
                    "label_counts": state_payload.get("label_counts", {}),
                }).encode())
            except Exception as exc:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }).encode())
        elif self.path.startswith('/hover?path='):
            # Track last hovered image (for Hammerspoon to use)
            path = unquote(self.path.split('path=')[1])
            log_interaction("hover_image", {"path": path, "name": Path(path).name})
            try:
                LAST_HOVER_FILE.write_text(path)
            except:
                pass
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        elif self.path.startswith('/start-drag'):
            # Reveal files in Finder for native drag - the proven working method
            paths = _extract_paths_from_request(self.path)
            request_id = str(int(time.time() * 1000))
            if not paths:
                log_interaction("reveal_for_drag", {
                    "request_id": request_id,
                    "count": 0,
                    "success": False,
                    "error": "start-drag request missing paths",
                    "method": "invalid_request",
                    "path_count": 0,
                    "valid_count": 0,
                    "invalid_count": 0,
                })
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": False,
                    "count": 0,
                    "request_id": request_id,
                    "return_code": None,
                    "method": "invalid_request",
                    "error": "No paths provided"
                }).encode())
                return

            reveal_result = reveal_files_in_finder(paths)
            success = reveal_result.get("success", False)

            log_interaction("reveal_for_drag", {
                "request_id": request_id,
                "count": len(paths),
                "success": success,
                "return_code": reveal_result.get("return_code"),
                "path_count": reveal_result.get("requested_count"),
                "valid_count": reveal_result.get("valid_count"),
                "invalid_count": reveal_result.get("invalid_count"),
                "sample_paths": reveal_result.get("sample_paths"),
                "missing_paths": reveal_result.get("missing_paths"),
                "stderr": reveal_result.get("stderr"),
                "stdout": reveal_result.get("stdout"),
                "method": reveal_result.get("method"),
                "script": reveal_result.get("script", "")[:240] if reveal_result.get("script") else None,
            })
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": success,
                "count": len(paths),
                "request_id": request_id,
                "return_code": reveal_result.get("return_code"),
                "method": reveal_result.get("method"),
                "error": reveal_result.get("error", reveal_result.get("stderr"))
            }).encode())
        elif self.path.startswith('/copy-files-to-clipboard'):
            # Copy multiple file paths to clipboard as files (macOS)
            paths = _extract_paths_from_request(self.path)
            request_id = str(int(time.time() * 1000)) + '-copy'
            copy_result = copy_files_to_clipboard(paths)
            success = copy_result.get("success", False)

            log_interaction("copy_multi_files", {
                "request_id": request_id,
                "count": len(paths),
                "success": success,
                "return_code": copy_result.get("return_code"),
                "requested_count": copy_result.get("requested_count"),
                "valid_count": copy_result.get("valid_count"),
                "invalid_count": copy_result.get("invalid_count"),
                "sample_paths": copy_result.get("sample_paths"),
                "stderr": copy_result.get("stderr"),
                "stdout": copy_result.get("stdout")
            })
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": success,
                "count": len(paths),
                "request_id": request_id,
                "return_code": copy_result.get("return_code"),
                "error": copy_result.get("error", copy_result.get("stderr"))
            }).encode())
        elif self.path.startswith('/image?path='):
            # Serve image file for lightbox viewing
            query_part = self.path.split('path=', 1)[1]
            path = unquote(query_part)
            try:
                img_path = Path(path)
                if img_path.exists():
                    content = img_path.read_bytes()
                    ext = img_path.suffix.lower()
                    content_types = {
                        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                        '.png': 'image/png', '.gif': 'image/gif',
                        '.webp': 'image/webp', '.bmp': 'image/bmp'
                    }
                    content_type = content_types.get(ext, 'image/jpeg')
                    self.send_response(200)
                    self.send_header('Content-type', content_type)
                    self.send_header('Content-Length', len(content))
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(content)
                else:
                    self.send_response(404)
                    self.end_headers()
            except Exception:
                self.send_response(500)
                self.end_headers()
        elif self.path.startswith('/thumbnail'):
            # Serve cached thumbnail bytes for grid rendering
            try:
                query = parse_qs(urlparse(self.path).query)
                path_encoded = str(query.get("path", [""])[0] or "").strip()
                raw_max_size = query.get("max_size", [None])[0]
                full_quality_raw = str(query.get("full_quality", ["0"])[0]).strip().lower()
                debug = str(query.get("debug", ["0"])[0]).strip().lower() in {"1", "true", "yes", "on"}
                full_quality = full_quality_raw in {"1", "true", "yes", "on"}

                if not path_encoded:
                    self.send_response(400)
                    self.send_header('Content-type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(b'{"error": "missing_path"}')
                    return

                try:
                    max_size = int(raw_max_size) if raw_max_size is not None else 0
                except ValueError:
                    max_size = 0
                max_size = max(0, min(max_size, 5000))
                img_path = unquote(path_encoded)
                if not Path(img_path).exists():
                    self.send_response(404)
                    self.send_header('Content-type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(b'{"error": "not_found"}')
                    return

                thumbnail_bytes = _build_thumbnail_bytes(img_path=img_path, max_size=max_size, full_quality=full_quality)
                self.send_response(200)
                self.send_header('Content-type', 'image/jpeg')
                self.send_header('Content-Length', str(len(thumbnail_bytes)))
                self.send_header('Cache-Control', 'public, max-age=3600')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(thumbnail_bytes)
                log_interaction("thumbnail_request", {
                    "path": img_path,
                    "full_quality": full_quality,
                    "max_size": max_size,
                    "bytes": len(thumbnail_bytes),
                })
            except Exception as exc:
                import traceback
                error_message = f"{type(exc).__name__}: {exc}"
                debug_payload = {
                    "error": "server_error",
                    "message": error_message,
                    "path": str(path_encoded) if "path_encoded" in locals() else None,
                    "trace": traceback.format_exc().strip(),
                }
                log_interaction("thumbnail_request_error", {
                    "path": str(path_encoded) if "path_encoded" in locals() else None,
                    "error": error_message,
                })
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                if debug:
                    self.wfile.write(json.dumps(debug_payload).encode())
                else:
                    self.wfile.write(b'{"error": "server_error"}')
        elif self.path.startswith('/rotate?path='):
            # Rotate image 90 degrees clockwise and save in place
            query_part = self.path.split('path=', 1)[1]
            path = unquote(query_part)
            success = rotate_image(Path(path))
            log_interaction("rotate_image", {"path": path, "name": Path(path).name, "success": success})
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(f'{{"success": {str(success).lower()}, "name": "{Path(path).name}"}}'.encode())
        elif self.path == '/copy-last':
            # Copy the last hovered image (called by Hammerspoon)
            try:
                path = LAST_HOVER_FILE.read_text().strip()
                success = copy_image_to_clipboard(Path(path))
                log_interaction("hammerspoon_copy_last", {"path": path, "name": Path(path).name, "success": success})
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(f'{{"success": {str(success).lower()}, "name": "{Path(path).name}"}}'.encode())
            except:
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"success": false, "name": "none"}')
        elif self.path in ['/next', '/prev', '/current', '/index']:
            # Navigate through images (keyboard control from Word)
            try:
                index_data = json.loads(IMAGE_INDEX_FILE.read_text())
                images = index_data.get('images', [])
                current = index_data.get('current', 0)

                if not images:
                    raise ValueError("No images")

                if self.path == '/next':
                    current = (current + 1) % len(images)
                    log_interaction("keyboard_nav_next", {"index": current, "total": len(images)})
                elif self.path == '/prev':
                    current = (current - 1) % len(images)
                    log_interaction("keyboard_nav_prev", {"index": current, "total": len(images)})
                # /current and /index just return current without changing

                # Update index
                index_data['current'] = current
                IMAGE_INDEX_FILE.write_text(json.dumps(index_data))

                # Copy to clipboard (skip for /index - just polling)
                img_path = images[current]
                if self.path != '/index':
                    success = copy_image_to_clipboard(Path(img_path))
                else:
                    success = True

                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "success": success,
                    "name": Path(img_path).name,
                    "path": img_path,
                    "index": current,
                    "total": len(images)
                }).encode())
            except Exception as e:
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"success": false, "name": "no images", "index": 0, "total": 0}')
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, format, *args):
        pass  # Suppress logs

    def log_error(self, format, *args):
        import sys
        print(f"HTTP ERROR: {format % args}", file=sys.stderr)

_server_ready = threading.Event()
_server_error = None
_clipboard_server_started = False
CLIPBOARD_BOOT_LOG = Path(__file__).parent / ".clipboard_server_boot.log"


def start_clipboard_server():
    global _server_error, CLIPBOARD_PORT
    requested_port = _CLIPBOARD_PORT_REQUESTED
    max_attempts = max(1, _CLIPBOARD_PORT_SCAN)
    candidate_ports = _CLIPBOARD_PORT_SCAN_LIST[:max_attempts] if _CLIPBOARD_PORT_SCAN_LIST else [
        requested_port + i for i in range(max_attempts)
    ]
    if CLIPBOARD_PORT not in candidate_ports:
        candidate_ports.insert(0, CLIPBOARD_PORT)
    _server_error = None

    for attempt, candidate in enumerate(candidate_ports):
        if candidate != CLIPBOARD_PORT:
            log_interaction("clipboard_port_resolved", {
                "reason": "runtime_fallback",
                "requested_port": requested_port,
                "selected_port": candidate,
                "attempt": attempt,
                "scan_candidates": candidate_ports,
            })
            CLIPBOARD_PORT = candidate
        try:
            with open(CLIPBOARD_BOOT_LOG, "a", encoding="utf-8") as boot_log:
                boot_log.write(f"starting::{time.time()}::127.0.0.1:{CLIPBOARD_PORT}\n")
            log_interaction("clipboard_server_start", {
                "port": CLIPBOARD_PORT,
                "attempt": attempt,
                "scan_candidates": candidate_ports,
            })
            print(f"Starting clipboard server on 127.0.0.1:{CLIPBOARD_PORT}", file=sys.stderr)
            server = HTTPServer(('127.0.0.1', CLIPBOARD_PORT), ClipboardHandler)
            with open(CLIPBOARD_BOOT_LOG, "a", encoding="utf-8") as boot_log:
                boot_log.write(f"bound::{time.time()}::127.0.0.1:{CLIPBOARD_PORT}\n")
            print(f"Clipboard server bound on 127.0.0.1:{CLIPBOARD_PORT}", file=sys.stderr)
            log_interaction("clipboard_server_bound", {
                "port": CLIPBOARD_PORT,
                "attempt": attempt,
                "scan_candidates": candidate_ports,
            })
            _server_ready.set()
            server.serve_forever()
            return
        except Exception as e:
            _server_error = f"{type(e).__name__}: {e}"
            with open(CLIPBOARD_BOOT_LOG, "a", encoding="utf-8") as boot_log:
                boot_log.write(f"failed::{time.time()}::{_server_error}\n")
            log_interaction("clipboard_server_error", {
                "port": CLIPBOARD_PORT,
                "attempt": attempt,
                "error": _server_error,
            })
            print(f"Clipboard server failed on 127.0.0.1:{CLIPBOARD_PORT}: {_server_error}", file=sys.stderr)

            if getattr(e, "errno", None) == EADDRINUSE and attempt < len(candidate_ports) - 1:
                next_port = candidate_ports[attempt + 1]
                log_interaction("clipboard_server_retry", {
                    "requested_port": requested_port,
                    "failed_port": candidate,
                    "next_port": next_port,
                })
                continue
            break

    _server_ready.set()  # Unblock waiters even on error


def _check_server_health() -> bool:
    """Quick check if clipboard server is responding."""
    import urllib.request
    import urllib.error

    global _server_error
    _server_error = None
    try:
        req = urllib.request.urlopen(f'http://127.0.0.1:{CLIPBOARD_PORT}/index', timeout=1)
        status = req.status
        if status == 200:
            log_interaction("clipboard_server_health_check", {"port": CLIPBOARD_PORT, "ok": True})
            return True
        _server_error = f"http_status_{status}"
        log_interaction("clipboard_server_health_check", {
            "port": CLIPBOARD_PORT,
            "ok": False,
            "error": _server_error,
        })
        return False
    except urllib.error.HTTPError as e:
        _server_error = f"HTTPError:{e.code}:{e.reason}"
    except urllib.error.URLError as e:
        _server_error = f"URLError:{e.reason}"
    except Exception as e:
        _server_error = f"{type(e).__name__}: {e}"
    log_interaction("clipboard_server_health_check", {
        "port": CLIPBOARD_PORT,
        "ok": False,
        "error": _server_error,
    })
    return False


# Start clipboard server in background (only once per process)
if not _clipboard_server_started:
    _clipboard_server_started = True
    threading.Thread(target=start_clipboard_server, daemon=True).start()

# File to persist last viewed folder per parent directory
MEMORY_FILE = Path(__file__).parent / ".folder_memory.json"
THUMB_CACHE_DIR = Path(__file__).parent / ".thumb_cache"
GRID_HD_DPI = _read_env_int("GRID_HD_DPI", 330)
GRID_HD_JPEG_QUALITY = _read_env_int("GRID_HD_JPEG_QUALITY", 100)
GRID_HD_SCALE = _read_env_int("GRID_HD_SCALE", 2)
GRID_HD_MAX_RENDER_SIZE = _read_env_int("GRID_HD_MAX_RENDER_SIZE", 2400)


def _effective_render_size(requested_size: int) -> int:
    """Return a high-resolution but size-capped render dimension."""
    if requested_size <= 0:
        return 0
    scaled = requested_size * max(1, GRID_HD_SCALE)
    if GRID_HD_MAX_RENDER_SIZE > 0:
        return min(scaled, GRID_HD_MAX_RENDER_SIZE)
    return scaled


@st.cache_data(show_spinner=False)
def _build_thumbnail_bytes(img_path: str, max_size: int = 400, full_quality: bool = False) -> bytes:
    """Build thumbnail bytes from disk without Streamlit cache dependencies."""
    try:
        with Image.open(img_path) as img:
            # Convert to RGB if necessary (handles RGBA, P mode, etc.)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            # In full-quality mode, keep source resolution.
            # In default HD mode, cap size for faster visible-grid rendering.
            if not full_quality and max_size:
                render_size = _effective_render_size(max_size)
                img.thumbnail((render_size, render_size), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            image_kwargs = {"format": "JPEG"}
            if full_quality:
                image_kwargs["quality"] = 100
                image_kwargs["subsampling"] = 0
            else:
                image_kwargs["quality"] = GRID_HD_JPEG_QUALITY
                image_kwargs["dpi"] = (GRID_HD_DPI, GRID_HD_DPI)
                image_kwargs["subsampling"] = 0
                image_kwargs["optimize"] = True
            img.save(buffer, **image_kwargs)
        return buffer.getvalue()
    except Exception:
        # Fall back to original if thumbnail fails
        return Path(img_path).read_bytes()


@st.cache_data(show_spinner=False)
def get_thumbnail(img_path: str, max_size: int = 400, full_quality: bool = False) -> bytes:
    """Generate and cache image bytes for grid rendering."""
    return _build_thumbnail_bytes(img_path=img_path, max_size=max_size, full_quality=full_quality)


@st.cache_data(show_spinner=False)
def get_thumbnail_quality_label(img_path: str, max_size: int = 400, full_quality: bool = False) -> str:
    """Return human-readable loaded resolution + quality mode label."""
    try:
        with Image.open(img_path) as img:
            original_width, original_height = img.size
            if not full_quality and max_size:
                render_size = _effective_render_size(max_size)
                scale = min(render_size / original_width, render_size / original_height, 1.0)
                loaded_width = max(1, int(original_width * scale))
                loaded_height = max(1, int(original_height * scale))
            else:
                loaded_width, loaded_height = original_width, original_height

            if full_quality:
                return f"Full {loaded_width}×{loaded_height}"
            return f"HD {GRID_HD_DPI}dpi {loaded_width}×{loaded_height}"
    except Exception:
        return "N/A"


def _calc_size_reduction_label(original_bytes: int, rendered_bytes: int) -> str:
    """Return a human-readable reduction percentage string."""
    if original_bytes <= 0 or rendered_bytes <= 0:
        return "N/A"
    if rendered_bytes >= original_bytes:
        return "0%"
    reduction = (1 - (rendered_bytes / original_bytes)) * 100
    return f"-{reduction:.1f}%"


def load_folder_memory() -> dict:
    """Load the folder memory from disk."""
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def get_default_parent() -> str:
    """Get the default parent directory from memory, env, or a safe local fallback."""
    memory = load_folder_memory()
    fallback = str(Path(os.getenv("REPORT_LABELER_LOCAL_PHOTO_ROOT", "~/Pictures")).expanduser())
    return memory.get("_default_parent", fallback)


def set_default_parent(path: str) -> None:
    """Save a new default parent directory."""
    memory = load_folder_memory()
    memory["_default_parent"] = path
    save_folder_memory(memory)


def save_folder_memory(memory: dict) -> None:
    """Save the folder memory to disk."""
    try:
        MEMORY_FILE.write_text(json.dumps(memory, indent=2))
    except OSError:
        pass  # Silently fail if we can't write


def _normalize_annotation_labels(values) -> list[str]:
    """Normalize user-provided labels into deterministic, validated display labels."""

    if not values:
        return []

    normalized: list[str] = []

    def split_inputs(raw) -> list[str]:
        if isinstance(raw, (list, tuple, set)):
            out = [str(v).strip() for v in raw]
            return [v for v in out if v]
        if not isinstance(raw, str):
            return [str(raw).strip()]
        if not raw:
            return []
        return [v.strip() for v in re.split(r"[|,\n;]+", raw) if v.strip()]

    def maybe_fuzzy_match(raw_label: str) -> str | None:
        target = re.sub(r"\s+", " ", raw_label.strip().lower())
        if not target:
            return None
        if target in REPORT_LABELER_PRESET_LOOKUP:
            return REPORT_LABELER_PRESET_LOOKUP[target]

        best_matches = difflib.get_close_matches(
            target,
            [label.lower() for label in REPORT_LABELER_LABEL_PRESETS],
            n=1,
            cutoff=0.78,
        )
        if best_matches:
            canonical = REPORT_LABELER_PRESET_LOOKUP[best_matches[0]]
            if canonical:
                return canonical
        return None

    def parse_table_phrase(raw_label: str) -> list[str]:
        """Parse phrases like:
            table three row 1 row 2 row 3
            table five mg 1 mg 2
            table six md 1 md 2
            table three a1 a2 b1 b2 (legacy speech variants)
            table four row 2 test station 1
        -> ['Table 3 Row 1', 'Table 3 Row 2', 'Table 3 Row 3']
        """
        normalized_phrase = raw_label.lower().strip()
        if not normalized_phrase:
            return []

        def normalize_table_token(raw_token: str) -> str | None:
            token = (raw_token or "").strip().lower()
            if not token:
                return None
            if token.isdigit() and token in REPORT_LABELER_TABLES:
                return token
            return REPORT_LABELER_TABLE_WORDS.get(token)

        def add_pair_from_digits(letter: str, digits: list[str]) -> None:
            if len(digits) < 2:
                return
            sorted_digits = sorted(set(digits[:2]))
            if len(sorted_digits) < 2:
                return
            table_num = current_table or next_table_default
            index = REPORT_LABELER_LEGACY_PAIR_MAP.get(letter.lower())
            if index and table_num in REPORT_LABELER_TABLES:
                table_rows = REPORT_LABELER_TABLE_PRESET_ROWS.get(table_num, ())
                row_index = int(index) - 1
                if 0 <= row_index < len(table_rows):
                    output.append(f"Table {table_num} {table_rows[row_index]}")
            digits.clear()

        def normalize_numeric_token(raw: str) -> str | None:
            normalized = re.sub(r"[^a-z0-9]", "", str(raw).strip().lower())
            if not normalized:
                return None

            if normalized in {"one", "won"}:
                return "1"
            if normalized in {"two", "too", "to", "tou"}:
                return "2"
            if normalized in {"three", "tree", "free"}:
                return "3"
            if normalized in {"four", "for"}:
                return "4"
            if normalized in {"five", "fife"}:
                return "5"
            if normalized in {"six", "sicks"}:
                return "6"
            if normalized in {"seven", "sevan"}:
                return "7"
            if normalized.isdigit():
                return normalized
            return None

        def emit_numbered_label(kind: str, raw_number: str, raw_station: str | None = None) -> None:
            table_num = current_table or next_table_default
            if table_num not in REPORT_LABELER_TABLES:
                return
            table_rows = REPORT_LABELER_TABLE_PRESET_ROWS.get(table_num, ())
            if not table_rows:
                return

            normalized = normalize_numeric_token(raw_number)
            if not normalized:
                return

            if not normalized.isdigit():
                return

            row_index = int(normalized) - 1
            if row_index < 0 or row_index >= len(table_rows):
                return

            if table_num == "4" and kind == "row":
                if normalized not in {"2", "3"}:
                    return
                normalized_station = normalize_numeric_token(raw_station) if raw_station else None
                station_index = 0
                if normalized_station and normalized_station.isdigit():
                    station_index = max(1, min(int(normalized_station), 2)) - 1
                column = "A" if station_index == 0 else "B"
                output.append(f"Table 4 Row {normalized} Column {column} Test Station {station_index + 1}")
                return

            if table_num in {"3", "4"} and kind in {"mg", "md"}:
                # Keep MG token compatible; route MG1..MG7 to Table rows when table matches.
                row_index = min(row_index, len(table_rows) - 1)

            station_suffixes = REPORT_LABELER_TABLE_STATION_SUFFIXES.get(table_num)
            if station_suffixes:
                normalized_station = normalize_numeric_token(raw_station) if raw_station else None
                if normalized_station and normalized_station.isdigit():
                    station_index = max(1, min(int(normalized_station), len(station_suffixes))) - 1
                else:
                    station_index = 0
                output.append(
                    f"Table {table_num} {table_rows[row_index]} "
                    f"{station_suffixes[station_index]}"
                )
                return

            output.append(f"Table {table_num} {table_rows[row_index]}")

        normalized_phrase = re.sub(r"\btable\s+(one|two|three|four|five|six|seven|eight)\b", lambda m: "table " + REPORT_LABELER_TABLE_WORDS.get(m.group(1), m.group(1)), normalized_phrase)
        normalized_phrase = normalized_phrase.replace("-", " ")
        normalized_phrase = re.sub(r"\btable(\d+)\b", r"table \1", normalized_phrase)
        normalized_phrase = re.sub(r"\s+", " ", normalized_phrase).strip()

        if "table" not in normalized_phrase:
            # Might still be a custom name like "anurkaamshan trading"
            fallback = maybe_fuzzy_match(normalized_phrase)
            return [fallback] if fallback else []

        # Normalise spaces between letter-number when speech adds a gap: "a 1" -> "a1"
        normalized_phrase = re.sub(r"\b([abc])\s+([12])\b", r"\1\2", normalized_phrase)

        tokens = normalized_phrase.split()
        output: list[str] = []
        pending: dict[str, list[str]] = {"a": [], "b": [], "c": []}
        current_table = None
        next_table_default = "3"

        i = 0
        while i < len(tokens):
            token = tokens[i]

            if token.startswith("table"):
                # table3 or table 3 / table three
                token_body = token[5:] if token.startswith("table") else ""
                explicit = normalize_table_token(token_body)
                if explicit and explicit != "table":
                    current_table = explicit
                    next_table_default = explicit
                    for v in pending.values():
                        v.clear()
                    i += 1
                    continue

                # "table" and "table 3" are handled with split tokens
                if token == "table" and i + 1 < len(tokens):
                    next_token = normalize_table_token(tokens[i + 1])
                    if next_token:
                        current_table = next_token
                        next_table_default = next_token
                        for v in pending.values():
                            v.clear()
                        i += 2
                        continue
                    i += 1
                    continue

            # Explicit pair token: a1a2
            pair_match = re.fullmatch(r"([abc])([12])\1([12])", token)
            if pair_match:
                a, n1, n2 = pair_match.groups()
                add_pair_from_digits(a, [n1, n2])
                i += 1
                continue

            row_match = re.fullmatch(r"(row|mg|md)([0-9]+)", token)
            if row_match:
                kind, number = row_match.groups()
                emit_numbered_label(kind, number)
                i += 1
                continue

            # Handle compact transcription from a single word, e.g. a1a2b2b1
            compact_pairs = re.findall(r"([abc])([12])\1([12])", token)
            if compact_pairs:
                for a, n1, n2 in compact_pairs:
                    add_pair_from_digits(a, [n1, n2])
                i += 1
                continue

            cell_match = re.fullmatch(r"([abc])([12])", token)
            if cell_match:
                letter, digit = cell_match.groups()
                pending[letter].append(digit)
                if len(pending[letter]) >= 2:
                    add_pair_from_digits(letter, pending[letter])
                i += 1
                continue

            # Handle transposed token fragments e.g. "a 1"
            if re.fullmatch(r"[abc]", token) and i + 1 < len(tokens) and re.fullmatch(r"[12]", tokens[i + 1]):
                combined = f"{token}{tokens[i + 1]}"
                cell_match = re.fullmatch(r"([abc])([12])", combined)
                if cell_match:
                    letter, digit = cell_match.groups()
                    pending[letter].append(digit)
                    if len(pending[letter]) >= 2:
                        add_pair_from_digits(letter, pending[letter])
                    i += 2
                    continue

            if token in {"row", "mg", "md"} and i + 1 < len(tokens):
                current = current_table or next_table_default
                station_suffixes = REPORT_LABELER_TABLE_STATION_SUFFIXES.get(current)
                next_i = i + 2
                row_number = tokens[i + 1]
                station_number = None

                if current in REPORT_LABELER_TABLES and station_suffixes:
                    # row 2 station 1
                    if (
                        i + 3 < len(tokens)
                        and tokens[i + 2] in {"station", "teststation", "testation"}
                        and tokens[i + 3]
                    ):
                        station_number = tokens[i + 3]
                        next_i = i + 4

                    # row 2 test station 1
                    elif (
                        i + 4 < len(tokens)
                        and tokens[i + 2] in {"test", "teststation"}
                        and tokens[i + 3] == "station"
                        and tokens[i + 4]
                    ):
                        station_number = tokens[i + 4]
                        next_i = i + 5

                emit_numbered_label(token, row_number, station_number)
                i = next_i - 1
                continue

            # Handle table tokens separated from table keyword, e.g. "... table 4 ..."
            token_table = normalize_table_token(token)
            if token_table in REPORT_LABELER_TABLES:
                current_table = token_table
                next_table_default = token_table
                for v in pending.values():
                    v.clear()
                i += 1
                continue

            # Ignore separator-like speech artifacts.
            if token in {"and", "comma", "plus", "with", "the", "a", "an"}:
                i += 1
                continue

            # Any unmatched long phrase may still map to custom preset
            fallback = maybe_fuzzy_match(token)
            if fallback:
                output.append(fallback)
            i += 1

        # Flush any complete pairs in pending state
        for letter, digits in pending.items():
            if len(digits) >= 2:
                add_pair_from_digits(letter, digits)

        # Remove duplicates while preserving order
        deduped = []
        seen: set[str] = set()
        for label in output:
            if label in seen:
                continue
            seen.add(label)
            deduped.append(label)
        return deduped[:1]

    # Parse each input chunk and normalize tokens
    for value in split_inputs(values):
        exact = REPORT_LABELER_PRESET_LOOKUP.get(value.lower())
        if exact:
            normalized.append(exact)
            continue

        if re.match(r"\s*table\s+\d+\b", value, flags=re.IGNORECASE) and re.search(
            r"\b(?:col|column)\s*[ab]\b|\bts\s*\d+\b|\btotal\s+current\b|\bshunt\s+reading\b|\bopen\s+potential\b|\bcp\s+anode\b",
            value,
            flags=re.IGNORECASE,
        ):
            normalized.append(re.sub(r"\s+", " ", value.strip()))
            continue

        parsed = parse_table_phrase(value)
        if parsed:
            normalized.extend(parsed)
            continue

        # fallback to exact/fuzzy single-label
        fallback = maybe_fuzzy_match(value)
        if fallback:
            normalized.append(fallback)
            continue

        if len(value.strip()) > 2:
            normalized.append(re.sub(r"\s+", " ", value.strip()))

    # Deterministic output with a compact dedupe
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in normalized:
        label = value.strip()
        if not label:
            continue
        if label in seen:
            continue
        seen.add(label)
        cleaned.append(label)
    return cleaned[:1]


def _label_to_color(label: str) -> str:
    """Map a label to a deterministic pastel color."""
    if not label:
        return "hsl(152, 72%, 42%)"
    row_match = re.search(r"(?:Row|MG)\s+(\d+)", label, flags=re.IGNORECASE)
    if row_match:
        color = REPORT_LABELER_ROW_LABEL_COLOR_PALETTE.get(row_match.group(1))
        if color:
            return color
    digest = hashlib.sha256(label.encode("utf-8", errors="ignore")).hexdigest()
    hue = int(digest[:6], 16) % 360
    return f"hsl({hue}, 72%, 42%)"


def _first_video_file_in_folder(folder: Path) -> Path | None:
    """Return first video file from the folder tree in filename order."""
    try:
        video_candidates: list[Path] = []
        for entry in folder.rglob("*"):
            if entry.is_file() and entry.suffix.lower() in REPORT_LABELER_VIDEO_EXTENSIONS:
                video_candidates.append(entry)
        if not video_candidates:
            return None
        video_candidates.sort(key=lambda item: (item.name.lower(), str(item).lower()))
        return video_candidates[0]
    except (OSError, PermissionError):
        return None


def _resolve_instant_off_status(folder: Path) -> dict[str, str]:
    """Compute folder-level instant-off status from media presence."""
    first_video = _first_video_file_in_folder(folder)
    if first_video:
        return {
            "status": "Yes Video Exists",
            "video_file": first_video.name,
        }
    return {
        "status": "No Video Exists",
        "video_file": "",
    }


def _normalize_folder_state_key(folder: str | Path) -> str:
    return str(Path(folder).expanduser().resolve())


def _folder_state_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def load_folder_processing_state() -> dict:
    """Load durable folder-level metadata used by downstream report processing."""
    if not FOLDER_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(FOLDER_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_folder_processing_state(state: dict) -> None:
    try:
        FOLDER_STATE_FILE.write_text(json.dumps(state or {}, indent=2, sort_keys=True))
    except OSError:
        pass


def _normalize_station_key(station: str) -> str:
    raw = str(station or "").strip()
    if raw in {"1", "2"}:
        return raw
    match = re.search(r"(?:test\s*station|testation|station)\s*(\d+)", raw, flags=re.IGNORECASE)
    if match and match.group(1) in {"1", "2"}:
        return match.group(1)
    return ""


def _normalize_station_anode_counts(raw_counts) -> dict[str, int]:
    if not isinstance(raw_counts, dict):
        return {}
    normalized: dict[str, int] = {}
    for raw_key, raw_value in raw_counts.items():
        station_key = _normalize_station_key(str(raw_key))
        if not station_key:
            continue
        try:
            count = int(raw_value)
        except (TypeError, ValueError):
            continue
        if count in {3, 4}:
            normalized[station_key] = count
    return normalized


def _normalize_empty_slot_key(label_or_key: str) -> str:
    return re.sub(r"\s+", " ", str(label_or_key or "").strip().lower())


def _normalize_empty_slots(raw_slots) -> dict[str, dict[str, str]]:
    if not isinstance(raw_slots, dict):
        return {}
    normalized: dict[str, dict[str, str]] = {}
    for raw_key, raw_value in raw_slots.items():
        slot_key = _normalize_empty_slot_key(str(raw_key))
        if not slot_key:
            continue
        if isinstance(raw_value, dict):
            label = str(raw_value.get("label") or "").strip()
            value = str(raw_value.get("value") or "-").strip() or "-"
            updated_at = str(raw_value.get("updated_at") or "").strip()
        else:
            label = str(raw_value or "").strip()
            value = "-"
            updated_at = ""
        normalized[slot_key] = {
            "label": label,
            "value": value,
            "updated_at": updated_at,
        }
    return normalized


def _normalize_folder_processing_entry(entry) -> dict:
    source = entry if isinstance(entry, dict) else {}
    instant_source = source.get("instant_off") if isinstance(source.get("instant_off"), dict) else {}
    instant_status = str(instant_source.get("status") or "").strip()
    if instant_status not in REPORT_LABELER_INSTANT_OFF_STATUS_LABELS:
        instant_status = "No Video Exists"
    return {
        "instant_off": {
            "status": instant_status,
            "video_file": str(instant_source.get("video_file") or "").strip(),
            "source": str(instant_source.get("source") or "filesystem_scan").strip(),
            "updated_at": str(instant_source.get("updated_at") or "").strip(),
        },
        "station_anode_counts": _normalize_station_anode_counts(source.get("station_anode_counts")),
        "empty_slots": _normalize_empty_slots(source.get("empty_slots")),
    }


def get_folder_processing_state(folder: str | Path, refresh_instant_off: bool = True) -> dict:
    """Return and persist the durable state for one report folder."""
    key = _normalize_folder_state_key(folder)
    state = load_folder_processing_state()
    entry = _normalize_folder_processing_entry(state.get(key))

    if refresh_instant_off:
        instant = _resolve_instant_off_status(Path(key))
        entry["instant_off"] = {
            "status": instant.get("status", "No Video Exists"),
            "video_file": instant.get("video_file", ""),
            "source": "filesystem_scan",
            "updated_at": _folder_state_timestamp(),
        }

    if state.get(key) != entry:
        state[key] = entry
        save_folder_processing_state(state)
    return entry


def update_folder_station_anode_counts(folder: str | Path, counts) -> dict:
    key = _normalize_folder_state_key(folder)
    state = load_folder_processing_state()
    entry = _normalize_folder_processing_entry(state.get(key))
    entry["station_anode_counts"] = _normalize_station_anode_counts(counts)
    state[key] = entry
    save_folder_processing_state(state)
    return entry


def update_folder_empty_slot(folder: str | Path, slot_key: str, label: str, empty: bool) -> dict:
    key = _normalize_folder_state_key(folder)
    normalized_slot_key = _normalize_empty_slot_key(slot_key or label)
    state = load_folder_processing_state()
    entry = _normalize_folder_processing_entry(state.get(key))
    slots = dict(entry.get("empty_slots") or {})
    if empty and normalized_slot_key:
        slots[normalized_slot_key] = {
            "label": str(label or "").strip(),
            "value": "-",
            "updated_at": _folder_state_timestamp(),
        }
    elif normalized_slot_key:
        slots.pop(normalized_slot_key, None)
    entry["empty_slots"] = slots
    state[key] = entry
    save_folder_processing_state(state)
    return entry


def _normalize_annotation_key(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def load_image_annotations() -> dict[str, list[str]]:
    """Load per-image annotation labels."""
    if not ANNOTATION_METADATA_FILE.exists():
        return {}
    try:
        data = json.loads(ANNOTATION_METADATA_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {
        _normalize_annotation_key(str(path)): _normalize_annotation_stored(labels)
        for path, labels in (data.items() if isinstance(data, dict) else {})
        if isinstance(path, str)
    }


def _save_image_annotations(data: dict[str, list[str]]) -> None:
    try:
        ANNOTATION_METADATA_FILE.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def update_image_annotations(paths: list[str], labels: list[str], action: str) -> dict[str, list[str]]:
    """Update annotations for provided paths and return updated labels map."""
    action = (action or "add").strip().lower()
    raw_labels = _normalize_annotation_stored(labels)
    normalized_labels = _normalize_annotation_labels(labels)
    write_labels = normalized_labels or raw_labels
    current = load_image_annotations()
    result: dict[str, list[str]] = {}
    changed = False
    normalized_paths = [_normalize_annotation_path(path) for path in paths]

    for norm_path in normalized_paths:
        if not Path(norm_path).exists():
            continue
        existing = _normalize_annotation_stored(current.get(norm_path, []))

        if action in {"set", "replace", "set-only"}:
            next_labels = write_labels[:1]
        elif action == "add":
            next_labels = write_labels[:1]
        elif action == "remove":
            remove_keys = {
                _normalize_label_compare(label)
                for label in [*raw_labels, *normalized_labels]
                if _normalize_label_compare(label)
            }
            next_labels = [
                label for label in existing
                if _normalize_label_compare(label) not in remove_keys
            ]
            if next_labels == existing and len(existing) == 1 and remove_keys:
                next_labels = []
        elif action == "clear":
            next_labels = []
        else:
            next_labels = existing

        if next_labels != existing:
            changed = True
            current[norm_path] = next_labels
            result[norm_path] = next_labels

    if changed:
        _save_image_annotations(current)
    return result


def _normalize_label_compare(label: str) -> str:
    return re.sub(r"\s+", " ", str(label or "").strip()).lower()


def rename_image_annotation_label(old_labels: list[str], new_label: str) -> dict[str, list[str]]:
    """Replace a label globally in persistent image annotations."""
    target = str(new_label or "").strip()
    match_keys = {
        _normalize_label_compare(label)
        for label in old_labels
        if _normalize_label_compare(label)
    }
    if not match_keys or not target:
        return {}

    current = load_image_annotations()
    updated: dict[str, list[str]] = {}
    changed = False

    for path, labels in current.items():
        next_labels: list[str] = []
        seen: set[str] = set()
        touched = False
        for label in _normalize_annotation_stored(labels):
            replacement = target if _normalize_label_compare(label) in match_keys else label
            if replacement != label:
                touched = True
            replacement_key = _normalize_label_compare(replacement)
            if not replacement_key or replacement_key in seen:
                continue
            seen.add(replacement_key)
            next_labels.append(replacement)

        if touched:
            changed = True
            current[path] = next_labels
            updated[path] = next_labels

    if changed:
        _save_image_annotations(current)
    return updated


def _normalize_annotation_path(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def get_last_folder_index(parent_dir: str, subdirs: List[Path]) -> int:
    """Get the last viewed folder index for a parent directory."""
    memory = load_folder_memory()
    last_folder_name = memory.get(parent_dir)
    if last_folder_name:
        for i, subdir in enumerate(subdirs):
            if subdir.name == last_folder_name:
                return i
    return 0


def remember_folder(parent_dir: str, folder_name: str) -> None:
    """Remember the last viewed folder for a parent directory."""
    memory = load_folder_memory()
    old_folder = memory.get(parent_dir)
    if old_folder != folder_name:
        log_interaction("folder_change", {"parent": parent_dir, "from": old_folder, "to": folder_name})
    memory[parent_dir] = folder_name
    save_folder_memory(memory)


def strip_quotes(path_str: str) -> str:
    """Remove surrounding single or double quotes from a path string."""
    s = path_str.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1]
    return s


def list_images(base: Path, recursive: bool, sort_by: str = "name") -> List[Path]:
    """Return sorted list of image files under base."""
    exts = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
    if not base.is_dir():
        return []
    if recursive:
        paths = [p for p in base.rglob("*") if p.suffix.lower() in exts]
    else:
        paths = [p for p in base.iterdir() if p.is_file() and p.suffix.lower() in exts]
    if sort_by == "modified":
        return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)
    return sorted(paths)


def list_adjacent_folder_preload_images(
    folders: List[Path],
    current_index: int,
    recursive: bool,
    sort_by: str,
    radius: int = ADJACENT_FOLDER_PRELOAD_RADIUS,
    per_folder_limit: int = ADJACENT_FOLDER_PRELOAD_IMAGE_LIMIT,
) -> list[Path]:
    """Return nearby folder images to warm thumbnail/browser cache without changing active folder."""
    if not folders:
        return []
    try:
        current_index = int(current_index)
    except (TypeError, ValueError):
        return []
    if current_index < 0 or current_index >= len(folders):
        return []

    ordered_indexes: list[int] = []
    for distance in range(1, max(0, radius) + 1):
        for idx in (current_index + distance, current_index - distance):
            if 0 <= idx < len(folders) and idx not in ordered_indexes:
                ordered_indexes.append(idx)

    preload_paths: list[Path] = []
    for idx in ordered_indexes:
        try:
            preload_paths.extend(list_images(folders[idx], recursive, sort_by)[:per_folder_limit])
        except Exception:
            continue
    return preload_paths


def open_file_in_os(path: Path) -> None:
    """Open a file in the OS default viewer (local use only)."""
    try:
        system = platform.system()
        if system == "Darwin":  # macOS
            subprocess.Popen(["open", str(path)])
        elif system == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:  # Linux / other
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not open file: {exc}")


def reveal_in_finder(path: Path) -> None:
    """Reveal the file or folder in the system file manager."""
    log_interaction("reveal_in_finder", {"path": str(path), "name": path.name})
    try:
        system = platform.system()
        if system == "Darwin":  # macOS
            subprocess.Popen(["open", "-R", str(path)])
        elif system == "Windows":
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:  # Linux / other
            # Open the parent directory
            subprocess.Popen(["xdg-open", str(path.parent if path.is_file() else path)])
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not reveal in file manager: {exc}")


def rotate_image(path: Path) -> bool:
    """Rotate image 90 degrees clockwise and save in place."""
    try:
        with Image.open(path) as img:
            rotated = img.rotate(-90, expand=True)
            rotated.save(path)
        # Clear the thumbnail cache for this image
        get_thumbnail.clear()
        get_thumbnail_quality_label.clear()
        return True
    except Exception:
        return False


def reveal_files_in_finder(paths: list) -> dict:
    """Reveal files in Finder, selected and ready to drag.
    This is the proven working method - drag from Finder to any app."""
    try:
        if not paths:
            return {
                "success": False,
                "requested_count": 0,
                "valid_count": 0,
                "invalid_count": 0,
                "error": "no paths provided",
                "sample_paths": [],
                "missing_paths": [],
                "return_code": None,
                "stdout": "",
                "stderr": "no paths provided",
                "script": "",
                "method": "invalid_request",
            }

        valid_paths, invalid_paths = _normalize_existing_paths(paths)
        if not valid_paths:
            return {
                "success": False,
                "requested_count": len(paths),
                "valid_count": 0,
                "invalid_count": len(invalid_paths),
                "error": "no valid paths found",
                "sample_paths": [],
                "missing_paths": invalid_paths,
                "return_code": None,
                "stdout": "",
                "stderr": "no valid paths found",
                "script": "",
                "method": "invalid_paths",
            }

        # Primary path: use `open -R` directly to avoid AppleScript parsing issues.
        # This reliably reveals files in Finder in many environments.
        reveal_result = subprocess.run(
            ["open", "-R", *valid_paths],
            capture_output=True,
            text=True
        )

        # Always perform a Finder activation pass right after open.
        # This makes drag targets visible when the Finder session is backgrounded.
        safe_paths = [str(p).replace('\\', '\\\\').replace('"', '\\"') for p in valid_paths]
        file_list = ", ".join([f'POSIX file "{p}" as alias' for p in safe_paths])
        script = (
            "set theFiles to {" + file_list + "}\n"
            "tell application \"Finder\"\n"
            "  activate\n"
            "  reveal theFiles\n"
            "  set selection to theFiles\n"
            "end tell"
        )
        activate_result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)

        method = "open_with_osascript_activate"
        if reveal_result.returncode != 0 and activate_result.returncode != 0:
            method = "open_failed"
        elif reveal_result.returncode != 0:
            method = "open_failed_with_activation_only"
        elif activate_result.returncode != 0:
            method = "open_with_activation_failed"

        # Keep a strict AppleScript fallback only if both commands fail.
        if reveal_result.returncode != 0 and activate_result.returncode != 0:
            # Use per-path reveal as one more safe fallback for odd-path edge cases.
            for single_path in valid_paths:
                fallback_result = subprocess.run(
                    ["open", "-R", single_path],
                    capture_output=True,
                    text=True
                )
                if fallback_result.returncode == 0:
                    activate_result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
                    reveal_result = fallback_result
                    method = "single_open_fallback_with_activation"
                    break
            else:
                single_script = (
                    "set theFiles to {" + ", ".join([f'POSIX file "{p}" as alias' for p in safe_paths]) + "}\n"
                    "tell application \"Finder\"\n"
                    "  activate\n"
                    "  reveal theFiles\n"
                    "  set selection to theFiles\n"
                    "end tell"
                )
                reveal_result = subprocess.run(["osascript", "-e", single_script], capture_output=True, text=True)
                method = "final_osascript_fallback"

        return {
            "success": reveal_result.returncode == 0,
            "requested_count": len(paths),
            "valid_count": len(valid_paths),
            "invalid_count": len(invalid_paths),
            "sample_paths": valid_paths[:10],
            "missing_paths": invalid_paths[:10],
            "return_code": reveal_result.returncode,
            "stdout": (reveal_result.stdout[:1200] if reveal_result.stdout else ""),
            "stderr": (reveal_result.stderr[:1200] if reveal_result.stderr else ""),
            "error": reveal_result.stderr[:1200] if reveal_result.returncode != 0 else None,
            "script": script[:1200],
            "method": method
        }
    except Exception:
        valid_paths = locals().get("valid_paths", [])
        return {
            "success": False,
            "requested_count": len(paths),
            "valid_count": 0,
            "invalid_count": len(paths) - len(valid_paths),
            "error": "reveal_files_in_finder exception",
            "sample_paths": [],
            "missing_paths": valid_paths,
            "return_code": None,
            "stdout": "",
            "stderr": "reveal_files_in_finder exception",
            "script": "",
            "method": "exception",
        }


def copy_files_to_clipboard(paths: list) -> dict:
    """Copy multiple files to clipboard as file references (macOS only).
    This allows pasting into Finder or other apps that accept files."""
    try:
        if not paths:
            return {
                "success": False,
                "requested_count": 0,
                "valid_count": 0,
                "invalid_count": 0,
                "sample_paths": [],
                "error": "no paths provided",
                "return_code": None,
                "stdout": "",
                "stderr": "no paths provided",
            }

        system = platform.system()
        if system == "Darwin":
            valid_paths, invalid_paths = _normalize_existing_paths(paths)
            if not valid_paths:
                return {
                    "success": False,
                    "requested_count": len(paths),
                    "valid_count": 0,
                    "invalid_count": len(invalid_paths),
                    "sample_paths": [],
                    "error": "no valid paths found",
                    "return_code": None,
                    "stdout": "",
                    "stderr": "no valid paths found",
                }

            # Build AppleScript to set clipboard to list of file references
            file_ref_terms = []
            for p in valid_paths:
                safe_p = str(p).replace('\\', '\\\\').replace('"', '\\"')
                file_ref_terms.append(f'(POSIX file "{safe_p}")')
            file_refs = ', '.join(file_ref_terms)
            script = f'''
            set theFiles to {{{file_refs}}}
            set the clipboard to theFiles
            '''
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True
            )
            return {
                "success": result.returncode == 0,
                "requested_count": len(paths),
                "valid_count": len(valid_paths),
                "invalid_count": len(invalid_paths),
                "sample_paths": valid_paths[:10],
                "return_code": result.returncode,
                "stdout": result.stdout[:1200],
                "stderr": result.stderr[:1200],
                "error": result.stderr[:1200] if result.returncode != 0 else None,
            }
        else:
            return {
                "success": False,
                "requested_count": len(paths),
                "valid_count": 0,
                "invalid_count": len(paths),
                "sample_paths": [],
                "error": "non-macos platform",
                "return_code": None,
                "stdout": "",
                "stderr": "non-macos platform"
            }
    except Exception:
        return {
            "success": False,
            "requested_count": len(paths),
            "valid_count": 0,
            "invalid_count": len(paths),
            "sample_paths": [],
            "error": "copy_files_to_clipboard exception",
            "return_code": None,
            "stdout": "",
            "stderr": "copy_files_to_clipboard exception",
        }

def copy_image_to_clipboard(path: Path) -> bool:
    """Copy the original image to clipboard (macOS only)."""
    try:
        system = platform.system()
        if system == "Darwin":
            # Use osascript to copy image file to clipboard
            script = f'''
            set the clipboard to (read (POSIX file "{path}") as «class PNGf»)
            '''
            # Try PNG first, fall back to JPEG
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                # Try as JPEG
                script = f'''
                set the clipboard to (read (POSIX file "{path}") as JPEG picture)
                '''
                result = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True,
                    text=True
                )
            return result.returncode == 0
        else:
            return False
    except Exception:
        return False


def list_directories(parent: Path, sort_by: str = "name") -> List[Path]:
    """Return sorted list of subdirectories under parent."""
    if not parent.is_dir():
        return []
    dirs = [p for p in parent.iterdir() if p.is_dir() and not p.name.startswith(".")]
    if sort_by == "modified":
        return sorted(dirs, key=lambda p: p.stat().st_mtime, reverse=True)
    return sorted(dirs, key=lambda p: p.name.lower())


def _normalize_folder_status_labels(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        value = str(item or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def _load_annotation_labels_by_path() -> dict[str, list[str]]:
    try:
        data = json.loads(ANNOTATION_METADATA_FILE.read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(path): _normalize_folder_status_labels(labels) for path, labels in data.items()}


def _folder_status_number_token(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    words = {
        "zero": "0",
        "one": "1",
        "two": "2",
        "three": "3",
        "four": "4",
        "five": "5",
        "six": "6",
        "seven": "7",
        "eight": "8",
    }
    if value.isdigit():
        return str(int(value))
    return words.get(re.sub(r"[^a-z]", "", value), "")


def _folder_status_label_signature(label: str) -> tuple | None:
    text = re.sub(r"\s+", " ", str(label or "")).strip().lower()
    if not text:
        return None
    text = re.sub(r"\btestation\b", "test station", text)
    text = re.sub(r"\bts\s*(\d+)\b", r"test station \1", text)
    text = re.sub(r"\bcol\s*([a-z])\b", r"column \1", text)
    table_match = re.search(r"\btable\s*(\d+|one|two|three|four|five|six)\b", text)
    if not table_match:
        return None
    table = _folder_status_number_token(table_match.group(1))
    if not table:
        return None
    row_match = re.search(r"\b(?:row|mg|md)\s*(\d+|one|two|three|four|five|six|seven|eight)\b", text)
    row = _folder_status_number_token(row_match.group(1)) if row_match else ""
    station_match = re.search(r"\btest\s*station\s*(\d+|one|two)\b", text)
    station = _folder_status_number_token(station_match.group(1)) if station_match else ""
    column_match = re.search(r"\bcolumn\s*([ab])\b", text)
    column = column_match.group(1).upper() if column_match else ""
    if table == "4":
        if not column:
            column = "A" if station == "1" else ("B" if station == "2" else "")
        return (table, row, column, station)
    if table in {"5", "6"}:
        return (table, row, "", station)
    return (table, row, "", "")


def _is_unique_table4_status_label(label: str) -> bool:
    signature = _folder_status_label_signature(label)
    return bool(signature and signature[0] == "4" and signature[1] and signature[2] and signature[3])


def _folder_status_anode_count(raw) -> int:
    if isinstance(raw, int):
        return raw if raw in (3, 4) else 0
    if isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
        return value if value in (3, 4) else 0
    if isinstance(raw, dict):
        values = [_folder_status_anode_count(value) for value in raw.values()]
        values = [value for value in values if value]
        return max(values) if values else 0
    return 0


def _folder_status_station_counts(folder: Path) -> dict[str, int]:
    try:
        state = get_folder_processing_state(folder, refresh_instant_off=False)
    except Exception:
        state = {}
    raw_counts = state.get("station_anode_counts", {}) if isinstance(state, dict) else {}
    if not isinstance(raw_counts, dict):
        raw_counts = {}
    return {
        "1": _folder_status_anode_count(raw_counts.get("1") or raw_counts.get(1)),
        "2": _folder_status_anode_count(raw_counts.get("2") or raw_counts.get(2)),
    }


def _folder_required_status_labels(folder: Path) -> list[str]:
    required = [
        "Table 3 Row 1",
        "Table 3 Row 2",
        "Table 3 Row 3",
        "Table 3 Row 4",
        "Table 4 Row 2 Column A Test Station 1",
        "Table 4 Row 3 Column A Test Station 1",
        "Table 4 Row 2 Column B Test Station 2",
        "Table 4 Row 3 Column B Test Station 2",
    ]
    counts = _folder_status_station_counts(folder)
    station_one_count = counts.get("1", 0)
    station_two_count = counts.get("2", 0)
    if not station_one_count:
        required.append("Test Station 1 anode count")
    if not station_two_count:
        required.append("Test Station 2 anode count")
    if station_one_count:
        for table in ("5", "6"):
            for mg in range(1, station_one_count + 1):
                required.append(f"Table {table} MG {mg} Test Station 1")
    if station_two_count:
        start_mg = (station_one_count or 3) + 1
        end_mg = min(7, start_mg + station_two_count - 1)
        for table in ("5", "6"):
            for mg in range(start_mg, end_mg + 1):
                required.append(f"Table {table} MG {mg} Test Station 2")
    return required


def _short_folder_missing_label(label: str) -> str:
    text = str(label or "").strip()
    text = re.sub(r"^Table\s+", "T", text, flags=re.I)
    text = re.sub(r"\s+Test\s+Station\s+", " TS", text, flags=re.I)
    text = re.sub(r"\s+Column\s+", " Col ", text, flags=re.I)
    return text


def _folder_status_slot_key(label: str) -> str:
    return _normalize_empty_slot_key(label)


def _folder_status_empty_slot_keys(folder: Path) -> set[str]:
    try:
        state = get_folder_processing_state(folder, refresh_instant_off=False)
    except Exception:
        state = {}
    slots = state.get("empty_slots", {}) if isinstance(state, dict) else {}
    if not isinstance(slots, dict):
        return set()
    return {_normalize_empty_slot_key(key) for key in slots.keys() if _normalize_empty_slot_key(key)}


def get_folder_label_status(folder: Path, annotations: dict[str, list[str]] | None = None) -> dict:
    annotation_map = annotations if annotations is not None else _load_annotation_labels_by_path()
    try:
        image_paths = [str(path.expanduser().resolve()) for path in list_images(folder, False, "name")]
    except Exception:
        image_paths = []
    image_path_set = set(image_paths)

    labels_by_path: dict[str, list[str]] = {}
    for raw_path, labels in annotation_map.items():
        try:
            resolved = str(Path(raw_path).expanduser().resolve())
        except Exception:
            resolved = str(raw_path)
        if resolved in image_path_set:
            labels_by_path[resolved] = _normalize_folder_status_labels(labels)

    labeled_count = sum(1 for path in image_paths if labels_by_path.get(path))
    multi_label_count = sum(1 for labels in labels_by_path.values() if len(labels) > 1)
    table4_counts: dict[tuple, int] = {}
    actual_signatures = set()
    for labels in labels_by_path.values():
        for label in labels:
            signature = _folder_status_label_signature(label)
            if signature:
                actual_signatures.add(signature)
            if signature and signature[0] == "4":
                table4_counts[signature] = table4_counts.get(signature, 0) + 1
    duplicate_unique_count = sum(count - 1 for count in table4_counts.values() if count > 1)
    error_count = multi_label_count + duplicate_unique_count
    image_count = len(image_paths)

    required_labels = _folder_required_status_labels(folder)
    empty_slot_keys = _folder_status_empty_slot_keys(folder)
    missing_labels = [
        label for label in required_labels
        if label.endswith("anode count")
        or (
            _folder_status_slot_key(label) not in empty_slot_keys
            and _folder_status_label_signature(label) not in actual_signatures
        )
    ]
    # Anode count pseudo-labels are only missing when explicitly appended by _folder_required_status_labels.
    missing_count = len(missing_labels)
    handled_count = labeled_count + len(empty_slot_keys)
    try:
        instant_off_status = _resolve_instant_off_status(folder)
    except Exception:
        instant_off_status = {"status": "No Video Exists", "video_file": ""}
    video_exists = str(instant_off_status.get("status") or "") == "Yes Video Exists"

    if error_count:
        state = "errors"
    elif handled_count <= 0:
        state = "unlabeled"
    elif missing_count == 0:
        state = "fully labeled"
    elif missing_count == 1:
        state = "missing-one"
    else:
        state = "missing"

    return {
        "state": state,
        "image_count": image_count,
        "labeled_count": labeled_count,
        "required_count": len(required_labels),
        "missing_count": missing_count,
        "missing_labels": missing_labels,
        "multi_label_count": multi_label_count,
        "duplicate_unique_count": duplicate_unique_count,
        "error_count": error_count,
        "video_exists": video_exists,
        "video_file": str(instant_off_status.get("video_file") or "").strip(),
    }


def format_folder_status_tag(status: dict | None) -> str:
    status = status or {}
    state = str(status.get("state") or "unlabeled")
    errors = int(status.get("error_count") or 0)
    missing = int(status.get("missing_count") or 0)
    if errors:
        return f"errors {errors}"
    if state == "fully labeled":
        return "fully labeled"
    if state == "missing-one":
        labels = status.get("missing_labels") or []
        return "missing " + _short_folder_missing_label(str(labels[0] if labels else "1 label"))
    if missing:
        return f"missing {missing}"
    return "unlabeled"


def format_folder_video_tag(status: dict | None) -> str:
    return "video" if bool((status or {}).get("video_exists")) else "no video"


def folder_status_marker(status: dict | None) -> str:
    state = str((status or {}).get("state") or "unlabeled")
    if state == "fully labeled":
        return "GREEN"
    if state in {"missing", "missing-one"}:
        return "AMBER"
    if state == "errors":
        return "RED"
    return "GRAY"


def format_folder_with_label_status(folder: Path, status: dict | None = None) -> str:
    return f"{folder.name}    — {format_folder_status_tag(status)} · {format_folder_video_tag(status)}"


def render_folder_status_nav_item(folder: Path, index: int, status: dict | None = None, is_current: bool = False) -> str:
    """Render one folder navigation item. Used by both folder selectors to keep a 1:1 mapping."""
    classes = [
        "folder-status-item",
        folder_status_css_class(status),
        folder_video_css_class(status),
    ]
    if is_current:
        classes.append("current")
    href = f"?folder_index={index}&folder_nav_token={index}"
    return (
        '<a class="' + html.escape(" ".join(classes)) + '" '
        'href="' + html.escape(href) + '" '
        'data-folder-index="' + str(index) + '">'
        '<span class="folder-status-item-name">' + html.escape(folder.name) + '</span>'
        '<span class="folder-status-tags">'
        '<span class="folder-status-item-tag">' + html.escape(format_folder_status_tag(status)) + '</span>'
        '<span class="folder-status-video-tag">' + html.escape(format_folder_video_tag(status)) + '</span>'
        '</span>'
        '</a>'
    )


def folder_status_css_class(status: dict | None) -> str:
    state = str((status or {}).get("state") or "unlabeled").lower().replace(" ", "-")
    return f"folder-status-{state}"


def folder_video_css_class(status: dict | None) -> str:
    return "folder-video-yes" if bool((status or {}).get("video_exists")) else "folder-video-no"

def _load_ui_state_file() -> dict:
    if UI_STATE_FILE.exists():
        try:
            return json.loads(UI_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_ui_last_state(state: dict) -> None:
    try:
        data = _load_ui_state_file()
        data["last_state"] = state
        UI_STATE_FILE.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def _save_ui_defaults(state: dict) -> None:
    try:
        data = _load_ui_state_file()
        data["defaults"] = state
        UI_STATE_FILE.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def _get_ui_defaults() -> dict:
    data = _load_ui_state_file()
    factory = {
        "parent_dir": get_default_parent(),
        "folder_sort": "name",
        "cols_per_row": 4,
        "size_mode": "Stretch to column",
        "fixed_width": 600,
        "recursive": False,
        "sort_by": "name",
        "auto_copy_on_hover": False,
        "render_full_quality": False,
        "shift_arrow_folder_nav": False,
    }
    saved = data.get("defaults")
    if saved:
        factory.update(saved)
    return factory


def _get_ui_last_state() -> dict:
    data = _load_ui_state_file()
    last = data.get("last_state")
    defaults = _get_ui_defaults()
    if last:
        defaults.update(last)
    return defaults


# Session state key → state file key mapping
_STATE_KEYS = {
    "ui_parent_dir": "parent_dir",
    "folder-sort": "folder_sort",
    "ui_cols_per_row": "cols_per_row",
    "ui_size_mode": "size_mode",
    "ui_fixed_width": "fixed_width",
    "ui_recursive": "recursive",
    "ui_sort_by": "sort_by",
    "ui_auto_copy": "auto_copy_on_hover",
    "ui_render_full_quality": "render_full_quality",
    "shift_arrow_folder_nav": "shift_arrow_folder_nav",
}


def _apply_state_to_session(state: dict) -> None:
    """Write state dict values into session_state widget keys."""
    defaults = _get_ui_defaults()
    for ss_key, state_key in _STATE_KEYS.items():
        if state_key in state:
            st.session_state[ss_key] = state[state_key]
        elif state_key in defaults:
            st.session_state[ss_key] = defaults[state_key]


def _gather_current_state() -> dict:
    """Read current widget values from session_state."""
    return {state_key: st.session_state.get(ss_key)
            for ss_key, state_key in _STATE_KEYS.items()
            if st.session_state.get(ss_key) is not None}


def main() -> None:
    # Initialize all widget state from last saved state on first run
    if "ui_initialized" not in st.session_state:
        _apply_state_to_session(_get_ui_last_state())
        st.session_state.ui_initialized = True

    # Full-width, no sidebar noise
    st.set_page_config(
        page_title="Image Grid Viewer",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    # Edge-to-edge, hide header/footer, remove padding, no borders on images
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 0rem;
            padding-bottom: 0rem;
            padding-left: 0rem;
            padding-right: 0rem;
        }
        header, footer {
            visibility: hidden;
            height: 0;
        }
        .stImage img {
            border-radius: 0 !important;
            margin: 0 !important;
            padding: 0 !important;
        }
        /* Reduce gap between columns */
        [data-testid="stHorizontalBlock"] {
            gap: 0.25rem !important;
        }
        /* Tighter captions - minimal space */
        .stCaption, [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {
            margin: 0 !important;
            padding: 0 !important;
            font-size: 0.6rem !important;
            line-height: 1 !important;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 100%;
        }
        /* Kill all vertical spacing in grid area */
        [data-testid="stVerticalBlock"] > div,
        [data-testid="stVerticalBlockBorderWrapper"],
        [data-testid="stColumn"] > div {
            margin: 0 !important;
            padding: 0 !important;
            gap: 0 !important;
        }
        /* Tighter image containers */
        [data-testid="stImage"], [data-testid="stImage"] > div {
            margin: 0 !important;
            padding: 0 !important;
        }
        /* Row spacing */
        [data-testid="stHorizontalBlock"] {
            margin-bottom: 2px !important;
        }
        /* Image container for hover effect */
        .img-container {
            position: relative;
            display: inline-block;
            width: 100%;
            cursor: pointer;
            pointer-events: auto;
            touch-action: manipulation;
            user-select: none;
        }
        :root {
            --image-grid-brightness: 100%;
        }
        .img-container img {
            display: block;
            width: 100%;
            filter: brightness(var(--image-grid-brightness));
            transition: filter 0.15s ease, transform 0.2s ease;
            pointer-events: auto;
        }
        .copy-btn, .rotate-btn, .enlarge-btn {
            position: absolute;
            top: 8px;
            background: rgba(0, 0, 0, 0.5);
            color: white;
            border: none;
            border-radius: 4px;
            padding: 6px 10px;
            cursor: pointer;
            font-size: 16px;
            opacity: 0;
            transition: opacity 0.2s ease;
            z-index: 10;
        }
        .copy-btn {
            right: 8px;
        }
        .rotate-btn {
            right: 48px;
        }
        .enlarge-btn {
            right: 88px;
        }
        .quick-label-actions {
            position: absolute;
            top: 44px;
            left: auto;
            right: 8px;
            display: flex;
            flex-direction: column;
            gap: 4px;
            align-items: flex-end;
            z-index: 11;
            pointer-events: none;
            opacity: 0;
            transition: opacity 0.2s ease;
        }
        .quick-label-btn {
            border: 1px solid rgba(255, 255, 255, 0.38);
            background: rgba(0, 0, 0, 0.55);
            color: rgba(255, 255, 255, 0.93);
            border-radius: 999px;
            padding: 2px 7px;
            font-size: 10px;
            line-height: 1.1;
            white-space: nowrap;
            cursor: pointer;
            pointer-events: auto;
            text-align: right;
        }
        .quick-label-btn:hover {
            border-color: rgba(250, 204, 21, 0.72);
            color: #fef3c7;
            background: rgba(250, 204, 21, 0.2);
        }
        .auto-next-label-hint {
            position: absolute;
            left: 8px;
            bottom: 8px;
            z-index: 12;
            max-width: calc(100% - 16px);
            padding: 4px 8px;
            border-radius: 999px;
            border: 1px solid rgba(56, 189, 248, 0.7);
            background: linear-gradient(135deg, rgba(14, 165, 233, 0.78), rgba(15, 23, 42, 0.76));
            color: #f0f9ff;
            font-size: 10px;
            font-weight: 800;
            line-height: 1.1;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            box-shadow: 0 6px 18px rgba(14, 165, 233, 0.28);
            pointer-events: none;
            opacity: 0;
            transform: translateY(3px);
            transition: opacity 0.14s ease, transform 0.14s ease;
        }
        .img-container.auto-next-label-candidate:hover .auto-next-label-hint {
            opacity: 0.94;
            transform: translateY(0);
        }
        .img-container.auto-next-label-remove .auto-next-label-hint {
            border-color: rgba(251, 113, 133, 0.72);
            background: linear-gradient(135deg, rgba(225, 29, 72, 0.76), rgba(15, 23, 42, 0.72));
            box-shadow: 0 6px 18px rgba(225, 29, 72, 0.25);
        }
        .img-container.auto-batch-label-target {
            box-shadow: inset 0 0 0 3px rgba(14, 165, 233, 0.9), 0 0 0 2px rgba(14, 165, 233, 0.2), 0 10px 24px rgba(14, 165, 233, 0.22);
        }
        .img-container.auto-batch-label-target::before {
            content: "";
            position: absolute;
            inset: 0;
            z-index: 8;
            pointer-events: none;
            background: rgba(14, 165, 233, 0.13);
            mix-blend-mode: screen;
        }
        .selection-bar .missing-slots-btn.empty-active {
            border-color: rgba(56, 189, 248, 0.58);
            background: rgba(14, 165, 233, 0.18);
            color: #e0f2fe;
            box-shadow: none;
        }
          .selection-bar .missing-slots-btn.empty-none {
              border-color: rgba(148, 163, 184, 0.3);
              background: rgba(148, 163, 184, 0.08);
              color: rgba(226, 232, 240, 0.72);
          }
            .folder-chip-select {
                font-family: -apple-system, BlinkMacSystemFont, sans-serif;
                margin-top: 2px;
            }
            .folder-chip-select-label {
                display: block;
                margin: 0 0 4px 2px;
                color: rgba(255, 255, 255, 0.7);
                font-size: 0.875rem;
                font-weight: 600;
            }
            .folder-chip-select details {
                position: relative;
            }
          .folder-chip-select summary {
              display: grid;
              grid-template-columns: minmax(0, 1fr) auto;
              align-items: center;
              gap: 10px;
              min-height: 36px;
              padding: 7px 10px;
              border: 1px solid rgba(255, 255, 255, 0.14);
              border-radius: 14px;
              background: rgba(8, 13, 20, 0.82);
              color: rgba(255, 255, 255, 0.9);
              cursor: pointer;
              list-style: none;
              box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
          }
          .folder-chip-select summary::-webkit-details-marker {
              display: none;
          }
          .folder-chip-select summary::after {
              content: "⌄";
              color: rgba(255, 255, 255, 0.58);
              font-size: 13px;
              font-weight: 900;
          }
          .folder-chip-select details[open] summary {
              border-color: rgba(250, 204, 21, 0.42);
              border-radius: 14px 14px 6px 6px;
          }
          .folder-chip-select-panel {
              position: absolute;
              left: 0;
              right: 0;
              z-index: 1000;
              display: grid;
              gap: 5px;
              max-height: min(460px, 56vh);
              overflow: auto;
              padding: 8px;
              margin-top: 6px;
              border-radius: 14px;
              border: 1px solid rgba(255, 255, 255, 0.18);
              background: rgba(8, 13, 20, 0.98);
              box-shadow: 0 18px 48px rgba(0, 0, 0, 0.36);
          }
          .folder-chip-select .folder-status-item {
              display: grid;
              grid-template-columns: minmax(0, 1fr) auto;
              align-items: center;
              gap: 10px;
              padding: 7px 9px;
              border-radius: 11px;
              border: 1px solid rgba(255, 255, 255, 0.09);
              background: rgba(255, 255, 255, 0.045);
              color: rgba(255, 255, 255, 0.86);
              font-size: 12px;
              text-align: left;
              text-decoration: none;
          }
          .folder-chip-select .folder-status-item:hover,
          .folder-chip-select .folder-status-item.current {
              background: rgba(255, 255, 255, 0.105);
              border-color: rgba(250, 204, 21, 0.42);
          }
          .folder-chip-select .folder-status-item.current {
              box-shadow: inset 0 0 0 1px rgba(250, 204, 21, 0.18);
          }
          .folder-chip-select .folder-status-item-name,
          .folder-chip-select-current-name {
              min-width: 0;
              overflow: hidden;
              text-overflow: ellipsis;
              white-space: nowrap;
              font-weight: 650;
          }
          .folder-chip-select .folder-status-tags,
          .folder-chip-select-current-tags {
              display: inline-flex;
              align-items: center;
              justify-content: flex-end;
              gap: 6px;
              white-space: nowrap;
          }
          .folder-chip-select .folder-status-item-tag,
          .folder-chip-select .folder-status-video-tag,
          .folder-chip-select-current-tag,
          .folder-chip-select-current-video {
              padding: 2px 8px;
              border-radius: 999px;
              font-size: 10px;
              font-weight: 900;
              border: 1px solid rgba(255, 255, 255, 0.14);
          }
          .folder-chip-select .folder-status-fully-labeled .folder-status-item-tag,
          .folder-chip-select .folder-chip-select-current.folder-status-fully-labeled .folder-chip-select-current-tag {
              color: #bbf7d0;
              background: rgba(34, 197, 94, 0.18);
              border-color: rgba(134, 239, 172, 0.36);
          }
          .folder-chip-select .folder-status-partial .folder-status-item-tag,
          .folder-chip-select .folder-chip-select-current.folder-status-partial .folder-chip-select-current-tag {
              color: #fde68a;
              background: rgba(245, 158, 11, 0.18);
              border-color: rgba(251, 191, 36, 0.38);
          }
          .folder-chip-select .folder-status-unlabeled .folder-status-item-tag,
          .folder-chip-select .folder-chip-select-current.folder-status-unlabeled .folder-chip-select-current-tag {
              color: #d1d5db;
              background: rgba(148, 163, 184, 0.14);
              border-color: rgba(209, 213, 219, 0.24);
          }
          .folder-chip-select .folder-status-missing .folder-status-item-tag,
          .folder-chip-select .folder-status-missing-one .folder-status-item-tag,
          .folder-chip-select .folder-chip-select-current.folder-status-missing .folder-chip-select-current-tag,
          .folder-chip-select .folder-chip-select-current.folder-status-missing-one .folder-chip-select-current-tag {
              color: #fed7aa;
              background: rgba(249, 115, 22, 0.18);
              border-color: rgba(251, 146, 60, 0.42);
          }
          .folder-chip-select .folder-status-errors .folder-status-item-tag,
          .folder-chip-select .folder-chip-select-current.folder-status-errors .folder-chip-select-current-tag {
              color: #fecaca;
              background: rgba(239, 68, 68, 0.22);
              border-color: rgba(252, 165, 165, 0.48);
          }
          .folder-chip-select .folder-video-yes .folder-status-video-tag,
          .folder-chip-select .folder-chip-select-current.folder-video-yes .folder-chip-select-current-video {
              color: #bfdbfe;
              background: rgba(59, 130, 246, 0.18);
              border-color: rgba(147, 197, 253, 0.36);
          }
          .folder-chip-select .folder-video-no .folder-status-video-tag,
          .folder-chip-select .folder-chip-select-current.folder-video-no .folder-chip-select-current-video {
              color: #e5e7eb;
              background: rgba(148, 163, 184, 0.12);
              border-color: rgba(209, 213, 219, 0.22);
          }
          .quality-badge {
            position: absolute;
            left: 8px;
            top: 8px;
            background: rgba(0, 0, 0, 0.65);
            color: white;
            border-radius: 4px;
            padding: 4px 8px;
            font-size: 11px;
            line-height: 1.2;
            font-family: -apple-system, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            opacity: 0;
            transition: opacity 0.2s ease;
            z-index: 11;
            pointer-events: none;
            max-width: 95%;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        /* Lightbox overlay */
        .lightbox-overlay {
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            background: rgba(0, 0, 0, 0.7);
            pointer-events: none;
            z-index: 10000;
        }
        .lightbox-viewer {
            position: fixed;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 10001;
            pointer-events: auto;
        }
        .lightbox-image {
            max-width: 95vw;
            max-height: 95vh;
            object-fit: contain;
            filter: brightness(var(--image-grid-brightness));
            cursor: zoom-out;
            z-index: 10000;
        }
        .lightbox-close {
            position: fixed;
            top: 20px;
            right: 30px;
            color: white;
            font-size: 40px;
            cursor: pointer;
            opacity: 0.7;
            z-index: 10002;
            pointer-events: auto;
        }
        .lightbox-close:hover {
            opacity: 1;
        }
        /* Multi-select styles */
        .img-container.selected {
            outline: 3px solid var(--annotation-color, #4CAF50);
            outline-offset: -3px;
        }
        .img-container.selected::after {
            content: '✓';
            position: absolute;
            top: 8px;
            left: 8px;
            background: var(--annotation-color, #4CAF50);
            color: white;
            width: 24px;
            height: 24px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 14px;
            font-weight: bold;
            z-index: 11;
        }
        .img-container.label-inspection-highlight {
            outline: 3px solid rgba(250, 204, 21, 0.88);
            outline-offset: -3px;
            box-shadow: 0 0 0 2px rgba(250, 204, 21, 0.35);
            z-index: 1;
        }
        .img-container.image-jump-flash {
            animation: image-jump-flash 1s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .img-container.label-inspection-highlight::after {
            content: '✦';
            position: absolute;
            top: 8px;
            right: 8px;
            width: 22px;
            height: 22px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 12px;
            font-weight: 700;
            color: rgba(250, 204, 21, 0.95);
            background: rgba(24, 24, 24, 0.66);
            border: 1px solid rgba(250, 204, 21, 0.72);
            z-index: 12;
            pointer-events: none;
        }
        .img-container.copy-flash img {
            filter: brightness(calc(var(--image-grid-brightness) * 1.35));
            transform: scale(0.995);
        }
        @keyframes image-jump-flash {
            0% {
                outline: 3px solid rgba(250, 204, 21, 0.98);
                outline-offset: -3px;
                box-shadow: 0 0 0 0 rgba(250, 204, 21, 0.45);
            }
            45% {
                box-shadow: 0 0 0 14px rgba(250, 204, 21, 0.22);
            }
            100% {
                outline-color: rgba(250, 204, 21, 0.12);
                outline-offset: -3px;
                box-shadow: 0 0 0 0 rgba(250, 204, 21, 0);
            }
        }
        .img-container.labeled {
            outline: 3px solid var(--annotation-color);
            outline-offset: -3px;
            box-shadow:
                0 0 0 6px color-mix(in srgb, var(--annotation-group-color, var(--annotation-color)) 26%, transparent),
                0 10px 22px color-mix(in srgb, var(--annotation-group-color, var(--annotation-color)) 18%, transparent);
        }
        .img-container.labeled::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 6px;
            background: var(--annotation-group-color, var(--annotation-color));
            opacity: 0.88;
            z-index: 10;
            pointer-events: none;
        }
        .label-badges {
            position: absolute;
            left: 8px;
            right: 8px;
            bottom: 8px;
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
            pointer-events: auto;
            z-index: 11;
            align-items: center;
            justify-content: flex-start;
        }
        .img-container:hover {
            outline: 1px solid rgba(255, 255, 255, 0.22);
            outline-offset: -1px;
        }
        .label-chip {
            color: white;
            background: color-mix(in srgb, var(--chip-color, rgba(0, 0, 0, 0.6)) 30%, transparent 70%);
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 999px;
            font-size: 11px;
            line-height: 1.1;
            padding: 3px 8px;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            max-width: 100%;
            overflow: hidden;
            white-space: nowrap;
            text-overflow: ellipsis;
            backdrop-filter: blur(2px);
            pointer-events: auto;
        }
        .label-chip-x {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 14px;
            height: 14px;
            margin-left: 6px;
            border-radius: 50%;
            background: rgba(255, 255, 255, 0.22);
            color: white;
            font-size: 10px;
            line-height: 1;
            opacity: 0;
            cursor: pointer;
            pointer-events: auto;
        }
        .label-chip:hover .label-chip-x {
            opacity: 1;
        }
        .label-chip-more {
            opacity: 0.8;
        }
        .label-chip::before {
            content: '';
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
            margin-right: 4px;
            background: var(--annotation-color, #22c55e);
            opacity: 0.95;
        }
          .selection-bar {
              position: fixed;
              bottom: 0;
              left: 0;
              right: var(--folder-banner-width, 360px);
              background: rgba(0, 0, 0, 0.86);
              color: white;
              padding: 26px 16px 8px;
              border-radius: 18px 0 0 0;
              border: 1px solid rgba(255, 255, 255, 0.14);
              border-right: 0;
              border-bottom: 0;
              font-size: 14px;
              z-index: 9999;
              display: flex !important;
            align-items: center;
            gap: 8px;
              width: auto;
              flex-wrap: wrap;
              row-gap: 6px;
              column-gap: 8px;
              font-family: -apple-system, BlinkMacSystemFont, sans-serif;
              pointer-events: auto !important;
            z-index: 2147483647;
              visibility: hidden;
              box-sizing: border-box;
              transition: transform 0.18s ease, opacity 0.18s ease, box-shadow 0.18s ease;
          }
        .selection-bar.visible {
            visibility: visible;
        }
        .selection-bar .drag-btn {
            background: rgba(255, 255, 255, 0.15);
            color: white;
            border: 1px solid rgba(255, 255, 255, 0.3);
            padding: 4px 12px;
            border-radius: 12px;
            cursor: pointer;
            font-size: 12px;
            transition: background 0.2s, border-color 0.2s;
        }
        .selection-bar .drag-btn:hover {
            background: rgba(255, 255, 255, 0.3);
        }
        .selection-bar .drag-btn.clicked-1 {
            background: rgba(245, 158, 11, 0.45);
            border-color: rgba(245, 158, 11, 0.7);
        }
        .selection-bar .drag-btn.clicked-2 {
            background: rgba(34, 197, 94, 0.45);
            border-color: rgba(34, 197, 94, 0.7);
        }
        .selection-bar .copy-selected-btn {
            background: rgba(255, 255, 255, 0.15);
            color: white;
            border: 1px solid rgba(255, 255, 255, 0.3);
            padding: 4px 12px;
            border-radius: 12px;
            cursor: pointer;
            font-size: 12px;
        }
        .selection-bar .copy-selected-btn:hover {
            background: rgba(255, 255, 255, 0.3);
        }
        .selection-bar .clear-btn {
            background: transparent;
            color: rgba(255, 255, 255, 0.6);
            border: 1px solid rgba(255, 255, 255, 0.2);
            padding: 4px 8px;
            border-radius: 12px;
            cursor: pointer;
            font-size: 12px;
        }
        .selection-bar .clear-btn:hover {
            background: rgba(255,255,255,0.1);
        }
        .selection-bar button {
            pointer-events: auto;
            cursor: pointer;
        }
        .selection-bar .annotation-presets {
            display: flex;
            flex-direction: column;
            gap: 6px;
            align-items: stretch;
            max-width: min(76vw, 1200px);
            overflow: visible;
            padding-right: 12px;
        }
        .selection-bar .annotation-preset {
            border: 1px solid rgba(255, 255, 255, 0.35);
            background: rgba(255, 255, 255, 0.15);
            color: white;
            border-radius: 12px;
            padding: 3px 8px;
            font-size: 11px;
            cursor: pointer;
            line-height: 1.1;
            white-space: nowrap;
            position: relative;
        }
        .selection-bar .annotation-preset.used {
            background: rgba(34, 197, 94, var(--usage-alpha, 0.32));
            border-color: rgba(134, 239, 172, 0.85);
            font-weight: 650;
        }
        .selection-bar .annotation-preset.duplicate {
            background: rgba(239, 68, 68, 0.34);
            border-color: rgba(252, 165, 165, 0.92);
            box-shadow: inset 0 0 0 1px rgba(254, 202, 202, 0.48);
        }
        .selection-bar .annotation-preset-usage {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 16px;
            height: 16px;
            margin-left: 6px;
            opacity: 0.98;
            font-size: 9px;
            font-weight: 800;
            border: 1px solid rgba(134, 239, 172, 0.76);
            background: rgba(34, 197, 94, 0.38);
            color: #ecfdf5;
            border-radius: 999px;
            padding: 0 5px;
            line-height: 1;
            cursor: pointer;
            pointer-events: auto;
            box-shadow: 0 0 0 1px rgba(34, 197, 94, 0.16);
            transition: background 0.14s, border-color 0.14s, transform 0.14s;
        }
        .selection-bar .annotation-preset-usage:hover,
        .selection-bar .annotation-preset-usage:focus-visible {
            background: rgba(250, 204, 21, 0.32);
            border-color: rgba(250, 204, 21, 0.75);
            color: #fffbeb;
            transform: translateY(-1px);
        }
        .selection-bar .annotation-preset.used .annotation-preset-usage {
            background: rgba(34, 197, 94, 0.42);
            border-color: rgba(134, 239, 172, 0.8);
        }
        .selection-bar .annotation-preset.duplicate .annotation-preset-usage {
            background: rgba(239, 68, 68, 0.42);
            border-color: rgba(252, 165, 165, 0.82);
            color: #fee2e2;
        }
        .selection-bar .annotation-preset-overlap {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            height: 16px;
            margin-left: 6px;
            padding: 0 7px;
            border-radius: 999px;
            border: 1px solid rgba(252, 165, 165, 0.88);
            background: rgba(239, 68, 68, 0.38);
            color: #fee2e2;
            font-size: 9px;
            font-weight: 900;
            letter-spacing: 0.02em;
            line-height: 1;
            cursor: pointer;
            pointer-events: auto;
            box-shadow: 0 0 0 1px rgba(248, 113, 113, 0.2), 0 0 14px rgba(239, 68, 68, 0.24);
        }
        .selection-bar .annotation-preset-overlap:hover,
        .selection-bar .annotation-preset-overlap:focus-visible {
            border-color: rgba(253, 224, 71, 0.96);
            background: rgba(250, 204, 21, 0.3);
            color: #fef9c3;
            outline: none;
        }
        .selection-bar .annotation-preset:hover {
            background: rgba(255, 255, 255, 0.28);
            border-color: rgba(255, 255, 255, 0.55);
        }
        .selection-bar .annotation-preset.active {
            background: rgba(34, 197, 94, 0.45);
            border-color: rgba(134, 239, 172, 0.72);
            font-weight: 700;
        }
        .selection-bar .instant-off-row {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            margin-right: 4px;
            font-size: 11px;
            width: 100%;
        }
        .selection-bar .instant-off-title {
            color: rgba(255, 255, 255, 0.78);
            font-weight: 600;
            margin-right: 2px;
            white-space: nowrap;
        }
        .selection-bar .instant-off-options {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            flex-wrap: wrap;
        }
        .selection-bar .instant-off-btn {
            border: 1px solid rgba(255, 255, 255, 0.35);
            background: rgba(255, 255, 255, 0.15);
            color: white;
            border-radius: 12px;
            padding: 3px 8px;
            font-size: 11px;
            cursor: default;
            line-height: 1.1;
            white-space: nowrap;
        }
        .selection-bar .instant-off-btn.active {
            border-color: rgba(134, 239, 172, 0.72);
            color: #dcfce7;
            font-weight: 700;
        }
        .selection-bar .instant-off-btn.yes-video {
            background: rgba(34, 197, 94, 0.18);
            border-color: rgba(134, 239, 172, 0.5);
        }
        .selection-bar .instant-off-btn.no-video {
            background: rgba(239, 68, 68, 0.18);
            border-color: rgba(252, 165, 165, 0.52);
        }
        .selection-bar .annotation-group {
            display: inline-flex;
            align-items: center;
            gap: 5px;
            flex-wrap: wrap;
            opacity: 0.95;
            padding: 2px 4px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.14);
            border-radius: 6px;
            width: 100%;
            box-sizing: border-box;
        }
        .selection-bar .annotation-group-title {
            color: rgba(255, 255, 255, 0.77);
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.03em;
            text-transform: uppercase;
            margin-right: 4px;
            white-space: nowrap;
        }
        .selection-bar .annotation-group.pending {
            background: rgba(250, 204, 21, 0.16);
            border-color: rgba(250, 204, 21, 0.45);
        }
        .selection-bar .anode-count-row {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            margin-left: 2px;
        }
        .selection-bar .table-anode-row {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            margin-right: 6px;
            margin-bottom: 2px;
            width: 100%;
            flex-wrap: wrap;
        }
        .selection-bar .table-anode-title {
            color: rgba(255, 255, 255, 0.78);
            font-weight: 600;
            font-size: 10px;
            white-space: nowrap;
        }
        .selection-bar .table-anode-options {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            flex-wrap: wrap;
        }
        .selection-bar .anode-count-btn {
            border: 1px solid rgba(255, 255, 255, 0.35);
            background: rgba(255, 255, 255, 0.12);
            color: white;
            border-radius: 999px;
            padding: 2px 7px;
            font-size: 10px;
            line-height: 1.05;
            cursor: pointer;
        }
        .selection-bar .anode-count-btn:hover {
            background: rgba(255, 255, 255, 0.24);
            border-color: rgba(255, 255, 255, 0.58);
        }
        .selection-bar .anode-count-btn.active {
            background: rgba(34, 197, 94, 0.45);
            border-color: rgba(134, 239, 172, 0.72);
            color: #ecfdf5;
            font-weight: 700;
        }
        .selection-bar .annotation-mixed-note {
            color: #fecaca;
            font-size: 11px;
            border: 1px dashed rgba(254, 202, 202, 0.85);
            border-radius: 999px;
            padding: 2px 8px;
            white-space: nowrap;
            letter-spacing: 0.01em;
        }
        .selection-bar .annotation-mixed-note {
            cursor: pointer;
            pointer-events: auto;
        }
        .selection-bar .annotation-mixed-note:hover,
        .selection-bar .annotation-mixed-note:focus-visible {
            border-color: rgba(253, 224, 71, 0.84);
            background: rgba(250, 204, 21, 0.18);
            color: #fef3c7;
            outline: none;
        }
        .selection-bar .global-overlap-btn,
        .selection-bar .missing-slots-btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 5px;
            border-radius: 999px;
            padding: 4px 11px;
            border: 1px solid rgba(255, 255, 255, 0.22);
            background: rgba(255, 255, 255, 0.08);
            color: rgba(255, 255, 255, 0.76);
            font-size: 11px;
            font-weight: 900;
            letter-spacing: 0.01em;
            line-height: 1;
            white-space: nowrap;
            cursor: pointer;
            pointer-events: auto;
            transition: background 0.14s, border-color 0.14s, color 0.14s, transform 0.14s, box-shadow 0.14s;
        }
        .selection-bar .global-overlap-btn.inactive,
        .selection-bar .missing-slots-btn.complete {
            border-color: rgba(134, 239, 172, 0.36);
            background: rgba(34, 197, 94, 0.12);
            color: rgba(187, 247, 208, 0.82);
        }
        .selection-bar .global-overlap-btn.active,
        .selection-bar .missing-slots-btn.active {
            border-color: rgba(252, 165, 165, 0.88);
            background: rgba(239, 68, 68, 0.34);
            color: #fee2e2;
            box-shadow: 0 0 0 1px rgba(248, 113, 113, 0.18), 0 0 18px rgba(239, 68, 68, 0.22);
        }
        .selection-bar .global-overlap-btn:hover,
        .selection-bar .global-overlap-btn:focus-visible,
        .selection-bar .missing-slots-btn:hover,
        .selection-bar .missing-slots-btn:focus-visible {
            transform: translateY(-1px);
            outline: none;
        }
        .selection-bar .global-overlap-btn.active:hover,
        .selection-bar .global-overlap-btn.active:focus-visible,
        .selection-bar .missing-slots-btn.active:hover,
        .selection-bar .missing-slots-btn.active:focus-visible {
            border-color: rgba(253, 224, 71, 0.92);
            background: rgba(250, 204, 21, 0.28);
            color: #fef9c3;
        }
        .selection-bar .global-overlap-btn.inactive:hover,
        .selection-bar .global-overlap-btn.inactive:focus-visible,
        .selection-bar .missing-slots-btn.complete:hover,
        .selection-bar .missing-slots-btn.complete:focus-visible {
            border-color: rgba(134, 239, 172, 0.58);
            background: rgba(34, 197, 94, 0.18);
            color: #dcfce7;
        }
        .selection-bar .selection-inspect-btn {
            background: rgba(250, 204, 21, 0.18);
            color: #fef3c7;
            border: 1px solid rgba(250, 204, 21, 0.52);
            padding: 4px 10px;
            border-radius: 12px;
            cursor: pointer;
            font-size: 12px;
            line-height: 1.1;
        }
        .selection-bar .selection-inspect-btn:hover {
            background: rgba(250, 204, 21, 0.28);
        }
        .selection-bar .annotation-input {
            min-width: 220px;
            border: 1px solid rgba(255, 255, 255, 0.3);
            background: rgba(0, 0, 0, 0.5);
            color: white;
            border-radius: 12px;
            padding: 4px 10px;
            font-size: 12px;
        }
        .selection-bar .annotation-input:focus {
            outline: none;
            border-color: rgba(255, 255, 255, 0.6);
        }

        .selection-bar .selection-bar-fold-toggle {
            position: absolute;
            right: 12px;
            top: 5px;
            width: 22px;
            height: 18px;
            padding: 0;
            border: 0;
            border-radius: 999px;
            background: transparent;
            color: rgba(255, 255, 255, 0.34);
            font-size: 13px;
            font-weight: 900;
            line-height: 1;
            cursor: pointer;
            pointer-events: auto;
            opacity: 0.46;
            transition: opacity 0.16s ease 0.22s, background 0.16s ease 0.22s, color 0.16s ease 0.22s;
        }
        .selection-bar .selection-bar-fold-toggle:hover {
            background: rgba(255, 255, 255, 0.07);
            color: rgba(255, 255, 255, 0.78);
            opacity: 1;
        }
        body.selection-bar-folded .selection-bar {
            transform: translateY(calc(100% - 18px));
            opacity: 0.86;
            cursor: pointer;
            background: rgba(0, 0, 0, 0.85);
            border-radius: 10px 0 0 0;
            box-shadow: none;
        }
        body.selection-bar-folded .selection-bar > :not(.selection-bar-fold-toggle) {
            opacity: 0;
            pointer-events: none;
            visibility: hidden;
        }
        body.selection-bar-folded .selection-bar .selection-bar-fold-toggle {
            top: 0;
            right: 10px;
            height: 18px;
            color: rgba(255, 255, 255, 0.48);
            background: transparent;
            opacity: 0.74;
        }
        .selection-debug-panel {
            position: absolute;
            right: 12px;
            bottom: 12px;
            width: min(480px, 42vw);
            max-width: min(480px, 42vw);
            min-width: 300px;
            max-height: calc(100% - 24px);
            overflow: auto;
            background: rgba(10, 10, 16, 0.95);
            color: #f3f4f6;
            border: 1px solid rgba(255, 255, 255, 0.3);
            border-radius: 12px;
            z-index: 3;
            padding: 8px 10px;
            box-shadow: 0 10px 28px rgba(0, 0, 0, 0.38);
            backdrop-filter: blur(2px);
            display: none;
            box-sizing: border-box;
            flex: none;
            order: 20;
            margin: 0;
        }
        .selection-bar.has-inspection-panel {
            padding-right: min(520px, 46vw);
        }
        .selection-debug-panel.visible {
            display: block;
        }
        .selection-debug-panel-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 8px;
            font-size: 11px;
            font-weight: 700;
            margin-bottom: 6px;
            letter-spacing: 0.02em;
            color: rgba(255, 255, 255, 0.9);
        }
        .selection-debug-panel-close {
            border: 1px solid rgba(255, 255, 255, 0.4);
            background: rgba(255, 255, 255, 0.1);
            color: white;
            border-radius: 999px;
            padding: 2px 8px;
            cursor: pointer;
            font-size: 11px;
            line-height: 1.2;
        }
        .selection-debug-panel-close:hover {
            background: rgba(255, 255, 255, 0.2);
        }
        .selection-debug-panel-body {
            display: flex;
            flex-direction: column;
            gap: 5px;
            font-size: 11px;
        }
        .selection-debug-item {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 4px 6px;
            border: 1px solid rgba(255, 255, 255, 0.18);
            border-radius: 8px;
            background: rgba(255, 255, 255, 0.04);
            line-height: 1.2;
            cursor: pointer;
            user-select: none;
        }
        .selection-debug-item:hover {
            background: rgba(255, 255, 255, 0.1);
        }
        .selection-debug-index {
            font-weight: 700;
            color: rgba(255, 255, 255, 0.82);
            min-width: 28px;
        }
        .selection-debug-filename {
            flex: 1;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .selection-debug-labels {
            color: rgba(226, 232, 240, 0.72);
            max-width: 240px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .selection-debug-jump {
            border: 1px solid rgba(34, 197, 94, 0.6);
            background: rgba(34, 197, 94, 0.2);
            color: #dcfce7;
            border-radius: 999px;
            padding: 2px 7px;
            cursor: pointer;
            font-size: 10px;
            line-height: 1.2;
            white-space: nowrap;
        }
        .selection-debug-jump:hover {
            background: rgba(34, 197, 94, 0.34);
        }
        .selection-debug-empty {
            color: rgba(255, 255, 255, 0.68);
            font-size: 11px;
            padding: 6px 4px;
        }
        .selection-debug-item.missing-slot-item {
            cursor: default;
            align-items: flex-start;
        }
        .selection-debug-item.missing-slot-item:hover {
            background: rgba(255, 255, 255, 0.06);
        }
        .missing-slot-main {
            display: flex;
            flex-direction: column;
            gap: 3px;
            flex: 1;
            min-width: 0;
        }
        .missing-slot-label {
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            color: rgba(255, 255, 255, 0.9);
        }
        .missing-slot-state {
            width: fit-content;
            border-radius: 999px;
            padding: 1px 7px;
            font-size: 10px;
            font-weight: 700;
        }
        .missing-slot-state.empty {
            color: #dcfce7;
            background: rgba(34, 197, 94, 0.22);
            border: 1px solid rgba(34, 197, 94, 0.45);
        }
        .missing-slot-state.missing {
            color: #fef3c7;
            background: rgba(245, 158, 11, 0.18);
            border: 1px solid rgba(245, 158, 11, 0.45);
        }
        .missing-slot-toggle {
            border: 1px solid rgba(148, 163, 184, 0.55);
            background: rgba(15, 23, 42, 0.6);
            color: #e5e7eb;
            border-radius: 999px;
            padding: 3px 8px;
            cursor: pointer;
            font-size: 10px;
            line-height: 1.2;
            white-space: nowrap;
        }
        .missing-slot-toggle:hover {
            background: rgba(148, 163, 184, 0.2);
        }
        .verification-overlay {
            position: fixed;
            right: 20px;
            top: 60px;
            z-index: 2147483646;
            max-height: min(78vh, 720px);
            width: min(92vw, 520px);
            overflow: auto;
            display: none;
            background: rgba(12, 12, 18, 0.92);
            color: #f3f4f6;
            border: 1px solid rgba(255, 255, 255, 0.25);
            border-radius: 12px;
            box-shadow: 0 10px 32px rgba(0, 0, 0, 0.42);
            backdrop-filter: blur(2px);
            padding: 8px;
        }
        .verification-overlay.visible {
            display: block;
        }
        .verification-overlay-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 8px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.03em;
            color: rgba(255, 255, 255, 0.9);
            margin-bottom: 6px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.18);
            padding-bottom: 6px;
        }
        .verification-overlay-toggle {
            border: 1px solid rgba(255, 255, 255, 0.4);
            background: rgba(255, 255, 255, 0.1);
            color: white;
            border-radius: 999px;
            padding: 3px 8px;
            cursor: pointer;
            font-size: 11px;
            line-height: 1.2;
        }
        .verification-overlay-toggle:hover {
            background: rgba(255, 255, 255, 0.22);
        }
        .verification-overlay-body {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        .verification-table-section {
            border-top: 1px dashed rgba(255, 255, 255, 0.18);
            padding-top: 6px;
        }
        .verification-table-section:first-child {
            border-top: none;
            padding-top: 0;
        }
        .verification-table-title {
            font-size: 11px;
            margin-bottom: 4px;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            color: rgba(255, 255, 255, 0.82);
        }
        .verification-table {
            width: 100%;
            min-width: 240px;
            border-collapse: collapse;
            table-layout: fixed;
            font-size: 11px;
        }
        .verification-table th,
        .verification-table td {
            border: 1px solid rgba(255, 255, 255, 0.24);
            padding: 2px 4px;
            text-align: center;
            line-height: 1.2;
            vertical-align: middle;
            white-space: nowrap;
        }
        .verification-table th {
            background: rgba(255, 255, 255, 0.16);
            font-weight: 700;
            font-size: 10px;
        }
        .verification-row-label {
            text-align: left;
            text-wrap: nowrap;
        }
        .verification-dot {
            width: 16px;
            height: 16px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border-radius: 999px;
            font-size: 10px;
            line-height: 1;
            font-weight: 700;
        }
        .verification-dot.filled {
            color: #f8fafc;
            background: rgba(22, 163, 74, 0.9);
        }
        .verification-dot.empty {
            color: rgba(255, 255, 255, 0.42);
            background: rgba(255, 255, 255, 0.12);
        }
        .verification-empty {
            color: rgba(255, 255, 255, 0.75);
            font-size: 11px;
            padding: 4px 0;
        }
        .img-container:hover .copy-btn,
        .img-container:hover .rotate-btn,
        .img-container:hover .enlarge-btn,
        .img-container:hover .quick-label-actions,
        .img-container:hover .quality-badge {
            opacity: 0.7;
        }
        .copy-btn:hover, .rotate-btn:hover, .enlarge-btn:hover {
            opacity: 1 !important;
            background: rgba(0, 0, 0, 0.8);
        }
        /* Hide default streamlit button styling for copy buttons */
        .copy-btn-wrapper button {
            background: rgba(0, 0, 0, 0.5) !important;
            color: white !important;
            border: none !important;
            border-radius: 4px !important;
            padding: 4px 8px !important;
            min-height: 0 !important;
            height: auto !important;
            line-height: 1 !important;
            font-size: 14px !important;
        }
        .copy-btn-wrapper {
            position: absolute;
            top: 4px;
            right: 4px;
            z-index: 100;
            opacity: 0;
            transition: opacity 0.2s ease;
        }
        [data-testid="stColumn"]:hover .copy-btn-wrapper {
            opacity: 0.7;
        }
        .copy-btn-wrapper:hover {
            opacity: 1 !important;
        }
        .brightness-control {
            position: fixed;
            top: 10px;
            right: 10px;
            z-index: 99999;
            color: white;
            display: inline-flex;
            align-items: center;
            flex-direction: column;
            font-size: 12px;
            font-family: "Trebuchet MS", "Segoe UI", sans-serif;
            transition: opacity 0.2s ease;
            opacity: 0.55;
            pointer-events: auto;
        }
        .brightness-control:hover {
            opacity: 1;
        }
        .brightness-control .brightness-toggle {
            width: 28px;
            height: 28px;
            border-radius: 14px;
            border: 1px solid rgba(255, 255, 255, 0.45);
            background: rgba(0, 0, 0, 0.3);
            color: #fff;
            cursor: pointer;
            font-size: 14px;
            line-height: 1;
            display: inline-flex;
            align-items: center;
            justify-content: center;
        }
        .brightness-control .brightness-toggle:hover {
            background: rgba(0, 0, 0, 0.55);
        }
        .brightness-control .brightness-panel {
            margin-top: 4px;
            width: 0;
            max-height: 0;
            overflow: hidden;
            opacity: 0;
            transition: all 0.2s ease;
            display: flex;
            align-items: center;
            flex-direction: column;
            gap: 4px;
        }
        .brightness-control:hover .brightness-panel {
            width: auto;
            max-height: 120px;
            opacity: 1;
        }
        .brightness-control .brightness-slider {
            width: 190px;
            height: 22px;
            accent-color: #fff;
        }
        .brightness-control .brightness-value {
            color: rgba(255, 255, 255, 0.85);
            text-shadow: 0 1px 2px rgba(0, 0, 0, 0.4);
            font-variant-numeric: tabular-nums;
            font-size: 11px;
            white-space: nowrap;
        }
        .brightness-control .brightness-action {
            width: 120px;
            border: 1px solid rgba(255, 255, 255, 0.35);
            background: rgba(0, 0, 0, 0.45);
            color: #fff;
            border-radius: 12px;
            padding: 4px 8px;
            cursor: pointer;
            font-size: 11px;
        }
        .brightness-control .brightness-action:hover {
            background: rgba(0, 0, 0, 0.65);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.title("Image Grid Viewer")

    # === Boot checklist (shows on first load, auto-collapses) ===
    if "boot_complete" not in st.session_state:
        boot_status = st.status("Starting up...", expanded=True)
        with boot_status:
            # 1. UI state
            state = _get_ui_last_state()
            st.write(":white_check_mark: UI state restored" if state else ":white_check_mark: Using defaults")

            # 2. Clipboard/API server
            _server_ready.wait(timeout=2)
            server_ok = _check_server_health()
            port_detail = f"{CLIPBOARD_PORT}"
            if _CLIPBOARD_PORT_REASON == "fallback":
                port_detail = f"{CLIPBOARD_PORT} (fallback for {_CLIPBOARD_PORT_REQUESTED}, attempted {_CLIPBOARD_PORT_SCAN_LIST})"
            elif _CLIPBOARD_PORT_REASON == "scan_exhausted":
                port_detail = f"{CLIPBOARD_PORT} (scan exhausted; attempted {_CLIPBOARD_PORT_SCAN_LIST})"
            if server_ok:
                st.write(f":white_check_mark: API server on port {port_detail}")
            elif _server_error:
                st.write(f":x: API server failed: {_server_error}")
            else:
                st.write(f":x: API server not responding on port {port_detail}")

            # 3. Folder memory
            mem = load_folder_memory()
            st.write(f":white_check_mark: Folder memory ({len(mem)} entries)")

            # 4. Parent dir
            parent = Path(state.get("parent_dir", "")).expanduser()
            if parent.is_dir():
                st.write(f":white_check_mark: Parent dir exists")
            else:
                st.write(f":warning: Parent dir not found")

        boot_status.update(label="Boot complete", state="complete", expanded=False)
        st.session_state.boot_complete = True

    # === Parent directory selector ===
    default_parent = get_default_parent()
    col_parent, col_set_default, col_reset_default = st.columns([6, 1, 1])
    with col_parent:
        parent_dir_str = st.text_input("Parent directory (contains folders to browse)", default_parent, key="ui_parent_dir")
    with col_set_default:
        st.write("")  # Spacer to align button
        if st.button("Set as default", key="set-default-parent"):
            st.session_state._save_defaults = True
    with col_reset_default:
        st.write("")
        if st.button("Reset to default", key="reset-to-default"):
            _apply_state_to_session(_get_ui_defaults())
            st.rerun()
    parent_dir = Path(strip_quotes(parent_dir_str)).expanduser()

    # Log parent directory changes
    parent_dir_str_normalized = str(parent_dir)
    if "last_parent_dir" not in st.session_state:
        st.session_state.last_parent_dir = parent_dir_str_normalized
        log_interaction("app_start", {"parent_dir": parent_dir_str_normalized})
    elif st.session_state.last_parent_dir != parent_dir_str_normalized:
        log_interaction("parent_dir_change", {"from": st.session_state.last_parent_dir, "to": parent_dir_str_normalized})
        st.session_state.last_parent_dir = parent_dir_str_normalized

    folder_sort = st.radio("Sort folders by", ("name", "modified"), horizontal=True, key="folder-sort")
    all_subdirs = list_directories(parent_dir, folder_sort)

    # ==========================================================================
    # FOLDER FILTERS - Single place to define and apply all folder filters
    # ==========================================================================
    # Add new filter sets here. Each filter is: (folder_path_pattern, set_of_structure_numbers)
    FOLDER_FILTERS = {}
    # Add project-specific filter sets locally if needed.
    # Example:
    # FOLDER_FILTERS[str(Path(os.environ["REPORT_LABELER_LOCAL_PHOTO_ROOT"]).expanduser())] = {
    #     "label": "Show only priority structures",
    #     "structures": {"123", "456"},
    # }

    def apply_folder_filter(folders: List[Path], structure_set: set) -> List[Path]:
        """Filter folders to only include those with structure numbers in the set."""
        def matches(folder_path: Path) -> bool:
            try:
                match = re.search(r'#(\d+)', folder_path.name)
                return match is not None and match.group(1) in structure_set
            except Exception:
                return False
        return [f for f in folders if matches(f)]

    # Check if current folder has a filter available
    parent_dir_normalized = str(parent_dir).rstrip("/")
    filter_config = FOLDER_FILTERS.get(parent_dir_normalized)
    filter_active = False

    if filter_config and all_subdirs:
        filter_active = st.checkbox(filter_config["label"], value=False, key="priority-filter")

    # Apply filter if active - this is THE subdirs list used everywhere
    if filter_active and filter_config:
        subdirs = apply_folder_filter(all_subdirs, filter_config["structures"])
        # Reset index when filter changes
        if "last_filter_state" not in st.session_state:
            st.session_state.last_filter_state = False
        if st.session_state.last_filter_state != filter_active:
            log_interaction("filter_enabled", {"filter": filter_config["label"], "parent_dir": parent_dir_normalized})
            st.session_state.folder_index = 0
            st.session_state.last_filter_state = filter_active
    else:
        subdirs = all_subdirs
        if "last_filter_state" in st.session_state and st.session_state.last_filter_state:
            log_interaction("filter_disabled", {"parent_dir": parent_dir_normalized})
            st.session_state.folder_index = 0
            st.session_state.last_filter_state = False
    # ==========================================================================

    if subdirs:
        # Initialize session state for folder index - load from memory on first run
        if "folder_index" not in st.session_state:
            st.session_state.folder_index = get_last_folder_index(str(parent_dir), subdirs)

        folder_index_param = st.query_params.get("folder_index")
        if folder_index_param is not None:
            requested_folder_index = None
            try:
                raw_folder_index_param = folder_index_param[0] if isinstance(folder_index_param, list) else folder_index_param
                requested_folder_index = int(str(raw_folder_index_param))
            except (TypeError, ValueError, IndexError):
                requested_folder_index = None

            if requested_folder_index is not None and 0 <= requested_folder_index < len(subdirs):
                st.session_state.folder_index = requested_folder_index
                log_interaction(
                    "folder_status_list_nav",
                    {"folder_index": requested_folder_index, "folder": subdirs[requested_folder_index].name},
                )

            for query_key in ("folder_index", "folder_nav_token"):
                try:
                    del st.query_params[query_key]
                except Exception:
                    pass

        # Clamp to valid range
        st.session_state.folder_index = max(0, min(st.session_state.folder_index, len(subdirs) - 1))
        folder_status_annotations = _load_annotation_labels_by_path()
        folder_status_by_path = {str(d): get_folder_label_status(d, folder_status_annotations) for d in subdirs}
        folder_status_list_html = "".join(
            render_folder_status_nav_item(
                d,
                i,
                folder_status_by_path.get(str(d)),
                is_current=(i == st.session_state.folder_index),
            )
            for i, d in enumerate(subdirs)
        )

        # Fuzzy search for folders
        search_query = st.text_input("Search folders", key="folder_search", placeholder="Type to filter folders...")

        if search_query:
            query_lower = search_query.lower()
            query_parts = query_lower.split()

            def fuzzy_score(name: str) -> float:
                """Score a folder name against the search query. Higher = better match. 0 = no match."""
                name_lower = name.lower()
                # Exact substring match = best
                if query_lower in name_lower:
                    return 100.0
                # All parts present (AND matching)
                if all(part in name_lower for part in query_parts):
                    return 80.0
                # Fuzzy: check if query chars appear in order
                qi = 0
                for ch in name_lower:
                    if qi < len(query_lower) and ch == query_lower[qi]:
                        qi += 1
                if qi == len(query_lower):
                    return 50.0
                # Partial: any part matches
                if any(part in name_lower for part in query_parts):
                    return 30.0
                return 0.0

            scored = [(d, fuzzy_score(d.name)) for d in subdirs]
            filtered = sorted([(d, s) for d, s in scored if s > 0], key=lambda x: -x[1])
            display_dirs = [d for d, _ in filtered]
        else:
            display_dirs = subdirs

        if display_dirs:
            col_prev, col_dropdown, col_next, col_finder = st.columns([1, 6, 1, 1])

            # When searching, reset index to show first match
            if search_query:
                display_index = 0
            else:
                # Map current folder_index from full subdirs to display_dirs
                current_folder = subdirs[st.session_state.folder_index] if st.session_state.folder_index < len(subdirs) else subdirs[0]
                display_index = display_dirs.index(current_folder) if current_folder in display_dirs else 0

            with col_prev:
                if st.button("< Prev", disabled=display_index == 0, key="folder_prev"):
                    if search_query:
                        # Navigate within filtered results
                        target = display_dirs[max(0, display_index - 1)]
                        st.session_state.folder_index = subdirs.index(target)
                    else:
                        st.session_state.folder_index -= 1
                    st.rerun()
            with col_next:
                if st.button("Next >", disabled=display_index >= len(display_dirs) - 1, key="folder_next"):
                    if search_query:
                        target = display_dirs[min(len(display_dirs) - 1, display_index + 1)]
                        st.session_state.folder_index = subdirs.index(target)
                    else:
                        st.session_state.folder_index += 1
                    st.rerun()
            with col_dropdown:
                selected_subdir = display_dirs[display_index]
                new_idx = subdirs.index(selected_subdir)
                if new_idx != st.session_state.folder_index:
                    st.session_state.folder_index = new_idx
                selected_status = folder_status_by_path.get(str(selected_subdir))
                selector_items_html = "".join(
                    render_folder_status_nav_item(
                        d,
                        subdirs.index(d),
                        folder_status_by_path.get(str(d)),
                        is_current=(d == selected_subdir),
                    )
                    for d in display_dirs
                )
                current_classes = " ".join([
                    "folder-chip-select-current",
                    folder_status_css_class(selected_status),
                    folder_video_css_class(selected_status),
                ])
                st.markdown(
                    f'''
                    <div class="folder-chip-select">
                        <label class="folder-chip-select-label">Select a folder</label>
                        <details>
                            <summary class="{html.escape(current_classes)}">
                                <span class="folder-chip-select-current-name">{html.escape(selected_subdir.name)}</span>
                                <span class="folder-chip-select-current-tags">
                                    <span class="folder-chip-select-current-tag">{html.escape(format_folder_status_tag(selected_status))}</span>
                                    <span class="folder-chip-select-current-video">{html.escape(format_folder_video_tag(selected_status))}</span>
                                </span>
                            </summary>
                            <div class="folder-chip-select-panel">
                                {selector_items_html}
                            </div>
                        </details>
                    </div>
                    ''',
                    unsafe_allow_html=True,
                )
            with col_finder:
                st.write("")  # Spacer to align button
                if st.button("Show in Finder"):
                    reveal_in_finder(selected_subdir)
            folder = selected_subdir
        else:
            st.warning(f"No folders match '{search_query}'")
            folder = subdirs[st.session_state.folder_index]

        # Remember the current folder for next session
        remember_folder(str(parent_dir), folder.name)

        # Sticky folder name banner (always visible) - inline format
        folder_name = folder.name
        idx = st.session_state.folder_index
        is_first = idx == 0
        is_last = idx >= len(subdirs) - 1

        # Extract first number and #number from folder name
        def extract_nums(name: str) -> str:
            first = re.match(r'^(\d+)', name)
            hash_m = re.search(r'#(\d+)', name)
            first_num = first.group(1) if first else "?"
            hash_num = hash_m.group(1) if hash_m else "?"
            return f"{first_num}/#{hash_num}"

        current_info = extract_nums(folder_name)
        prev_info = extract_nums(subdirs[idx - 1].name) if not is_first else ""
        next_info = extract_nums(subdirs[idx + 1].name) if not is_last else ""

        # Extract #numbers for highlighted badges
        hash_match = re.search(r'#(\d+)', folder_name)
        hash_num = f"#{hash_match.group(1)}" if hash_match else ""

        prev_hash_match = re.search(r'#(\d+)', subdirs[idx - 1].name) if not is_first else None
        prev_hash_num = f"#{prev_hash_match.group(1)}" if prev_hash_match else ""

        # Extract letter grades (e.g., "B++, C++, D+") from inside parentheses after date
        # Pattern: (date - GRADES) or just GRADES at end of parentheses
        # Note: handles trailing spaces before closing paren like "C++ )"
        grades = ""
        note = ""
        try:
            grades_match = re.search(r'\([^)]*\d{4}\s*-\s*([A-D][+\-]*(?:\s*,\s*[A-D][+\-]*)*)\s*\)', folder_name)
            if not grades_match:
                # Try alternate pattern: grades at end of parentheses without date
                grades_match = re.search(r'-\s*([A-D][+\-]*(?:\s*,\s*[A-D][+\-]*)*)\s*\)', folder_name)
            if grades_match:
                grades = grades_match.group(1).strip()
        except (re.error, AttributeError, IndexError):
            grades = ""

        # Extract trailing note after the closing parenthesis (e.g., "- needs CP asap")
        try:
            note_match = re.search(r'\)\s*-\s*(.+)$', folder_name)
            if note_match:
                note = note_match.group(1).strip()
        except (re.error, AttributeError, IndexError):
            note = ""

        # Build highlighted folder name (escape HTML first, then add highlight spans)
        highlighted_name = html.escape(folder_name)
        try:
            if grades:
                escaped_grades = html.escape(grades)
                if escaped_grades in highlighted_name:
                    highlighted_name = highlighted_name.replace(
                        escaped_grades,
                        f'<span class="grades">{escaped_grades}</span>',
                        1
                    )
            if note:
                escaped_note = html.escape(note)
                if escaped_note in highlighted_name:
                    # Highlight the note portion at the end (including the dash)
                    highlighted_name = re.sub(
                        r'\)\s*-\s*' + re.escape(escaped_note) + r'$',
                        f') - <span class="note">{escaped_note}</span>',
                        highlighted_name
                    )
        except (re.error, ValueError, TypeError):
            # Fall back to unhighlighted name on any error
            highlighted_name = html.escape(folder_name)

        # Keyboard shortcut handler and banner - inject into parent frame
        # Get toggle state for Shift+Arrow folder navigation
        shift_arrow_folder_nav_js = "true" if st.session_state.get("shift_arrow_folder_nav", False) else "false"

        # Escape for JavaScript string
        banner_content = f'''
            <span class="banner-toggle" id="banner-toggle">&#9660;</span>
            <div class="banner-content">
                {f'<span class="hash-highlight-dull">{prev_hash_num}</span>' if prev_hash_num else ''}
                <span class="prev-info">{prev_info}</span>
                <span class="nav-btn {"disabled" if is_first else ""}" id="banner-prev">&lt;</span>
                  <button type="button" class="current-info folder-status-trigger" id="folder-status-trigger" title="Show folder selector">{current_info}</button>
                  <div class="folder-status-popover" id="folder-status-popover" aria-label="Folder selector">
                      <div class="folder-status-popover-title">Folder selector</div>
                      <div class="folder-status-popover-list">{folder_status_list_html}</div>
                  </div>
                <span class="nav-btn {"disabled" if is_last else ""}" id="banner-next">&gt;</span>
                <span class="next-info">{next_info}</span>
                {f'<span class="hash-highlight">{hash_num}</span>' if hash_num else ''}
                <span class="folder-name">{highlighted_name}</span>
            </div>
        '''
        # Escape special chars for JS
        banner_content_js = banner_content.replace('\\', '\\\\').replace('`', '\\`').replace('$', '\\$')

        components.html(
            f"""
            <script>
            (function() {{
                try {{
                    const doc = window.parent.document;
                    const restoreScrollKey = '__imageGridScrollY';
                    const topWindow = window.parent;
                    const topDoc = topWindow.document || doc;

                    // Restore scroll position after rerun from folder-nav clicks to avoid jump.
                    (function restoreScrollFromFolderNav() {{
                        try {{
                            const saved = topWindow[restoreScrollKey];
                            if (typeof saved === 'number' && Number.isFinite(saved)) {{
                                topWindow.scrollTo(0, saved);
                                topWindow[restoreScrollKey] = null;
                            }}
                        }} catch (restoreErr) {{
                            // best-effort restore; keep page usable even if unavailable
                        }}
                    }})();

                    // Inject or update banner
                    const bannerStyles = `
                          .sticky-folder-banner {{
                              position: fixed;
                              bottom: 0;
                              right: 0;
                              background: rgba(0, 0, 0, 0.86);
                              color: white;
                              padding: 6px 12px 7px;
                              border-radius: 0;
                              border: 1px solid rgba(255, 255, 255, 0.14);
                              border-left: 0;
                              border-bottom: 0;
                              font-size: 0.75rem;
                              z-index: 2147483647;
                              white-space: nowrap;
                              display: flex;
                              align-items: center;
                                gap: 6px;
                                font-family: -apple-system, BlinkMacSystemFont, sans-serif;
                                transition: transform 0.2s ease;
                                box-sizing: border-box;
                            }}
                        .sticky-folder-banner.collapsed {{
                            transform: translateX(calc(100% - 30px));
                        }}
                        .sticky-folder-banner.collapsed .banner-content {{
                            opacity: 0;
                            pointer-events: none;
                        }}
                        .sticky-folder-banner .banner-toggle {{
                            cursor: pointer;
                            padding: 2px 6px;
                            font-size: 0.6rem;
                            opacity: 0.7;
                            transition: transform 0.2s ease;
                        }}
                        .sticky-folder-banner .banner-toggle:hover {{
                            opacity: 1;
                        }}
                        .sticky-folder-banner.collapsed .banner-toggle {{
                            transform: rotate(180deg);
                        }}
                        .sticky-folder-banner .banner-content {{
                            display: flex;
                            align-items: center;
                            gap: 6px;
                            transition: opacity 0.2s ease;
                        }}
                        .sticky-folder-banner .hash-highlight {{
                            background: #ffd700;
                            color: #000;
                            padding: 1px 5px;
                            border-radius: 3px;
                            font-weight: bold;
                        }}
                        .sticky-folder-banner .hash-highlight-dull {{
                            background: rgba(255, 215, 0, 0.3);
                            color: rgba(255, 255, 255, 0.7);
                            padding: 1px 5px;
                            border-radius: 3px;
                            font-size: 0.65rem;
                        }}
                        .sticky-folder-banner .folder-name {{
                            opacity: 0.85;
                            font-size: 0.65rem;
                        }}
                        .sticky-folder-banner .nav-btn {{
                            cursor: pointer;
                            padding: 2px 8px;
                            background: rgba(255,255,255,0.2);
                            border-radius: 3px;
                            font-weight: bold;
                            user-select: none;
                        }}
                        .sticky-folder-banner .nav-btn:hover:not(.disabled) {{
                            background: rgba(255,255,255,0.4);
                        }}
                        .sticky-folder-banner .nav-btn.disabled {{
                            opacity: 0.3;
                            cursor: not-allowed;
                        }}
                        .sticky-folder-banner .prev-info,
                        .sticky-folder-banner .next-info {{
                            opacity: 0.6;
                            font-size: 0.65rem;
                        }}
                        .sticky-folder-banner .current-info {{
                            font-size: 0.85rem;
                        }}
                        .sticky-folder-banner .folder-status-trigger {{
                            cursor: pointer;
                            user-select: none;
                            border-radius: 999px;
                            padding: 2px 8px;
                            border: 1px solid rgba(255, 255, 255, 0.12);
                            background: rgba(255, 255, 255, 0.08);
                            color: white;
                            font: inherit;
                            font-weight: 800;
                        }}
                        .sticky-folder-banner .folder-status-trigger:hover {{
                            border-color: rgba(250, 204, 21, 0.8);
                            background: rgba(250, 204, 21, 0.18);
                        }}
                        .sticky-folder-banner .folder-status-popover {{
                            display: none;
                            position: absolute;
                            right: 8px;
                            bottom: calc(100% + 8px);
                            width: min(760px, calc(100vw - 140px));
                            max-height: 52vh;
                            overflow: auto;
                            padding: 10px;
                            border-radius: 14px;
                            border: 1px solid rgba(255, 255, 255, 0.22);
                            background: rgba(8, 13, 20, 0.96);
                            box-shadow: 0 18px 46px rgba(0, 0, 0, 0.42);
                            z-index: 1000001;
                        }}
                        .sticky-folder-banner.folder-status-open .folder-status-popover {{
                            display: block;
                        }}
                        .sticky-folder-banner .folder-status-popover-title {{
                            margin-bottom: 8px;
                            color: rgba(255, 255, 255, 0.78);
                            font-size: 11px;
                            font-weight: 900;
                            letter-spacing: 0.06em;
                            text-transform: uppercase;
                        }}
                        .sticky-folder-banner .folder-status-popover-list {{
                            display: grid;
                            gap: 5px;
                        }}
                        .sticky-folder-banner .folder-status-item {{
                            display: grid;
                            grid-template-columns: minmax(0, 1fr) auto;
                            align-items: center;
                            gap: 10px;
                            padding: 6px 8px;
                            border-radius: 10px;
                            border: 1px solid rgba(255, 255, 255, 0.09);
                            background: rgba(255, 255, 255, 0.045);
                            color: rgba(255, 255, 255, 0.86);
                            font-size: 12px;
                              cursor: pointer;
                              width: 100%;
                              text-align: left;
                              text-decoration: none;
                          }}
                        .sticky-folder-banner .folder-status-item:hover {{
                            background: rgba(255, 255, 255, 0.105);
                            border-color: rgba(250, 204, 21, 0.34);
                        }}
                        .sticky-folder-banner .folder-status-item-name {{
                            min-width: 0;
                            overflow: hidden;
                            text-overflow: ellipsis;
                            white-space: nowrap;
                        }}
                        .sticky-folder-banner .folder-status-tags {{
                            display: inline-flex;
                            align-items: center;
                            justify-content: flex-end;
                            gap: 6px;
                            white-space: nowrap;
                        }}
                        .sticky-folder-banner .folder-status-item-tag {{
                            padding: 2px 8px;
                            border-radius: 999px;
                            font-size: 10px;
                            font-weight: 900;
                            white-space: nowrap;
                            border: 1px solid rgba(255, 255, 255, 0.14);
                        }}
                        .sticky-folder-banner .folder-status-fully-labeled .folder-status-item-tag {{
                            color: #bbf7d0;
                            background: rgba(34, 197, 94, 0.18);
                            border-color: rgba(134, 239, 172, 0.36);
                        }}
                        .sticky-folder-banner .folder-status-partial .folder-status-item-tag {{
                            color: #fde68a;
                            background: rgba(245, 158, 11, 0.18);
                            border-color: rgba(251, 191, 36, 0.38);
                        }}
                        .sticky-folder-banner .folder-status-unlabeled .folder-status-item-tag {{
                            color: #d1d5db;
                            background: rgba(148, 163, 184, 0.14);
                            border-color: rgba(209, 213, 219, 0.24);
                        }}
                        .sticky-folder-banner .folder-status-missing .folder-status-item-tag,
                        .sticky-folder-banner .folder-status-missing-one .folder-status-item-tag {{
                            color: #fed7aa;
                            background: rgba(249, 115, 22, 0.18);
                            border-color: rgba(251, 146, 60, 0.42);
                        }}
                        .sticky-folder-banner .folder-status-errors .folder-status-item-tag {{
                            color: #fecaca;
                            background: rgba(239, 68, 68, 0.22);
                            border-color: rgba(252, 165, 165, 0.48);
                        }}
                        .sticky-folder-banner .folder-status-video-tag {{
                            padding: 2px 8px;
                            border-radius: 999px;
                            font-size: 10px;
                            font-weight: 900;
                            border: 1px solid rgba(255, 255, 255, 0.14);
                        }}
                        .sticky-folder-banner .folder-video-yes .folder-status-video-tag {{
                            color: #bfdbfe;
                            background: rgba(59, 130, 246, 0.18);
                            border-color: rgba(147, 197, 253, 0.36);
                        }}
                        .sticky-folder-banner .folder-video-no .folder-status-video-tag {{
                            color: #e5e7eb;
                            background: rgba(148, 163, 184, 0.12);
                            border-color: rgba(209, 213, 219, 0.22);
                        }}
                        .sticky-folder-banner .grades {{
                            background: #4CAF50;
                            color: white;
                            padding: 1px 6px;
                            border-radius: 3px;
                            font-weight: bold;
                            font-size: 0.7rem;
                        }}
                        .sticky-folder-banner .note {{
                            background: #ff6b6b;
                            color: white;
                            padding: 1px 6px;
                            border-radius: 3px;
                            font-size: 0.7rem;
                            font-style: italic;
                        }}
                    `;

                    // Add styles if not present
                    let styleEl = doc.getElementById('banner-styles');
                    if (!styleEl) {{
                        styleEl = doc.createElement('style');
                        styleEl.id = 'banner-styles';
                        doc.head.appendChild(styleEl);
                    }}
                    styleEl.textContent = bannerStyles;

                    // Create or update banner
                    let banner = doc.getElementById('folder-banner');
                    const wasCollapsed = banner && banner.classList.contains('collapsed');
                    if (!banner) {{
                        banner = doc.createElement('div');
                        banner.id = 'folder-banner';
                        banner.className = 'sticky-folder-banner';
                        doc.body.appendChild(banner);
                    }}
                      banner.innerHTML = `{banner_content_js}`;
                      if (wasCollapsed) banner.classList.add('collapsed');

                      function syncBottomDockLayout() {{
                          try {{
                              const selectionBar = doc.querySelector('.selection-bar');
                              const bannerRect = banner.getBoundingClientRect();
                              const selectionRect = selectionBar ? selectionBar.getBoundingClientRect() : null;
                              const bannerWidth = Math.max(300, Math.ceil(bannerRect.width || 0));
                              const selectionHeight = selectionRect && selectionRect.height
                                  ? Math.max(34, Math.ceil(selectionRect.height))
                                  : 38;
                              doc.documentElement.style.setProperty('--folder-banner-width', bannerWidth + 'px');
                              doc.documentElement.style.setProperty('--selection-bar-height', selectionHeight + 'px');
                              if (doc.body) {{
                                  doc.body.classList.add('report-labeler-bottom-dock');
                              }}
                          }} catch (layoutErr) {{
                              // best-effort layout sync; default CSS vars keep controls usable
                          }}
                      }}
                      topWindow.__reportLabelerSyncBottomDockLayout = syncBottomDockLayout;
                      syncBottomDockLayout();
                      setTimeout(syncBottomDockLayout, 0);
                      setTimeout(syncBottomDockLayout, 120);

                      // Toggle handler
                      const toggleBtn = doc.getElementById('banner-toggle');
                    if (toggleBtn) {{
                        toggleBtn.onclick = function() {{
                            banner.classList.toggle('collapsed');
                        }};
                    }}

                    function bindFolderStatusPopover(activeDoc) {{
                        if (!activeDoc || !activeDoc.getElementById) return;
                        const activeBanner = activeDoc.getElementById('folder-banner');
                        const folderStatusTrigger = activeDoc.getElementById('folder-status-trigger');
                        const folderStatusPopover = activeDoc.getElementById('folder-status-popover');
                        if (!activeBanner || !folderStatusTrigger || !folderStatusPopover) return;
                        folderStatusTrigger.onclick = function(e) {{
                            e.preventDefault();
                            e.stopPropagation();
                            activeBanner.classList.toggle('folder-status-open');
                        }};
                        folderStatusPopover.querySelectorAll('.folder-status-item[data-folder-index]').forEach((item) => {{
                            item.onclick = function(e) {{
                                e.preventDefault();
                                e.stopPropagation();
                                  const index = Number(item.getAttribute('data-folder-index'));
                                  if (!Number.isInteger(index) || index < 0) return;
                                  try {{
                                      topWindow[restoreScrollKey] = topWindow.scrollY || activeDoc.documentElement.scrollTop || 0;
                                      const href = item.getAttribute('href') || '';
                                      const url = href
                                          ? new URL(href, topWindow.location.href)
                                          : new URL(topWindow.location.href);
                                      url.searchParams.set('folder_index', String(index));
                                      url.searchParams.set('folder_nav_token', String(Date.now()));
                                      topWindow.location.assign(url.toString());
                                  }} catch (navErr) {{
                                      console.warn('folder status navigation failed', navErr);
                                  }}
                            }};
                        }});
                        if (activeDoc.__reportLabelerFolderStatusOutsideClick !== true) {{
                              activeDoc.__reportLabelerFolderStatusOutsideClick = true;
                              activeDoc.addEventListener('click', function(e) {{
                                  const latestBanner = activeDoc.getElementById('folder-banner');
                                  const latestSelectionBar = activeDoc.querySelector('.selection-bar');
                                  if (latestSelectionBar && latestSelectionBar.contains(e.target)) {{
                                      return;
                                  }}
                                  if (latestBanner && !latestBanner.contains(e.target)) {{
                                      latestBanner.classList.remove('folder-status-open');
                                  }}
                              }}, true);
                        }}
                    }}
                    bindFolderStatusPopover(doc);
                    try {{
                        bindFolderStatusPopover(window.document);
                    }} catch (bindErr) {{
                        // Same-origin fallback only; banner already exists in parent doc.
                    }}

	                    // Helper to click nav buttons while preserving scroll to avoid viewport jumps.
	                    function cleanupInjectedUiBeforeFolderNav() {{
	                        try {{
	                            doc.querySelectorAll('.selection-bar, .lightbox-overlay, .lightbox-viewer, .lightbox-close, #verification-overlay-panel').forEach((el) => {{
	                                if (el && el.parentNode) {{
	                                    el.parentNode.removeChild(el);
	                                }}
	                            }});
                            doc.querySelectorAll('.img-container.selected, .img-container.focused, .img-container.highlighted, .img-container.copy-flash, .img-container.label-inspection-highlight, .img-container.image-jump-flash').forEach((el) => {{
                                el.classList.remove('selected', 'focused', 'highlighted', 'copy-flash', 'label-inspection-highlight', 'image-jump-flash');
                                el.style.outline = '';
                                el.style.outlineOffset = '';
                                el.style.boxShadow = '';
                            }});
	                        }} catch (cleanupErr) {{
	                            // best-effort cleanup before Streamlit replaces the grid
	                        }}
	                    }}

	                    function clickNavButton(dir) {{
                        const currentScroll = (() => {{
                            try {{
                                return topWindow.scrollY || topDoc.documentElement.scrollTop || topDoc.body.scrollTop || 0;
                            }} catch (err) {{
                                return 0;
                            }}
                        }})();

                        const buttons = doc.querySelectorAll('button');
                        for (let i = 0; i < buttons.length; i++) {{
                            const btn = buttons[i];
                            if (btn.textContent.includes(dir) && !btn.disabled) {{
                                try {{
                                    topWindow[restoreScrollKey] = currentScroll;
                                }} catch (e) {{
                                    // Persisted state unavailable in some embedding states.
                                }}
	                                try {{
	                                    const active = topDoc.activeElement;
	                                    if (active && active.blur) active.blur();
	                                }} catch (e) {{
	                                    // Ignore focus errors; they are not critical for navigation.
	                                }}
	                                cleanupInjectedUiBeforeFolderNav();
	                                btn.click();
	                                return;
                            }}
                        }}
                    }}

                    // Setup keyboard nav (only once) - controlled by toggle
                    // Remove old handler if exists
                    if (doc.body.__folderNavHandler) {{
                        doc.removeEventListener('keydown', doc.body.__folderNavHandler, true);
                    }}

                    // Add Shift+Arrow folder navigation handler if toggle is enabled
                    const shiftArrowFolderNavEnabled = {shift_arrow_folder_nav_js};
                    if (shiftArrowFolderNavEnabled) {{
                        function folderNavHandler(e) {{
                            // Only handle Shift+Arrow keys for folder navigation
                            if (!e.shiftKey) return;
                            if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;

                            // Don't handle if in input/textarea
                            const tag = e.target.tagName.toUpperCase();
                            if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) return;

                            e.preventDefault();
                            e.stopPropagation();

                            if (e.key === 'ArrowLeft') {{
                                clickNavButton('Prev');
                            }} else if (e.key === 'ArrowRight') {{
                                clickNavButton('Next');
                            }}
                        }}
                        doc.body.__folderNavHandler = folderNavHandler;
                        doc.addEventListener('keydown', folderNavHandler, true);
                    }}

                    // "/" key focuses the folder search input
                    if (!doc.body.__searchFocusHandler) {{
                        function searchFocusHandler(e) {{
                            if (e.key !== '/') return;
                            const tag = e.target.tagName.toUpperCase();
                            if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) return;
                            e.preventDefault();
                            // Find the search input in the parent Streamlit doc
                            const topDoc = window.parent.document || document;
                            const inputs = topDoc.querySelectorAll('input[aria-label="Search folders"]');
                            if (inputs.length > 0) {{
                                inputs[0].focus();
                                inputs[0].select();
                            }}
                        }}
                        doc.body.__searchFocusHandler = searchFocusHandler;
                        doc.addEventListener('keydown', searchFocusHandler, true);
                        // Also listen on the parent doc so it works when focus is outside the iframe
                        try {{
                            const topDoc = window.parent.document;
                            if (topDoc && !topDoc.body.__searchFocusHandler) {{
                                topDoc.body.__searchFocusHandler = searchFocusHandler;
                                topDoc.addEventListener('keydown', searchFocusHandler, true);
                            }}
                        }} catch(ignored) {{}}
                    }}

                    // Click handlers for banner nav buttons
                    const prevBtn = doc.getElementById('banner-prev');
                    const nextBtn = doc.getElementById('banner-next');
                    if (prevBtn) {{
                        prevBtn.onclick = function() {{
                            if (!this.classList.contains('disabled')) clickNavButton('Prev');
                        }};
                    }}
                    if (nextBtn) {{
                        nextBtn.onclick = function() {{
                            if (!this.classList.contains('disabled')) clickNavButton('Next');
                        }};
                    }}

                }} catch(err) {{
                    console.error('Banner error:', err);
                }}
            }})();
            </script>
            """,
            height=1,
            scrolling=False,
        )
    else:
        st.warning("No subdirectories found in parent directory.")
        # Fallback to manual input
        query_params = st.query_params
        if "folder" in query_params:
            default_dir = strip_quotes(query_params["folder"])
        else:
            default_dir = str(parent_dir)
        folder_str = st.text_input("Folder with images (manual entry)", default_dir)
        folder = Path(strip_quotes(folder_str)).expanduser()

    # Show in Finder for current folder (always available)
    if folder.exists():
        if st.button("Show current folder in Finder", key="reveal-current"):
            reveal_in_finder(folder)

    col_controls = st.columns(3)
    with col_controls[0]:
        cols_per_row = st.slider("Columns per row", 1, 10, 4, key="ui_cols_per_row")
    with col_controls[1]:
        size_mode = st.radio(
            "Size mode",
            ("Stretch to column", "Fixed width (px)"),
            index=0,
            key="ui_size_mode",
        )
    with col_controls[2]:
        fixed_width = st.slider("Fixed width (px)", 100, 1600, 600, step=50, key="ui_fixed_width")

    col_opts = st.columns(4)
    with col_opts[0]:
        recursive = st.checkbox("Search subfolders", value=False, key="ui_recursive")
    with col_opts[1]:
        sort_by = st.radio("Sort by", ("name", "modified"), horizontal=True, key="ui_sort_by")
    with col_opts[2]:
        auto_copy_on_hover = st.checkbox("Auto-copy on hover", value=False, key="ui_auto_copy", help="Copy original image to clipboard when hovering")
    with col_opts[3]:
        shift_arrow_folder_nav = st.checkbox(
            "Shift+Arrow folder nav",
            key="shift_arrow_folder_nav",
            help="Use Shift+Arrow to change folders (disables image selection)"
        )
    render_full_quality = st.checkbox(
        "Render visible grid images at full quality",
        value=False,
        key="ui_render_full_quality",
        help=f"Disable downscale for visible cards. Full mode uses source resolution; default HD output is limited to {GRID_HD_DPI} DPI quality profile.",
    )

    images = list_images(folder, recursive, sort_by)

    if not folder.exists():
        st.error("Folder does not exist.")
        return

    st.write(f"**{len(images)} image(s) found** in `{folder}`")

    # ===== Open any file (by path) =====
    st.subheader("Open any file by path (not just images)")
    any_path_str = st.text_input(
        "Absolute path to open",
        "",
        placeholder="/Users/.../some_file.ext",
        key="any-path",
    )
    if st.button("Open this file in OS default app"):
        if any_path_str.strip():
            open_file_in_os(Path(strip_quotes(any_path_str)).expanduser())
        else:
            st.warning("Please enter a file path first.")

    # ===== Select & open image from the current folder =====
    if images:
        st.subheader("Open one of the images from this folder")
        choose_img = st.selectbox(
            "Pick an image to open in OS viewer",
            images,
            format_func=lambda p: p.name,
        )
        if st.button("Open selected image in OS default app"):
            open_file_in_os(choose_img)

    st.subheader("Grid view")

    if not images:
        st.info("No images found yet. Adjust the folder or options above.")
        return

    # Write image list for keyboard navigation (Hammerspoon)
    try:
        current_idx = 0
        if IMAGE_INDEX_FILE.exists():
            old_data = json.loads(IMAGE_INDEX_FILE.read_text())
            current_idx = old_data.get('current', 0)
        # Clamp to valid range
        current_idx = max(0, min(current_idx, len(images) - 1))
        IMAGE_INDEX_FILE.write_text(json.dumps({
            'images': [str(p) for p in images],
            'current': current_idx,
            'folder': str(folder)
        }))
    except:
        pass

    # ===== Display grid =====
    # Calculate thumbnail size based on display settings
    thumb_size = fixed_width if size_mode == "Fixed width (px)" else 600
    preload_images = list_adjacent_folder_preload_images(
        subdirs,
        st.session_state.folder_index,
        recursive,
        sort_by,
    )
    preload_thumbnail_urls = [
        (
            f"http://127.0.0.1:{CLIPBOARD_PORT}/thumbnail?path="
            f"{quote(str(path))}&max_size={thumb_size}&full_quality={'1' if render_full_quality else '0'}"
        )
        for path in preload_images
    ]
    image_annotations = load_image_annotations()
    rendered_annotation_labels_by_path: dict[str, list[str]] = {}
    rendered_annotation_paths_by_label: dict[str, list[str]] = {}
    for annotated_img in images:
        annotated_path = str(annotated_img.resolve())
        labels_for_path = _normalize_annotation_stored(image_annotations.get(annotated_path, []))
        rendered_annotation_labels_by_path[annotated_path] = labels_for_path
        for label in labels_for_path:
            rendered_annotation_paths_by_label.setdefault(label, []).append(annotated_path)
    rendered_annotation_counts = {
        label: len(set(paths))
        for label, paths in rendered_annotation_paths_by_label.items()
    }
    folder_processing_state = get_folder_processing_state(folder, refresh_instant_off=True)
    instant_off_status = folder_processing_state.get("instant_off", _resolve_instant_off_status(folder))

    for i in range(0, len(images), cols_per_row):
        row_imgs = images[i : i + cols_per_row]
        cols = st.columns(len(row_imgs), gap="small")
        for col_idx, (col, img_path) in enumerate(zip(cols, row_imgs)):
            with col:
                # Use cached thumbnails for faster rendering
                thumb_bytes = get_thumbnail(str(img_path), max_size=thumb_size, full_quality=render_full_quality)
                quality_label = get_thumbnail_quality_label(str(img_path), max_size=thumb_size, full_quality=render_full_quality)
                try:
                    original_bytes = img_path.stat().st_size
                except Exception:
                    original_bytes = 0
                reduction_label = _calc_size_reduction_label(original_bytes, len(thumb_bytes))
                overlay_label = f"{quality_label} ({reduction_label})"
                img_id = f"img_{i}_{col_idx}"
                image_path = str(img_path.resolve())
                current_labels = _normalize_annotation_stored(image_annotations.get(image_path, []))
                label_class = " labeled" if current_labels else ""
                label_color = _label_to_color(current_labels[0]) if current_labels else ""
                container_style = f' style="--annotation-color: {label_color};"' if current_labels else ""
                badge_labels = ""
                thumb_url = (
                    "http://127.0.0.1:" +
                    str(CLIPBOARD_PORT) +
                    "/thumbnail?path=" +
                    quote(str(img_path)) +
                    "&max_size=" +
                    str(thumb_size) +
                    "&full_quality=" +
                    ("1" if render_full_quality else "0")
                )

                # Render image with hover copy button using custom HTML
                width_style = "width: 100%;" if size_mode == "Stretch to column" else f"width: {fixed_width}px;"

                st.markdown(
                    f'''
                    <div class="img-container{label_class}" id="{img_id}-container" data-path="{html.escape(image_path)}" data-labels='{html.escape(json.dumps(current_labels), quote=True)}'{container_style}>
                        <div class="quality-badge">{overlay_label}</div>
                    <div class="label-badges">{badge_labels}</div>
                        <img
                            src="{thumb_url}"
                            alt="{html.escape(img_path.name)}"
                            loading="lazy"
                            decoding="async"
                            draggable="false"
                            style="{width_style}"
                        />
                        <button type="button" class="enlarge-btn" title="View full size" tabindex="-1">⛶</button>
                        <button type="button" class="rotate-btn" title="Rotate 90° clockwise" tabindex="-1">↻</button>
                        <button type="button" class="copy-btn" title="Copy original to clipboard" tabindex="-1">⧉</button>
                    </div>
                    ''',
                    unsafe_allow_html=True,
                )
                st.caption(img_path.name)

    # Inject JavaScript to handle hover/click copy via background server (no page reload)
    auto_copy_js = "true" if auto_copy_on_hover else "false"
    shift_arrow_folder_nav_js = "true" if shift_arrow_folder_nav else "false"

    js_code = f"""
    <script>
    (function() {{
        const currentDocument = window.document;
        let parent = currentDocument;
        function safeGetDocument(targetWindow) {{
            try {{
                if (!targetWindow || !targetWindow.document) {{
                    return null;
                }}
                const candidate = targetWindow.document;
                return candidate.querySelectorAll ? candidate : null;
            }} catch (err) {{
                return null;
            }}
        }}
        function countImageContainers(doc) {{
            try {{
                if (!doc || !doc.querySelectorAll) {{
                    return 0;
                }}
                return doc.querySelectorAll('.img-container').length;
            }} catch (err) {{
                return 0;
            }}
        }}
        function resolveInteractionDocument() {{
            const preferred = [
                safeGetDocument(window.parent),
                safeGetDocument(window.top),
                currentDocument,
            ];
            for (let i = 0; i < preferred.length; i++) {{
                const doc = preferred[i];
                if (doc && countImageContainers(doc) > 0) {{
                    return doc;
                }}
            }}
            return preferred[0] || preferred[1] || currentDocument;
        }}
        parent = resolveInteractionDocument() || currentDocument;
        const activeScriptToken = 'report-labeler-' + Date.now() + '-' + Math.random().toString(36).slice(2);
        const activeScriptKey = '__reportLabelerActiveScriptToken';
        const activeTimerKey = '__reportLabelerActiveTimers';
        const hostWindow = (parent && parent.defaultView) ? parent.defaultView : window;
        try {{
            const staleTimers = Array.isArray(hostWindow[activeTimerKey]) ? hostWindow[activeTimerKey] : [];
            staleTimers.forEach((timer) => {{
                try {{
                    if (timer && timer.owner && timer.id) {{
                        timer.owner.clearInterval(timer.id);
                    }}
                }} catch (err) {{}}
            }});
            hostWindow[activeTimerKey] = [];
            hostWindow[activeScriptKey] = activeScriptToken;
        }} catch (err) {{}}
        function isActiveScriptInstance() {{
            try {{
                return hostWindow[activeScriptKey] === activeScriptToken;
            }} catch (err) {{
                return false;
            }}
        }}
        function setActiveInterval(callback, delayMs) {{
            const timerId = window.setInterval(() => {{
                if (!isActiveScriptInstance()) {{
                    window.clearInterval(timerId);
                    return;
                }}
                callback();
            }}, delayMs);
            try {{
                const timers = Array.isArray(hostWindow[activeTimerKey]) ? hostWindow[activeTimerKey] : [];
                timers.push({{ owner: window, id: timerId }});
                hostWindow[activeTimerKey] = timers;
            }} catch (err) {{}}
            return timerId;
        }}
        const brightnessStorageKey = 'image-grid-brightness';
        const brightnessPresetKey = 'image-grid-brightness-preset';
        const defaultBrightness = 100;
        const autoCopyEnabled = {auto_copy_js};
        const shiftArrowFolderNav = {shift_arrow_folder_nav_js};
        const clipboardPorts = {json.dumps(_CLIPBOARD_PORT_SCAN_LIST)};
        let clipboardServerBase = 'http://127.0.0.1:{CLIPBOARD_PORT}';
        let clipboardServerUrl = clipboardServerBase + '/copy?path=';
        let clipboardIndexUrl = clipboardServerBase + '/index';
        let clipboardStartDragUrl = clipboardServerBase + '/start-drag?paths=';
        const activeFolderPath = {json.dumps(str(folder))};
        const adjacentFolderPreloadThumbnailUrls = {json.dumps(preload_thumbnail_urls)};
        const baseAnnotationPresetGroups = {json.dumps(REPORT_LABELER_LABEL_PRESET_GROUPS)};
        const baseAnnotationPresetLabelLookup = (() => {{
            const lookup = Object.create(null);
            Object.keys(baseAnnotationPresetGroups).forEach((tableName) => {{
                const labels = Array.isArray(baseAnnotationPresetGroups[tableName]) ? baseAnnotationPresetGroups[tableName] : [];
                labels.forEach((label) => {{
                    if (typeof label !== 'string') {{
                        return;
                    }}
                    const key = normalizeLabelText(label);
                    if (key) {{
                        lookup[key] = label;
                    }}
                }});
            }});
            return lookup;
        }})();
        const baseAnnotationPresetOrder = (() => {{
            const out = Object.create(null);
            Object.keys(baseAnnotationPresetGroups).forEach((tableName) => {{
                const order = Object.create(null);
                const labels = Array.isArray(baseAnnotationPresetGroups[tableName]) ? baseAnnotationPresetGroups[tableName] : [];
                labels.forEach((label, index) => {{
                    if (typeof label === 'string') {{
                        order[normalizeLabelText(label)] = index;
                    }}
                }});
                out[tableName] = order;
            }});
            return out;
        }})();
        let annotationPresetGroups = Object.assign({{}}, baseAnnotationPresetGroups);
        let clientAnnotationLabelsByPath = {json.dumps(rendered_annotation_labels_by_path)};
        let clientAnnotationLabelCounts = {json.dumps(rendered_annotation_counts)};
        const tableStationSuffixes = {json.dumps({k: list(v) for k, v in REPORT_LABELER_TABLE_STATION_SUFFIXES.items()})};
        const tablePresetOrder = {json.dumps([f"Table {t}" for t in REPORT_LABELER_TABLES])};
        const tableSelectionLabelCaps = {{
            "3": 5,
        }};
        const instantOffStatusChoices = {json.dumps(list(REPORT_LABELER_INSTANT_OFF_STATUS_LABELS))};
        const labelRenameMapStorageKey = 'report-labeler-label-rename-map-v1';
        const stationAnodeStateStorageKey = 'report-labeler-station-anode-state-v1';
        const tableStationAnodeOptions = [3, 4];
        let tableFourQuickLabels = [];
        let annotationFlatPresets = [];
        let annotationPresetEntries = [];
        const presetStateStorageKey = 'report-labeler-annotation-presets-v1';
        const instantOffStatusStorageKey = 'report-labeler-instant-off-status-v1';
        const legacyTableAnodeStateStorageKey = 'report-labeler-table-anode-state-v1';
        const legacyStationAnodeStateStorageKey = 'report-labeler-table-station-anode-state-v1';
        const folderInstantOffStatusState = {json.dumps(instant_off_status)};
        const folderInstantOffStatus = folderInstantOffStatusState && typeof folderInstantOffStatusState.status === 'string'
            ? folderInstantOffStatusState.status
            : '';
        const folderInstantOffVideo = folderInstantOffStatusState && typeof folderInstantOffStatusState.video_file === 'string'
            ? folderInstantOffStatusState.video_file
            : '';
        const initialFolderProcessingState = {json.dumps(folder_processing_state)};
        let folderEmptySlotState = normalizeFolderEmptySlotState(
            initialFolderProcessingState && initialFolderProcessingState.empty_slots
                ? initialFolderProcessingState.empty_slots
                : {{}}
        );
        const verificationOverlayStorageKey = 'report-labeler-verification-overlay-v1';
        let verificationOverlayVisible = false;
        let verificationOverlayFrame = null;
        let selectionDebugPanelVisible = false;
        let selectionDebugRenderFrame = null;
        let selectionDebugPanelMode = 'selection';
        let selectionDebugPanelLabel = '';
        let lastAutoNextClick = {{
            path: '',
            canonicalLabel: '',
        }};
        const annotationUndoStack = [];
        const annotationRedoStack = [];
        const annotationHistoryLimit = 80;
        let labelRenameMap = loadLabelRenameMap();
        labelRenameMap = normalizeAndRepairLabelRenameMap(labelRenameMap);
        const clipboardApiState = {{
            ready: null,
            checkedAt: 0,
            lastError: '',
        }};
        let hoverDebounce = null;
        let lastHoveredPath = null;

        applyRenameMapToPresetGroups();

        function normalizeHost(host) {{
            return host && host.includes(':') ? `[${{host}}]` : host;
        }}

        function resolveLabelForParsing(label) {{
            const canonical = getCanonicalPresetLabelForParsing(label);
            return canonical || String(label || '').trim();
        }}

        function normalizeRequiredSlotKey(labelOrKey) {{
            return String(labelOrKey || '').trim().toLowerCase().replace(/\\s+/g, ' ');
        }}

        function requiredSlotKeyForLabel(label) {{
            const parsed = resolveLabelForParsing(label);
            return normalizeRequiredSlotKey(parsed || label);
        }}

        function normalizeFolderEmptySlotState(rawSlots) {{
            if (!rawSlots || typeof rawSlots !== 'object' || Array.isArray(rawSlots)) {{
                return {{}};
            }}
            const normalized = {{}};
            Object.keys(rawSlots).forEach((rawKey) => {{
                const key = normalizeRequiredSlotKey(rawKey);
                if (!key) {{
                    return;
                }}
                const rawValue = rawSlots[rawKey];
                if (rawValue && typeof rawValue === 'object' && !Array.isArray(rawValue)) {{
                    normalized[key] = {{
                        label: String(rawValue.label || '').trim(),
                        value: String(rawValue.value || '-').trim() || '-',
                        updated_at: String(rawValue.updated_at || '').trim(),
                    }};
                }} else {{
                    normalized[key] = {{
                        label: String(rawValue || '').trim(),
                        value: '-',
                        updated_at: '',
                    }};
                }}
            }});
            return normalized;
        }}

        const labelNumberWordMap = {{
            one: '1',
            two: '2',
            three: '3',
            four: '4',
            five: '5',
            six: '6',
            seven: '7',
            eight: '8',
            nine: '9',
            ten: '10',
            zero: '0',
        }};

        function parseLabelNumberToken(raw) {{
            const value = String(raw || '').trim();
            if (!value) {{
                return '';
            }}
            const direct = /^\\d+$/.exec(value);
            if (direct) {{
                return direct[0];
            }}
            const token = value.toLowerCase().replace(/[^a-z]/gi, '').trim();
            return token && labelNumberWordMap[token] ? labelNumberWordMap[token] : '';
        }}

        function getCanonicalPresetLabelForParsing(label) {{
            const normalized = normalizeLabelText(label);
            if (!normalized) {{
                return '';
            }}
            if (baseAnnotationPresetLabelLookup[normalized]) {{
                return baseAnnotationPresetLabelLookup[normalized];
            }}
            const seen = new Set();
            let current = normalized;
            for (let i = 0; i < 50; i++) {{
                let predecessor = '';
                Object.keys(labelRenameMap || {{}}).forEach((sourceKey) => {{
                    if (predecessor || seen.has(sourceKey)) {{
                        return;
                    }}
                    if (normalizeLabelText(labelRenameMap[sourceKey]) === current) {{
                        predecessor = sourceKey;
                    }}
                }});
                if (!predecessor) {{
                    return '';
                }}
                if (baseAnnotationPresetLabelLookup[predecessor]) {{
                    return baseAnnotationPresetLabelLookup[predecessor];
                }}
                seen.add(predecessor);
                current = predecessor;
            }}
            return '';
        }}

        function getTableFromLabel(label) {{
            const parsed = resolveLabelForParsing(label);
            const match = /\\bTable\\s*(\\d+|[a-z]+)\\b/i.exec(parsed || '');
            if (!match) {{
                return null;
            }}
            const value = parseLabelNumberToken(match[1]);
            return value ? String(Number(value)) : null;
        }}

        function getTableSelectionCap(table) {{
            if (!table) {{
                return null;
            }}
            const cap = tableSelectionLabelCaps[table];
            return Number.isInteger(cap) && cap > 0 ? cap : null;
        }}

        function getStationSelectionCap(label) {{
            const table = getTableFromLabel(label);
            if (table === '4' && parseStationFromLabel(label)) {{
                return 2;
            }}
            return null;
        }}

        let folderStationAnodeState = {{}};
        let folderStationAnodeStateInitialized = false;

        function normalizeStationAnodeState(rawEntry) {{
            if (!rawEntry || typeof rawEntry !== 'object' || Array.isArray(rawEntry)) {{
                return {{}};
            }}
            const normalized = {{}};
            Object.keys(rawEntry).forEach((key) => {{
                const value = normalizeTableAnodeStateValue(rawEntry[key]);
                if (value) {{
                    const stationNumber = parseStationNumber(key) || (String(key) === '1' || String(key) === '2' ? Number(key) : 0);
                    if (stationNumber === 1 || stationNumber === 2) {{
                        normalized[String(stationNumber)] = value;
                    }}
                }}
            }});
            if (!normalized['1']) {{
                const legacyStationOne = normalizeTableAnodeStateValue(rawEntry['5']) || normalizeTableAnodeStateValue(rawEntry['6']);
                if (legacyStationOne) {{
                    normalized['1'] = legacyStationOne;
                }}
            }}
            return normalized;
        }}

        function readLocalStorageStationAnodeState() {{
            if (!activeFolderPath) return {{}};
            const mergeEntries = (data) => {{
                if (!data || typeof data !== 'object' || Array.isArray(data)) {{
                    return {{}};
                }}
                const rawEntry = data[activeFolderPath];
                return normalizeStationAnodeState(rawEntry);
            }};
            try {{
                const raw = parent.defaultView.localStorage.getItem(stationAnodeStateStorageKey);
                const data = safeJsonParse(raw);
                const folderEntry = mergeEntries(data);
                if (folderEntry && Object.keys(folderEntry).length) {{
                    return folderEntry;
                }}
                const legacyTableRaw = parent.defaultView.localStorage.getItem(legacyTableAnodeStateStorageKey);
                const legacyTableData = safeJsonParse(legacyTableRaw);
                const migratedTableState = mergeEntries(legacyTableData);
                if (migratedTableState && Object.keys(migratedTableState).length) {{
                    setFolderStationAnodeState(migratedTableState);
                    return migratedTableState;
                }}
                const legacyRaw = parent.defaultView.localStorage.getItem(legacyStationAnodeStateStorageKey);
                const legacyData = safeJsonParse(legacyRaw);
                const migrated = mergeEntries(legacyData);
                if (migrated && Object.keys(migrated).length) {{
                    setFolderStationAnodeState(migrated);
                    return migrated;
                }}
            }} catch (err) {{
                return {{}};
            }}
            return {{}};
        }}

        function getFolderStationAnodeState() {{
            if (!folderStationAnodeStateInitialized) {{
                const persistedCounts = initialFolderProcessingState && initialFolderProcessingState.station_anode_counts
                    ? initialFolderProcessingState.station_anode_counts
                    : {{}};
                folderStationAnodeState = normalizeStationAnodeState(persistedCounts);
                if (!Object.keys(folderStationAnodeState).length) {{
                    folderStationAnodeState = readLocalStorageStationAnodeState();
                    if (Object.keys(folderStationAnodeState).length) {{
                        persistFolderStationAnodeState(folderStationAnodeState);
                    }}
                }}
                folderStationAnodeStateInitialized = true;
            }}
            return Object.assign({{}}, folderStationAnodeState);
        }}

        function writeLocalStorageStationAnodeState(nextState) {{
            if (!activeFolderPath) return;
            try {{
                const raw = parent.defaultView.localStorage.getItem(stationAnodeStateStorageKey);
                let data = safeJsonParse(raw);
                if (typeof data !== 'object' || data === null || Array.isArray(data)) {{
                    data = {{}};
                }}
                const normalized = normalizeStationAnodeState(nextState);
                if (Object.keys(normalized).length) {{
                    data[activeFolderPath] = normalized;
                    parent.defaultView.localStorage.setItem(stationAnodeStateStorageKey, JSON.stringify(data));
                }} else {{
                    delete data[activeFolderPath];
                    parent.defaultView.localStorage.setItem(stationAnodeStateStorageKey, JSON.stringify(data));
                }}
            }} catch (err) {{
                return;
            }}
        }}

        async function persistFolderStationAnodeState(nextState) {{
            if (!activeFolderPath) return;
            try {{
                const normalized = normalizeStationAnodeState(nextState);
                const query = new URLSearchParams({{
                    action: 'set-station-anodes',
                    folder: activeFolderPath,
                    station_counts: JSON.stringify(normalized),
                }}).toString();
                const response = await fetch(clipboardServerBase + '/folder-state?' + query);
                const data = await response.json().catch(() => ({{}}));
                if (!data || !data.success) {{
                    logClientEvent('folder_state_persist_failed', {{
                        folder: activeFolderPath,
                        station_anode_counts: normalized,
                    }});
                }}
            }} catch (err) {{
                logClientEvent('folder_state_persist_error', {{
                    folder: activeFolderPath,
                    error: err && err.message ? err.message : String(err),
                }});
            }}
        }}

        function setFolderStationAnodeState(nextState) {{
            folderStationAnodeState = normalizeStationAnodeState(nextState);
            folderStationAnodeStateInitialized = true;
            writeLocalStorageStationAnodeState(folderStationAnodeState);
            persistFolderStationAnodeState(folderStationAnodeState);
        }}

        function normalizeStationValue(value) {{
            return String(value || '').replace(/\\s+/g, ' ').trim();
        }}

        function normalizeTableAnodeStateValue(rawValue) {{
            if (typeof rawValue === 'number') {{
                const value = Number(rawValue);
                return Number.isInteger(value) && tableStationAnodeOptions.indexOf(value) >= 0 ? value : 0;
            }}
            if (!rawValue || typeof rawValue !== 'object' || Array.isArray(rawValue)) {{
                return 0;
            }}
            const values = Object.values(rawValue).map((item) => Number(item)).filter((value) => Number.isInteger(value) && tableStationAnodeOptions.indexOf(value) >= 0);
            if (!values.length) {{
                return 0;
            }}
            return Math.max.apply(null, values);
        }}

        function parseStationFromLabel(label) {{
            const parsed = resolveLabelForParsing(label);
            const match = /(?:test\\s*station|testation)\\s*(\\d+|[a-z]+)/i.exec(parsed || '');
            if (!match) {{
                return '';
            }}
            const station = parseLabelNumberToken(match[1]);
            return station ? `Test Station ${{station}}` : '';
        }}

        function parseStationNumber(station) {{
            const raw = String(station || '').trim();
            if (raw === '1' || raw === '2') {{
                return Number(raw);
            }}
            const match = /(?:test\\s*station|testation)\\s*(\\d+)/i.exec(raw);
            const value = match ? Number(match[1]) : 0;
            return Number.isInteger(value) ? value : 0;
        }}

        function getTableStationAnodeCount(table, station) {{
            return getStationAnodeCount(station || 'Test Station 1');
        }}

        function getStationAnodeCount(station) {{
            const folderState = getFolderStationAnodeState();
            const stationNumber = parseStationNumber(station);
            if (stationNumber !== 1 && stationNumber !== 2) {{
                return 0;
            }}
            return normalizeTableAnodeStateValue(folderState[String(stationNumber)]);
        }}

        function setTableStationAnodeCount(table, station, count) {{
            return setStationAnodeCount(station || 'Test Station 1', count);
        }}

        function setStationAnodeCount(station, count) {{
            const numericCount = Number(count);
            let nextCount = Number.isInteger(numericCount) ? numericCount : 0;
            if (nextCount > 0 && tableStationAnodeOptions.indexOf(nextCount) < 0) {{
                return;
            }}
            if (nextCount < 0) {{
                nextCount = 0;
            }}
            const current = getFolderStationAnodeState();
            const next = Object.assign({{}}, current);
            const stationNumber = parseStationNumber(station);
            if (stationNumber !== 1 && stationNumber !== 2) {{
                return;
            }}
            const stationKey = String(stationNumber);
            if (nextCount === 0) {{
                if (stationKey in next) {{
                    delete next[stationKey];
                }}
            }} else {{
                next[stationKey] = nextCount;
            }}
            setFolderStationAnodeState(next);
            return next;
        }}

        function parseLabelParts(label) {{
            const parsed = resolveLabelForParsing(label);
            const table = getTableFromLabel(parsed);
            if (!table) {{
                return null;
            }}
            const rowMatch = /(?:row|mg|md)\\s*(\\d+|[a-z]+)/i.exec(parsed || '');
            const station = parseStationFromLabel(label || '');
            return {{
                table,
                rowNumber: Number(parseLabelNumberToken(rowMatch ? rowMatch[1] : 0)) || 0,
                station,
            }};
        }}

        function getAnnotatedRowsForTableStation(table, station) {{
            if (!table || !station) {{
                return [];
            }}
            const targetStation = String(station).trim();
            const rows = new Set();
            parent.querySelectorAll('.img-container').forEach((container) => {{
                const labels = parseLabelList(container.dataset.labels || '[]');
                for (const label of labels) {{
                    const parsed = parseLabelParts(label);
                    if (!parsed || parsed.table !== String(table) || parsed.station !== targetStation) {{
                        continue;
                    }}
                    if (Number.isInteger(parsed.rowNumber) && parsed.rowNumber > 0) {{
                        rows.add(parsed.rowNumber);
                    }}
                }}
            }});
            return Array.from(rows).sort((a, b) => a - b);
        }}

        function getTableStationAnodeRange(table, station) {{
            if (!table) {{
                return {{start: 1, end: 999}};
            }}
            if (!tableNeedsAnodeCount(table)) {{
                return {{start: 1, end: 999}};
            }}
            const normalizedTable = String(table);
            const configuredStationOne = getStationAnodeCount('Test Station 1');
            const configuredStationTwo = getStationAnodeCount('Test Station 2');
            const observedStationOneRows = getAnnotatedRowsForTableStation(normalizedTable, 'Test Station 1');
            const stationOneCount = configuredStationOne > 0
                ? configuredStationOne
                : (observedStationOneRows.length > 0 ? Math.max(...observedStationOneRows) : 0);
            const stationNumber = parseStationNumber(station);
            const start = stationNumber === 2 && stationOneCount > 0
                ? stationOneCount + 1
                : 1;
            let end = 999;
            if (stationNumber === 1 && stationOneCount > 0) {{
                end = stationOneCount;
            }} else if (stationNumber === 2 && stationOneCount > 0 && configuredStationTwo > 0) {{
                end = stationOneCount + configuredStationTwo;
            }}
            return {{
                start: Math.max(1, start),
                end: Math.max(start, end),
            }};
        }}

        function isRowInTableAnodeRange(table, station, rowNumber) {{
            const range = getTableStationAnodeRange(table, station);
            const rowIndex = Number(rowNumber);
            if (!Number.isInteger(rowIndex) || rowIndex <= 0) {{
                return false;
            }}
            return rowIndex >= range.start && rowIndex <= range.end;
        }}

        function shouldShowStationPreset(label, table, station) {{
            const parsed = parseLabelParts(label);
            if (!parsed || parsed.table !== table) {{
                return false;
            }}
            if (station) {{
                if (parsed.station !== station) {{
                    return false;
                }}
                if (!tableNeedsAnodeCount(parsed.table)) {{
                    return true;
                }}
                if (!isRowInTableAnodeRange(parsed.table, station, parsed.rowNumber)) {{
                    return false;
                }}
            }}
            return true;
        }}

        function tableNeedsAnodeCount(table) {{
            return table === '5' || table === '6';
        }}

        function annotationGroupTitle(tableName, table, station) {{
            const stationSuffix = station ? ' ' + station : '';
            if (table === '3') {{
                return 'Table 3 Structure-to-Soil Potential';
            }}
            if (table === '4') {{
                return 'Table 4 Shunt Reading / Total Current' + stationSuffix;
            }}
            if (table === '5') {{
                return 'Table 5 CP Anode Current' + stationSuffix;
            }}
            if (table === '6') {{
                return 'Table 6 Open Potential' + stationSuffix;
            }}
            return tableName || '';
        }}

        function buildAnnotationPresetEntries() {{
            const entries = [];
            const entryOrder = (entry) => {{
                const table = entry[3] || '';
                const station = entry[4] || '';
                const stationNumber = parseStationNumber(station);
                if (table === '3') return 300;
                if (table === '4') return 400 + stationNumber;
                if ((table === '5' || table === '6') && stationNumber) {{
                    return 500 + (stationNumber * 10) + Number(table);
                }}
                return 900 + Number(table || 0);
            }};
            tablePresetOrder.forEach((tableName) => {{
                const labels = Array.isArray(annotationPresetGroups[tableName]) ? annotationPresetGroups[tableName] : [];
                const table = getTableFromLabel(tableName);
                const stations = tableStationSuffixes[table] || [];
                if (stations && stations.length) {{
                    stations.forEach((station) => {{
                        const needsAnodeCount = tableNeedsAnodeCount(table);
                        const count = needsAnodeCount ? getStationAnodeCount(station) : 0;
                        const filtered = needsAnodeCount
                            ? labels.filter((label) => shouldShowStationPreset(label, table, station))
                            : labels.filter((label) => parseStationFromLabel(label) === station);
                        entries.push([annotationGroupTitle(tableName, table, station), filtered, count, table, station]);
                    }});
                    return;
                }}
                entries.push([annotationGroupTitle(tableName, table, ''), labels, 0, table, '']);
            }});
            return entries
                .map((entry, index) => [entry, index])
                .sort((a, b) => {{
                    const delta = entryOrder(a[0]) - entryOrder(b[0]);
                    return delta || (a[1] - b[1]);
                }})
                .map((item) => item[0]);
        }}

        function getFlatPresetList() {{
            return buildAnnotationPresetEntries()
                .flatMap((entry) => Array.isArray(entry[1]) ? entry[1] : []);
        }}

        function getPresetEntryOrderKey(entry) {{
            const table = String((entry && entry[3]) || getTableFromLabel((entry && entry[0]) || '') || '');
            const station = String((entry && entry[4]) || '');
            const stationNumber = parseStationNumber(station);
            const stationTableOrder = {{ '4': 0, '5': 1, '6': 2 }};
            if ((stationNumber === 1 || stationNumber === 2) && Object.prototype.hasOwnProperty.call(stationTableOrder, table)) {{
                return 100 + ((stationNumber - 1) * 10) + stationTableOrder[table];
            }}
            if (table === '3') {{
                return 0;
            }}
            const fallbackTableOrder = {{ '4': 40, '5': 50, '6': 60 }};
            return Object.prototype.hasOwnProperty.call(fallbackTableOrder, table) ? fallbackTableOrder[table] : 999;
        }}

        function orderAnnotationPresetEntries(entries) {{
            const list = Array.isArray(entries) ? entries.slice() : [];
            return list.sort((left, right) => getPresetEntryOrderKey(left) - getPresetEntryOrderKey(right));
        }}

        function rebuildAnnotationPresets() {{
            applyRenameMapToPresetGroups();
            annotationPresetEntries = orderAnnotationPresetEntries(buildAnnotationPresetEntries());
            annotationFlatPresets = annotationPresetEntries.flatMap((entry) => {{
                const entryLabels = Array.isArray(entry[1]) ? entry[1] : [];
                return entryLabels;
            }});
        }}

        function parseVerificationCellLabel(label) {{
            const table = getTableFromLabel(label);
            if (!table) {{
                return null;
            }}
            const raw = String(label || '').replace(/^\\s*Table\\s+\\d+\\s+/i, '').trim();
            if (!raw) {{
                return null;
            }}
            const stationMatch = /(.*)\\s+((?:Test\\s+Station|Testation)\\s+\\d+)$/i.exec(raw);
            if (stationMatch) {{
                const rowName = stationMatch[1].trim().replace(/\\s+/g, ' ');
                if (!rowName) {{
                    return null;
                }}
                return {{
                    table,
                    row: rowName,
                    station: stationMatch[2].trim().replace(/\\s+/g, ' '),
                }};
            }}
            return {{
                table,
                row: raw.replace(/\\s+/g, ' '),
                station: '',
            }};
        }}

        const verificationTableTemplate = (() => {{
            const template = {{}};
            tablePresetOrder.forEach((tableName) => {{
                const rows = [];
                const stations = [];
                const labels = Array.isArray(annotationPresetGroups[tableName]) ? annotationPresetGroups[tableName] : [];
                labels.forEach((label) => {{
                    const parsed = parseVerificationCellLabel(label);
                    if (!parsed) {{
                        return;
                    }}
                    if (rows.indexOf(parsed.row) < 0) {{
                        rows.push(parsed.row);
                    }}
                    const station = parsed.station || '';
                    if (stations.indexOf(station) < 0) {{
                        stations.push(station);
                    }}
                }});
                if (rows.length) {{
                    template[tableName] = {{
                        rows,
                        stations: stations.length ? stations : [''],
                    }};
                }}
            }});
            return template;
        }})();

        function makeVerificationCellKey(rowName, station) {{
            return rowName + '||' + (station || '');
        }}

        function getVerificationFillState() {{
            const fillState = {{}};
            Object.keys(verificationTableTemplate).forEach((tableName) => {{
                fillState[tableName] = {{}};
            }});

            parent.querySelectorAll('.img-container').forEach((container) => {{
                const labels = normalizeLabelArray(container.dataset.labels || '[]');
                labels.forEach((label) => {{
                    const parsed = parseVerificationCellLabel(label || '');
                    if (!parsed) {{
                        return;
                    }}
                    const tableName = `Table ${{parsed.table}}`;
                    if (!verificationTableTemplate[tableName]) {{
                        return;
                    }}
                    const station = parsed.station || '';
                    fillState[tableName][makeVerificationCellKey(parsed.row, station)] = true;
                }});
            }});
            return fillState;
        }}

        function isVerificationOverlayVisible() {{
            return verificationOverlayVisible;
        }}

        function setVerificationOverlayVisible(next) {{
            verificationOverlayVisible = Boolean(next);
            try {{
                parent.defaultView.localStorage.setItem(verificationOverlayStorageKey, verificationOverlayVisible ? '1' : '0');
            }} catch (err) {{
                // localStorage blocked or unavailable
            }}
            renderVerificationOverlay();
        }}

        function toggleVerificationOverlay() {{
            setVerificationOverlayVisible(!verificationOverlayVisible);
        }}

        function hydrateVerificationOverlayState() {{
            try {{
                return parent.defaultView.localStorage.getItem(verificationOverlayStorageKey) === '1';
            }} catch (err) {{
                return false;
            }}
        }}

        function requestVerificationOverlayRefresh(force) {{
            if (!isVerificationOverlayVisible() && !force) {{
                return;
            }}
            if (verificationOverlayFrame) {{
                return;
            }}
            verificationOverlayFrame = parent.defaultView.requestAnimationFrame(() => {{
                verificationOverlayFrame = null;
                renderVerificationOverlay();
            }});
        }}

        function renderVerificationOverlay() {{
            let overlay = parent.getElementById('verification-overlay-panel');
            if (!overlay) {{
                overlay = parent.createElement('div');
                overlay.id = 'verification-overlay-panel';
                overlay.className = 'verification-overlay';
                parent.body.appendChild(overlay);
            }}

            if (!isVerificationOverlayVisible()) {{
                overlay.classList.remove('visible');
                return;
            }}

            overlay.classList.add('visible');
            const fillState = getVerificationFillState();
            const tables = Object.keys(verificationTableTemplate);
            const chunks = tables.map((tableName) => {{
                const config = verificationTableTemplate[tableName];
                const hasStationSplit = config.stations.length > 1 || (config.stations[0] !== '');
                let stationHeader = '';
                if (hasStationSplit) {{
                    stationHeader = config.stations
                        .map((station) => '<th>' + escapeHtml(station || '—') + '</th>')
                        .join('');
                }} else {{
                    stationHeader = '<th>Filled</th>';
                }}
                const rows = config.rows.map((rowName) => {{
                    let cells = '';
                    for (let si = 0; si < config.stations.length; si++) {{
                        const station = config.stations[si] || '';
                        const isFilled = !!fillState[tableName][makeVerificationCellKey(rowName, station)];
                        const dot = isFilled
                            ? '<span class=\"verification-dot filled\">●</span>'
                            : '<span class=\"verification-dot empty\">○</span>';
                        cells += '<td>' + dot + '</td>';
                    }}
                    return '<tr><td class=\"verification-row-label\">' + escapeHtml(rowName) + '</td>' + cells + '</tr>';
                }}).join('');
                return (
                    '<section class=\"verification-table-section\">' +
                    '<div class=\"verification-table-title\">' + escapeHtml(tableName) + '</div>' +
                    '<table class=\"verification-table\">' +
                    '<thead><tr><th>Row</th>' + stationHeader + '</tr></thead>' +
                    '<tbody>' + rows + '</tbody>' +
                    '</table>' +
                    '</section>'
                );
            }}).join('');

            const body = chunks.length ? chunks : '<div class=\"verification-empty\">No table templates found</div>';
            overlay.innerHTML =
                '<div class=\"verification-overlay-header\">' +
                '<span>Verification fill preview</span>' +
                '<button type=\"button\" class=\"verification-overlay-toggle\" id=\"verification-overlay-close\">Hide</button>' +
                '</div>' +
                '<div class=\"verification-overlay-body\">' +
                body +
                '</div>';
            const closeBtn = overlay.querySelector('#verification-overlay-close');
            if (closeBtn) {{
                closeBtn.addEventListener('click', function() {{
                    setVerificationOverlayVisible(false);
                }});
            }}
        }}

        verificationOverlayVisible = hydrateVerificationOverlayState();

        function normalizeStationLabel(label) {{
            return String(label || '')
                .replace(/^\\s*Table\\s+(\\d+)\\s+/i, 'Table $1 ')
                .replace(/\\s+(?:Test\\s+)?(?:Station|ation)\\s+\\d+$/i, '')
                .replace(/\\s+/g, ' ')
                .trim();
        }}

        function stripTablePrefix(label) {{
            return String(label || '').replace(/^\\s*Table\\s+\\d+\\s+/i, '').trim();
        }}

        function sanitizeRowName(label) {{
            return String(label || '')
                .replace(/\\s+(?:Test\\s+)?(?:Station|ation)\\s+\\d+$/i, '')
                .replace(/\\s+/g, ' ')
                .trim();
        }}

        function normalizeLabelText(value) {{
            return String(value || '').trim().toLowerCase();
        }}

        function isCanonicalPresetLabel(label) {{
            return !!baseAnnotationPresetLabelLookup[normalizeLabelText(label)];
        }}

        function getCanonicalPresetOrderForLabel(tableName, rawLabel) {{
            const canonical = getCanonicalPresetLabelForParsing(rawLabel);
            if (!canonical) {{
                return Number.MAX_SAFE_INTEGER;
            }}
            const parsedTable = getTableFromLabel(rawLabel);
            const resolvedTableName = tableName && String(tableName).trim()
                ? tableName
                : (parsedTable ? `Table ${{parsedTable}}` : '');
            if (!resolvedTableName) {{
                return Number.MAX_SAFE_INTEGER;
            }}
            const tableOrder = baseAnnotationPresetOrder[resolvedTableName];
            if (!tableOrder) {{
                return Number.MAX_SAFE_INTEGER;
            }}
            const order = tableOrder[normalizeLabelText(canonical)];
            return Number.isInteger(order) ? order : Number.MAX_SAFE_INTEGER;
        }}

        function sortPresetLabelsByCanonicalOrder(tableName, labels) {{
            if (!Array.isArray(labels) || labels.length < 2) {{
                return labels;
            }}
            const withIndex = labels.map((label, index) => ({{
                label,
                index,
                order: getCanonicalPresetOrderForLabel(tableName, label),
            }}));
            withIndex.sort((a, b) => {{
                const orderDelta = a.order - b.order;
                if (orderDelta !== 0) {{
                    return orderDelta;
                }}
                return a.index - b.index;
            }});
            return withIndex.map((item) => item.label);
        }}

        function ensureCanonicalPresetCoverage(tableName, labels) {{
            const baseLabels = Array.isArray(baseAnnotationPresetGroups[tableName]) ? baseAnnotationPresetGroups[tableName] : [];
            if (!baseLabels.length) {{
                return labels;
            }}
            const canonicalSet = new Set();
            const out = Array.isArray(labels) ? labels.slice() : [];
            out.forEach((label) => {{
                const canonical = resolveLabelForParsing(label);
                if (!canonical) {{
                    return;
                }}
                const canonicalKey = normalizeLabelText(canonical);
                if (canonicalKey) {{
                    canonicalSet.add(canonicalKey);
                }}
            }});
            baseLabels.forEach((baseLabel) => {{
                const baseKey = normalizeLabelText(baseLabel);
                if (!baseKey || canonicalSet.has(baseKey)) {{
                    return;
                }}
                out.push(baseLabel);
                canonicalSet.add(baseKey);
            }});
            return out;
        }}

        function normalizeAndRepairLabelRenameMap(rawMap) {{
            const sourceMap = rawMap && typeof rawMap === 'object' && !Array.isArray(rawMap)
                ? Object.assign({{}}, rawMap)
                : {{}};
            let changed = false;
            Object.keys(sourceMap).forEach((sourceKey) => {{
                const sourceLabel = baseAnnotationPresetLabelLookup[sourceKey];
                if (!sourceLabel) {{
                    return;
                }}
                const resolved = resolveRenamedLabel(sourceLabel);
                const targetKey = normalizeLabelText(resolved);
                if (!targetKey) {{
                    if (sourceMap[sourceKey]) {{
                        delete sourceMap[sourceKey];
                        changed = true;
                    }}
                    return;
                }}
                if (isCanonicalPresetLabel(resolved) && targetKey !== sourceKey) {{
                    delete sourceMap[sourceKey];
                    changed = true;
                }} else if (normalizeLabelText(sourceMap[sourceKey] || '') !== normalizeLabelText(resolved)) {{
                    sourceMap[sourceKey] = resolved;
                    changed = true;
                }}
            }});
            if (changed) {{
                saveLabelRenameMap(sourceMap);
            }}
            return sourceMap;
        }}

        function safeParseJson(raw) {{
            try {{
                return JSON.parse(raw || '{{}}');
            }} catch (err) {{
                return {{}};
            }}
        }}

        function loadLabelRenameMap() {{
            const out = Object.create(null);
            try {{
                const raw = parent.defaultView.localStorage.getItem(labelRenameMapStorageKey);
                const parsed = safeParseJson(raw);
                if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {{
                    return out;
                }}
                Object.keys(parsed).forEach((sourceLabel) => {{
                    const target = String(parsed[sourceLabel] || '').trim();
                    const key = normalizeLabelText(sourceLabel);
                    if (key && target) {{
                        out[key] = target;
                    }}
                }});
            }} catch (err) {{
                return out;
            }}
            return out;
        }}

        function saveLabelRenameMap(nextMap) {{
            try {{
                parent.defaultView.localStorage.setItem(labelRenameMapStorageKey, JSON.stringify(nextMap || {{}}));
            }} catch (err) {{
                // localStorage may be blocked
            }}
        }}

        function resolveRenamedLabel(rawLabel) {{
            const target = String(rawLabel || '').trim();
            if (!target) {{
                return target;
            }}
            let key = normalizeLabelText(target);
            const seen = new Set();
            let current = target;
            while (labelRenameMap[key]) {{
                const next = String(labelRenameMap[key] || '').trim();
                const nextKey = normalizeLabelText(next);
                if (!next || seen.has(nextKey)) {{
                    break;
                }}
                seen.add(key);
                key = nextKey;
                current = next;
            }}
            return current;
        }}

        function setLabelRename(fromLabel, toLabel) {{
            const fromKey = normalizeLabelText(fromLabel);
            const nextLabel = String(toLabel || '').trim();
            const nextKey = normalizeLabelText(nextLabel);
            if (!fromKey || !nextLabel || fromKey === nextKey) {{
                return;
            }}
            if (isCanonicalPresetLabel(nextLabel) && nextKey !== fromKey) {{
                showCopyBanner('🏷', 'Cannot rename to existing standard label: ' + nextLabel);
                return;
            }}
            const mapCopy = Object.assign({{}}, labelRenameMap);
            mapCopy[fromKey] = nextLabel;
            Object.keys(mapCopy).forEach((sourceKey) => {{
                if (normalizeLabelText(mapCopy[sourceKey]) === fromKey) {{
                    mapCopy[sourceKey] = nextLabel;
                }}
            }});
            labelRenameMap = mapCopy;
            saveLabelRenameMap(labelRenameMap);
        }}

        function applyRenameMapToLabel(rawLabel) {{
            const resolved = resolveRenamedLabel(rawLabel);
            return resolved || String(rawLabel || '').trim();
        }}

        function applyRenameMapToLabelList(labels) {{
            if (!Array.isArray(labels) || !labels.length) {{
                return [];
            }}
            const out = [];
            const seen = new Set();
            labels.forEach((label) => {{
                const next = applyRenameMapToLabel(label);
                if (!next || seen.has(normalizeLabelText(next))) {{
                    return;
                }}
                seen.add(normalizeLabelText(next));
                out.push(next);
            }});
            return out;
        }}

        function applyRenameMapToPresetGroups() {{
            const nextGroups = Object.create(null);
            Object.keys(baseAnnotationPresetGroups).forEach((tableName) => {{
                const labels = Array.isArray(baseAnnotationPresetGroups[tableName]) ? baseAnnotationPresetGroups[tableName] : [];
                const renamed = labels.map((label) => applyRenameMapToLabel(label)).filter((value) => value);
                const dedup = [];
                renamed.forEach((value) => {{
                    if (dedup.indexOf(value) >= 0) {{
                        return;
                    }}
                    dedup.push(value);
                }});
                const restored = ensureCanonicalPresetCoverage(tableName, dedup);
                nextGroups[tableName] = sortPresetLabelsByCanonicalOrder(tableName, restored);
            }});
            annotationPresetGroups = nextGroups;
            tableFourQuickLabels = (annotationPresetGroups['Table 4'] || []).slice(0, 4);
        }}

        function getSelectedPathsInOrder() {{
            return Array.from(parent.querySelectorAll('.img-container'))
                .map((container) => container.dataset.path)
                .filter((path) => path && selectedPaths.has(path));
        }}

        function decodeLabelData(raw) {{
            if (!raw) {{
                return '';
            }}
            try {{
                return decodeURIComponent(raw);
            }} catch (err) {{
                return String(raw);
            }}
        }}

        function isLabelSetMatched(labelA, labelB) {{
            return normalizeLabelText(labelA) === normalizeLabelText(labelB);
        }}

        function getOrderedLabelPaths(label) {{
            const target = normalizeLabelText(label);
            const out = [];
            if (!target) {{
                return out;
            }}
            parent.querySelectorAll('.img-container').forEach((container) => {{
                const path = container && container.dataset ? container.dataset.path : '';
                const labels = getAuthoritativeLabelsForPath(
                    path,
                    container && container.dataset ? container.dataset.labels : '[]'
                );
                if (!path) {{
                    return;
                }}
                const hasMatch = labels.some((item) => isLabelSetMatched(item, target));
                if (hasMatch) {{
                    out.push(path);
                }}
            }});
            return out;
        }}

        function clearLabelInspectionHighlights() {{
            parent.querySelectorAll('.img-container.label-inspection-highlight').forEach((container) => {{
                container.classList.remove('label-inspection-highlight');
            }});
        }}

        function highlightLabelMatches(label) {{
            clearLabelInspectionHighlights();
            const paths = getOrderedLabelPaths(label);
            paths.forEach((path) => {{
                const container = getContainerByPath(path);
                if (container) {{
                    container.classList.add('label-inspection-highlight');
                }}
            }});
        }}

        function syncSelectionStateWithDom() {{
            if (!parent) {{
                return;
            }}
            const visibleSelected = new Set();
            parent.querySelectorAll('.img-container.selected').forEach((container) => {{
                const path = container && container.dataset ? container.dataset.path : '';
                if (path) {{
                    visibleSelected.add(path);
                }}
            }});
            visibleSelected.forEach((path) => {{
                if (path) {{
                    selectedPaths.add(path);
                }}
            }});
            Array.from(selectedPaths).forEach((path) => {{
                if (!path || !visibleSelected.has(path)) {{
                    selectedPaths.delete(path);
                }}
            }});
        }}

        function getImageItemForPath(path, fallbackIndex = null, leadingLabels = []) {{
            const normalizedPath = String(path || '').trim();
            if (!normalizedPath) {{
                return null;
            }}
            const container = getContainerByPath(normalizedPath);
            const allContainers = Array.from(parent.querySelectorAll('.img-container'));
            const index = Number.isInteger(fallbackIndex)
                ? fallbackIndex
                : (container ? allContainers.indexOf(container) : -1);
            const labels = getAuthoritativeLabelsForPath(
                normalizedPath,
                container && container.dataset ? container.dataset.labels : '[]'
            );
            const combinedLabels = [];
            (Array.isArray(leadingLabels) ? leadingLabels : []).forEach((label) => {{
                const value = String(label || '').trim();
                if (value && combinedLabels.indexOf(value) < 0) {{
                    combinedLabels.push(value);
                }}
            }});
            labels.forEach((label) => {{
                if (combinedLabels.indexOf(label) < 0) {{
                    combinedLabels.push(label);
                }}
            }});
            return {{
                path: normalizedPath,
                index,
                filename: normalizedPath.split('/').pop(),
                labels: combinedLabels,
            }};
        }}

        function getGlobalOverlapItems() {{
            const allContainers = Array.from(parent.querySelectorAll('.img-container'));
            const itemsByPath = new Map();
            const uniqueLabelBuckets = new Map();
            const addIssue = (path, index, reason) => {{
                const item = getImageItemForPath(path, index, [reason]);
                if (!item) {{
                    return;
                }}
                if (!itemsByPath.has(item.path)) {{
                    itemsByPath.set(item.path, item);
                    return;
                }}
                const existing = itemsByPath.get(item.path);
                item.labels.forEach((label) => {{
                    if (existing.labels.indexOf(label) < 0) {{
                        existing.labels.push(label);
                    }}
                }});
            }};

            allContainers.forEach((container, index) => {{
                const path = container && container.dataset ? String(container.dataset.path || '').trim() : '';
                if (!path) {{
                    return;
                }}
                const labels = Object.prototype.hasOwnProperty.call(clientAnnotationLabelsByPath || {{}}, path)
                    ? normalizeLabelArray(clientAnnotationLabelsByPath[path] || [])
                    : [];
                if (labels.length > 1) {{
                    addIssue(path, index, 'Overlapping labels on image');
                }}
                labels.forEach((label) => {{
                    if (!isUniqueExactTableLabel(label)) {{
                        return;
                    }}
                    const key = normalizeLabelText(label);
                    if (!key) {{
                        return;
                    }}
                    if (!uniqueLabelBuckets.has(key)) {{
                        uniqueLabelBuckets.set(key, {{ label, entries: [] }});
                    }}
                    uniqueLabelBuckets.get(key).entries.push({{ path, index }});
                }});
            }});

            uniqueLabelBuckets.forEach((bucket) => {{
                if (!bucket || !Array.isArray(bucket.entries) || bucket.entries.length <= 1) {{
                    return;
                }}
                const reason = 'Duplicate unique label: ' + bucket.label;
                bucket.entries.forEach((entry) => addIssue(entry.path, entry.index, reason));
            }});

            return Array.from(itemsByPath.values()).sort((a, b) => (a.index || 0) - (b.index || 0));
        }}

        function getGlobalOverlapSummary() {{
            const items = getGlobalOverlapItems();
            return {{
                active: items.length > 0,
                count: items.length,
            }};
        }}

        function highlightGlobalOverlapImages() {{
            clearLabelInspectionHighlights();
            getGlobalOverlapItems().forEach((item) => {{
                const container = getContainerByPath(item.path);
                if (container) {{
                    container.classList.add('label-inspection-highlight');
                }}
            }});
        }}

        function getOrderedSelectionItems() {{
            const allContainers = Array.from(parent.querySelectorAll('.img-container'));
            const items = [];
            for (let i = 0; i < allContainers.length; i++) {{
                const container = allContainers[i];
                const path = container.dataset.path;
                if (!path || !selectedPaths.has(path)) {{
                    continue;
                }}
                const labels = normalizeLabelArray(container.dataset.labels || '[]');
                items.push({{
                    path,
                    index: i,
                    filename: path.split('/').pop(),
                    labels,
                }});
            }}
            return items;
        }}

        function isSelectionDebugPanelVisible() {{
            return selectionDebugPanelVisible;
        }}

        function setSelectionDebugPanelVisible(next) {{
            selectionDebugPanelVisible = Boolean(next);
            if (!selectionDebugPanelVisible) {{
                clearLabelInspectionHighlights();
            }}
            renderSelectionDebugPanel();
        }}

        function hideSelectionDebugPanel() {{
            clearLabelInspectionHighlights();
            setSelectionDebugPanelVisible(false);
        }}

        function setSelectionDebugPanelMode(mode, label) {{
            selectionDebugPanelMode = (mode === 'label' && label)
                ? 'label'
                : (mode === 'overlap' ? 'overlap' : (mode === 'missing-slots' ? 'missing-slots' : 'selection'));
            selectionDebugPanelLabel = String(label || '');
        }}

        function openSelectionDebugPanel() {{
            setSelectionDebugPanelMode('selection');
            setSelectionDebugPanelVisible(true);
        }}

        function openLabelMatchPanel(label) {{
            const normalized = String(label || '').trim();
            if (!normalized) {{
                return;
            }}
            setSelectionDebugPanelMode('label', normalized);
            setSelectionDebugPanelVisible(true);
        }}

        function openGlobalOverlapPanel() {{
            setSelectionDebugPanelMode('overlap');
            setSelectionDebugPanelVisible(true);
        }}

        function getAuthoritativeLabelsForPath(path, fallbackRaw) {{
            const normalizedPath = String(path || '').trim();
            if (normalizedPath && Object.prototype.hasOwnProperty.call(clientAnnotationLabelsByPath || {{}}, normalizedPath)) {{
                return normalizeLabelArray(clientAnnotationLabelsByPath[normalizedPath] || []);
            }}
            return normalizeLabelArray(fallbackRaw || []);
        }}

        function getUsageButtonRawLabel(button) {{
            if (!button) {{
                return '';
            }}
            const raw = decodeLabelData(button.getAttribute('data-preset-usage') || '');
            if (raw) {{
                return raw;
            }}
            const parentBtn = button.closest ? button.closest('.annotation-preset') : null;
            return parentBtn && parentBtn.dataset ? String(parentBtn.dataset.preset || '').trim() : '';
        }}

        function stopUsageBadgeEvent(e) {{
            if (!e) {{
                return;
            }}
            e.preventDefault();
            e.stopPropagation();
            if (e.stopImmediatePropagation) {{
                e.stopImmediatePropagation();
            }}
        }}

        function bindAnnotationPresetUsageButtons(scope) {{
            const root = scope && scope.querySelectorAll ? scope : parent;
            root.querySelectorAll('.annotation-preset-usage').forEach((button) => {{
                if (!button || button.dataset.usageInspectBound === '1') {{
                    return;
                }}
                button.dataset.usageInspectBound = '1';
                button.addEventListener('dblclick', function(e) {{
                    const rawLabel = getUsageButtonRawLabel(button);
                    if (!rawLabel) {{
                        stopUsageBadgeEvent(e);
                        return;
                    }}
                    if (!isLabelRenameShortcut(e)) {{
                        stopUsageBadgeEvent(e);
                        return;
                    }}
                    stopUsageBadgeEvent(e);
                    startRenameFromPresetLabel(rawLabel);
                }});
                button.addEventListener('mouseenter', function() {{
                    const rawLabel = getUsageButtonRawLabel(button);
                    if (rawLabel) {{
                        highlightLabelMatches(rawLabel);
                    }}
                }});
                button.addEventListener('mouseleave', function() {{
                    clearLabelInspectionHighlights();
                }});
                button.addEventListener('focus', function() {{
                    const rawLabel = getUsageButtonRawLabel(button);
                    if (rawLabel) {{
                        highlightLabelMatches(rawLabel);
                    }}
                }});
                button.addEventListener('blur', function() {{
                    clearLabelInspectionHighlights();
                }});
                button.addEventListener('mousedown', function(e) {{
                    stopUsageBadgeEvent(e);
                }});
                button.addEventListener('click', function(e) {{
                    const rawLabel = getUsageButtonRawLabel(button);
                    if (!rawLabel) {{
                        stopUsageBadgeEvent(e);
                        return;
                    }}
                    stopUsageBadgeEvent(e);
                    if (e && isLabelRenameShortcut(e)) {{
                        return;
                    }}
                    if (e && e.detail > 1) {{
                        return;
                    }}
                    openLabelMatchPanel(rawLabel);
                }});
                button.addEventListener('keydown', function(e) {{
                    if (e.key === 'Enter' || e.key === ' ') {{
                        const rawLabel = getUsageButtonRawLabel(button);
                        stopUsageBadgeEvent(e);
                        if (rawLabel) {{
                            openLabelMatchPanel(rawLabel);
                        }}
                    }}
                }});
            }});
        }}

        function getOverlapButtonRawLabel(button) {{
            if (!button) {{
                return '';
            }}
            const raw = decodeLabelData(button.getAttribute('data-preset-overlap') || '');
            if (raw) {{
                return raw;
            }}
            const parentBtn = button.closest ? button.closest('.annotation-preset') : null;
            return parentBtn && parentBtn.dataset ? String(parentBtn.dataset.preset || '').trim() : '';
        }}

        function bindAnnotationPresetOverlapButtons(scope) {{
            const root = scope && scope.querySelectorAll ? scope : parent;
            root.querySelectorAll('.annotation-preset-overlap').forEach((button) => {{
                if (!button || button.dataset.overlapInspectBound === '1') {{
                    return;
                }}
                button.dataset.overlapInspectBound = '1';
                button.addEventListener('mouseenter', function() {{
                    const rawLabel = getOverlapButtonRawLabel(button);
                    if (rawLabel) {{
                        highlightLabelMatches(rawLabel);
                    }}
                }});
                button.addEventListener('mouseleave', function() {{
                    clearLabelInspectionHighlights();
                }});
                button.addEventListener('focus', function() {{
                    const rawLabel = getOverlapButtonRawLabel(button);
                    if (rawLabel) {{
                        highlightLabelMatches(rawLabel);
                    }}
                }});
                button.addEventListener('blur', function() {{
                    clearLabelInspectionHighlights();
                }});
                button.addEventListener('mousedown', function(e) {{
                    stopUsageBadgeEvent(e);
                }});
                button.addEventListener('click', function(e) {{
                    const rawLabel = getOverlapButtonRawLabel(button);
                    stopUsageBadgeEvent(e);
                    if (rawLabel) {{
                        openLabelMatchPanel(rawLabel);
                    }}
                }});
                button.addEventListener('dblclick', function(e) {{
                    stopUsageBadgeEvent(e);
                }});
                button.addEventListener('keydown', function(e) {{
                    if (e.key === 'Enter' || e.key === ' ') {{
                        const rawLabel = getOverlapButtonRawLabel(button);
                        stopUsageBadgeEvent(e);
                        if (rawLabel) {{
                            openLabelMatchPanel(rawLabel);
                        }}
                    }}
                }});
            }});
        }}

        function promptRenameLabel(oldLabel) {{
            const current = String(oldLabel || '').trim();
            if (!current) {{
                return null;
            }}
            const promptValue = parent.defaultView.prompt('Rename label', current);
            if (promptValue === null) {{
                return null;
            }}
            return String(promptValue).trim();
        }}

        function isLabelRenameShortcut(event) {{
            return !!(event && (event.shiftKey || event.ctrlKey || event.metaKey));
        }}

        async function renameLabelEverywhere(oldLabel, newLabel) {{
            if (!isActiveScriptInstance()) {{
                return;
            }}
            const source = String(oldLabel || '').trim();
            const target = String(newLabel || '').trim();
            if (!source || !target) {{
                showCopyBanner('🏷', 'Rename canceled');
                return;
            }}
            if (normalizeLabelText(source) === normalizeLabelText(target)) {{
                showCopyBanner('🏷', 'Label unchanged');
                return;
            }}
            const canonicalSource = getCanonicalPresetLabelForParsing(source) || source;
            setLabelRename(canonicalSource, target);
            if (normalizeLabelText(source) !== normalizeLabelText(canonicalSource)) {{
                setLabelRename(source, target);
            }}

            try {{
                const payload = new URLSearchParams({{
                    old_label: source,
                    canonical_label: canonicalSource,
                    new_label: target,
                }}).toString();
                const response = await fetch(clipboardServerBase + '/rename-label?' + payload);
                const data = await response.json().catch(() => ({{}}));
                if (!data || !data.success) {{
                    showCopyBanner('✗', 'Rename failed');
                    return;
                }}
                applyRenameMapToPresetGroups();
                rebuildAnnotationPresets();
                applyAuthoritativeAnnotationState(data);
                parent.querySelectorAll('.img-container').forEach((container) => {{
                    renderLabelBadges(container, clientAnnotationLabelsByPath[container.dataset.path || ''] || []);
                    syncQuickLabelActions(container);
                    bindQuickLabelButtonEvents(container, getContainerPath(container));
                }});
                if (isSelectionDebugPanelVisible()) {{
                    renderSelectionDebugPanel();
                }}
                updateSelectionCount();
                requestSelectionBarRefresh();
                const updatedCount = Number(data.updated_count || 0);
                showCopyBanner('🏷', 'Renamed ' + source + ' → ' + target + ' for ' + updatedCount + ' image(s)');
            }} catch (err) {{
                showCopyBanner('✗', 'Rename failed');
                console.error('Rename error:', err);
            }}
        }}

        let renameMapSyncStarted = false;
        async function persistRenameMapToAnnotationStore() {{
            if (!isActiveScriptInstance()) {{
                return;
            }}
            if (renameMapSyncStarted || !labelRenameMap || !Object.keys(labelRenameMap).length) {{
                return;
            }}
            renameMapSyncStarted = true;
            const ready = await ensureClipboardServerReady('rename_map_sync');
            if (!ready) {{
                return;
            }}
            let latestState = null;
            let changedCount = 0;
            const sourceKeys = Object.keys(labelRenameMap);
            for (let i = 0; i < sourceKeys.length; i++) {{
                const sourceKey = sourceKeys[i];
                const target = String(labelRenameMap[sourceKey] || '').trim();
                if (!sourceKey || !target) {{
                    continue;
                }}
                const canonicalSource = baseAnnotationPresetLabelLookup[sourceKey] || sourceKey;
                try {{
                    const payload = new URLSearchParams({{
                        old_label: sourceKey,
                        canonical_label: canonicalSource,
                        new_label: target,
                    }}).toString();
                    const response = await fetch(clipboardServerBase + '/rename-label?' + payload);
                    const data = await response.json().catch(() => ({{}}));
                    if (data && data.success) {{
                        changedCount += Number(data.updated_count || 0);
                        latestState = data;
                    }}
                }} catch (err) {{
                    logClientEvent('rename_map_sync_error', {{
                        source: sourceKey,
                        target,
                        error: err && err.message ? err.message : String(err),
                    }});
                }}
            }}
            if (latestState) {{
                applyAuthoritativeAnnotationState(latestState);
                parent.querySelectorAll('.img-container').forEach((container) => {{
                    renderLabelBadges(container, clientAnnotationLabelsByPath[container.dataset.path || ''] || []);
                }});
                rebuildClientAnnotationLabelCounts();
                requestSelectionBarRefresh();
            }}
            if (changedCount > 0) {{
                logClientEvent('rename_map_synced_to_annotations', {{
                    changed: changedCount,
                    sources: sourceKeys.length,
                }});
            }}
        }}

        function startRenameFromPresetLabel(rawLabel) {{
            const label = String(rawLabel || '').trim();
            if (!label) return;
            const next = promptRenameLabel(label);
            if (next === null) {{
                return;
            }}
            if (!next) {{
                const canonical = getCanonicalPresetLabelForParsing(label);
                if (canonical) {{
                    const target = String(canonical).trim();
                    if (normalizeLabelText(target) && normalizeLabelText(target) !== normalizeLabelText(label)) {{
                        renameLabelEverywhere(label, target);
                        return;
                    }}
                }}
                showCopyBanner('🏷', 'No change');
                return;
            }}
            if (normalizeLabelText(label) === normalizeLabelText(next)) {{
                showCopyBanner('🏷', 'No change');
                return;
            }}
            renameLabelEverywhere(label, next);
        }}

        function requestSelectionBarRefresh() {{
            if (barApplyAnnotation) {{
                // Rebuild the bar content only when selection exists; otherwise no-op
                updateSelectionCount();
            }} else {{
                const bar = parent.querySelector('.selection-bar.visible');
                if (bar) {{
                    updateSelectionCount();
                }}
            }}
        }}

        function flashImageJump(container, durationMs = 1000) {{
            if (!container || !container.classList) {{
                return;
            }}
            if (imageJumpFlashTimers.has(container)) {{
                const oldTimer = imageJumpFlashTimers.get(container);
                clearTimeout(oldTimer);
                imageJumpFlashTimers.delete(container);
            }}
            container.classList.remove('image-jump-flash');
            container.offsetHeight;
            container.classList.add('image-jump-flash');
            const timer = window.setTimeout(() => {{
                if (container && container.classList) {{
                    container.classList.remove('image-jump-flash');
                }}
                imageJumpFlashTimers.delete(container);
            }}, durationMs);
            imageJumpFlashTimers.set(container, timer);
        }}

        function jumpToContainerByPath(path, silent) {{
            const container = getContainerByPath(path);
            if (!container) {{
                if (!silent) {{
                    showCopyBanner('✗', 'Cannot locate image: ' + String(path || '').split('/').pop());
                }}
                return;
            }}
            const containers = Array.from(parent.querySelectorAll('.img-container'));
            const targetIndex = containers.indexOf(container);
            if (targetIndex >= 0) {{
                setFocus(targetIndex);
            }} else {{
                container.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
            }}
            flashImageJump(container);
        }}

        function updateSelectionDebugPanelPosition(panel) {{
            if (!panel) {{
                return;
            }}
            if (panel.parentElement && panel.parentElement.classList && panel.parentElement.classList.contains('selection-bar')) {{
                panel.style.removeProperty('top');
                panel.style.removeProperty('left');
                return;
            }}
            const visibleBar = parent.querySelector('.selection-bar.visible');
            let bottomOffset = 16;
            if (visibleBar) {{
                const barRect = visibleBar.getBoundingClientRect();
                const gap = 12;
                const candidate = Math.max(16, Math.ceil((window.innerHeight - barRect.top) + gap));
                if (Number.isFinite(candidate) && candidate > 0) {{
                    bottomOffset = candidate;
                }}
            }}
            panel.style.setProperty('bottom', bottomOffset + 'px');
        }}

        function renderSelectionDebugPanel() {{
            let panel = parent.getElementById('selection-debug-panel');
            const panelHost = parent.querySelector('.selection-bar.visible') || parent.querySelector('.selection-bar') || parent.body;
            if (!panel) {{
                panel = parent.createElement('div');
                panel.id = 'selection-debug-panel';
                panel.className = 'selection-debug-panel';
            }}
            if (panel.parentNode !== panelHost) {{
                panelHost.appendChild(panel);
            }}

            if (!isSelectionDebugPanelVisible()) {{
                panel.classList.remove('visible');
                if (panelHost.classList && panelHost.classList.contains('selection-bar')) {{
                    panelHost.classList.remove('has-inspection-panel');
                }}
                return;
            }}

            const mode = selectionDebugPanelMode === 'label'
                ? 'label'
                : (selectionDebugPanelMode === 'overlap' ? 'overlap' : (selectionDebugPanelMode === 'missing-slots' ? 'missing-slots' : 'selection'));
            const labelForMode = String(selectionDebugPanelLabel || '').trim();
            if (mode === 'missing-slots') {{
                const missingItems = getMissingSlotItems();
                const rows = missingItems.map((item) => {{
                    const idxText = item.index >= 0 ? String(item.index + 1) : '—';
                    const safeLabel = escapeHtml(item.label);
                    const labelAttr = escapeHtmlAttribute(item.label);
                    const slotKeyAttr = escapeHtmlAttribute(item.slotKey);
                    const stateClass = item.empty ? 'empty' : 'missing';
                    const stateText = item.empty ? 'empty (-)' : 'missing';
                    const nextEmpty = item.empty ? '0' : '1';
                    const actionText = item.empty ? 'Clear empty' : 'Mark empty (-)';
                    return (
                        '<div class="selection-debug-item missing-slot-item">' +
                            '<span class="selection-debug-index">#' + idxText + '</span>' +
                            '<span class="missing-slot-main">' +
                                '<span class="missing-slot-label" title="' + labelAttr + '">' + safeLabel + '</span>' +
                                '<span class="missing-slot-state ' + stateClass + '">' + stateText + '</span>' +
                            '</span>' +
                            '<button type="button" class="missing-slot-toggle" data-slot-key="' + slotKeyAttr + '" data-slot-label="' + labelAttr + '" data-empty-next="' + nextEmpty + '">' + actionText + '</button>' +
                        '</div>'
                    );
                }}).join('');
                const listHtml = rows || '<div class="selection-debug-empty">No missing or empty slots</div>';
                if (panelHost.classList && panelHost.classList.contains('selection-bar')) {{
                    panelHost.classList.add('has-inspection-panel');
                }}
                panel.classList.add('visible');
                panel.innerHTML =
                    '<div class="selection-debug-panel-header">' +
                        '<span>Missing / Empty Slots (' + missingItems.length + ')</span>' +
                        '<button type="button" class="selection-debug-panel-close">Close</button>' +
                    '</div>' +
                    '<div class="selection-debug-panel-body">' + listHtml + '</div>';
                updateSelectionDebugPanelPosition(panel);

                const closeBtn = panel.querySelector('.selection-debug-panel-close');
                if (closeBtn) {{
                    closeBtn.addEventListener('click', function() {{
                        hideSelectionDebugPanel();
                    }});
                }}
                panel.querySelectorAll('.missing-slot-toggle').forEach((button) => {{
                    button.addEventListener('click', function(event) {{
                        event.preventDefault();
                        event.stopPropagation();
                        const slotKey = button.getAttribute('data-slot-key') || '';
                        const label = button.getAttribute('data-slot-label') || '';
                        const nextEmpty = button.getAttribute('data-empty-next') === '1';
                        setRequiredSlotEmpty(slotKey, label, nextEmpty);
                    }});
                }});
                return;
            }}
            const items = mode === 'label'
                ? getOrderedLabelPaths(labelForMode).map((path) => getImageItemForPath(path)).filter(Boolean)
                : (mode === 'overlap' ? getGlobalOverlapItems() : getOrderedSelectionItems());
            const panelHeaderLabel = mode === 'label'
                ? ('Images for ' + escapeHtml(labelForMode))
                : (mode === 'overlap' ? 'Overlapping Labels' : 'Selected Images');
            panel.classList.add('visible');
            const rows = items.map((item) => {{
                const idxText = item.index >= 0 ? String(item.index + 1) : '—';
                const labelText = item.labels.length ? item.labels.join(' | ') : 'unlabeled';
                const pathAttr = escapeHtmlAttribute(item.path);
                const safeFile = escapeHtmlAttribute(item.filename);
                const safeLabels = escapeHtmlAttribute(labelText);
                return (
                        '<div class=\"selection-debug-item\" data-jump-path=\"' + pathAttr + '\">' +
                            '<span class=\"selection-debug-index\">#' + idxText + '</span>' +
                        '<span class=\"selection-debug-filename\" title=\"' + escapeHtmlAttribute(item.path) + '\">' + safeFile + '</span>' +
                        '<span class=\"selection-debug-labels\" title=\"' + safeLabels + '\">' + escapeHtml(labelText) + '</span>' +
                        '<button type=\"button\" class=\"selection-debug-jump\" data-jump-path=\"' + pathAttr + '\">Go</button>' +
                    '</div>'
                );
            }}).join('');
            const emptyText = mode === 'label'
                ? '<div class=\"selection-debug-empty\">No images for this label</div>'
                : '<div class=\"selection-debug-empty\">No selected images</div>';
            const listHtml = rows || emptyText;
            if (panelHost.classList && panelHost.classList.contains('selection-bar')) {{
                panelHost.classList.add('has-inspection-panel');
            }}
            panel.innerHTML =
                '<div class=\"selection-debug-panel-header\">' +
                    '<span>' + panelHeaderLabel + ' (' + items.length + ')</span>' +
                    '<button type=\"button\" class=\"selection-debug-panel-close\">Close</button>' +
                '</div>' +
                '<div class=\"selection-debug-panel-body\">' + listHtml + '</div>';
            updateSelectionDebugPanelPosition(panel);

            const closeBtn = panel.querySelector('.selection-debug-panel-close');
            if (closeBtn) {{
                closeBtn.addEventListener('click', function() {{
                    hideSelectionDebugPanel();
                }});
            }}

            panel.querySelectorAll('.selection-debug-jump').forEach((button) => {{
                button.addEventListener('click', function() {{
                    const path = button.getAttribute('data-jump-path') || '';
                    if (!path) return;
                    jumpToContainerByPath(path);
                }});
            }});
            panel.querySelectorAll('.selection-debug-item').forEach((item) => {{
                if (item.classList.contains('selection-debug-empty')) {{
                    return;
                }}
                item.addEventListener('click', function(e) {{
                    if (e.target && e.target.classList && e.target.classList.contains('selection-debug-jump')) {{
                        return;
                    }}
                    const path = item.getAttribute('data-jump-path') || '';
                    jumpToContainerByPath(path);
                }});
            }});
        }}

        function scheduleSelectionDebugPanelRefresh() {{
            if (!isSelectionDebugPanelVisible() || selectionDebugRenderFrame) {{
                return;
            }}
            selectionDebugRenderFrame = parent.defaultView.requestAnimationFrame(() => {{
                selectionDebugRenderFrame = null;
                renderSelectionDebugPanel();
            }});
        }}

        function getOrderedSelectionLabels() {{
            const paths = getSelectedPathsInOrder();
            return paths.map((path) => {{
                const container = getContainerByPath(path);
                if (!container) return '';
                return normalizeSingleLabelArray(container.dataset.labels || '[]')[0] || '';
            }});
        }}

        function countImagesWithLabel(label) {{
            const authoritative = getOrderedLabelPaths(label);
            if (authoritative.length > 0 || (clientAnnotationLabelsByPath && Object.keys(clientAnnotationLabelsByPath || {{}}).length)) {{
                return authoritative.length;
            }}
            const target = normalizeLabelText(label);
            if (!target) {{
                return 0;
            }}
            const directCount = clientAnnotationLabelCounts[label];
            if (Number.isInteger(directCount)) {{
                return directCount;
            }}
            const matchedKey = Object.keys(clientAnnotationLabelCounts)
                .find((key) => normalizeLabelText(key) === target);
            return matchedKey ? Number(clientAnnotationLabelCounts[matchedKey] || 0) : 0;
        }}

        function labelMatchesCanonical(rawLabel, canonicalLabel) {{
            const canonicalKey = normalizeLabelText(canonicalLabel);
            if (!canonicalKey) {{
                return false;
            }}
            const rawKey = normalizeLabelText(rawLabel);
            if (rawKey === canonicalKey) {{
                return true;
            }}
            return normalizeLabelText(resolveLabelForParsing(rawLabel)) === canonicalKey;
        }}

        function getDisplayLabelForCanonical(canonicalLabel) {{
            const canonicalKey = normalizeLabelText(canonicalLabel);
            if (!canonicalKey) {{
                return '';
            }}
            const presetMatch = (Array.isArray(annotationFlatPresets) ? annotationFlatPresets : []).find((label) => {{
                return normalizeLabelText(label) === canonicalKey
                    || normalizeLabelText(resolveLabelForParsing(label)) === canonicalKey;
            }});
            return presetMatch || applyRenameMapToLabel(canonicalLabel) || canonicalLabel;
        }}

        function getContainerLabels(container) {{
            if (!container || !container.dataset) {{
                return [];
            }}
            return getAuthoritativeLabelsForPath(
                container.dataset.path || '',
                container.dataset.labels || '[]'
            );
        }}

        function containerHasCanonicalLabel(container, canonicalLabel) {{
            return getContainerLabels(container).some((label) => labelMatchesCanonical(label, canonicalLabel));
        }}

        function getCanonicalLabelOnContainer(container, canonicalLabel) {{
            return getContainerLabels(container).find((label) => labelMatchesCanonical(label, canonicalLabel)) || '';
        }}

        function countImagesWithCanonicalLabel(canonicalLabel) {{
            const paths = [];
            parent.querySelectorAll('.img-container').forEach((container) => {{
                const path = container && container.dataset ? String(container.dataset.path || '') : '';
                if (!path) {{
                    return;
                }}
                if (containerHasCanonicalLabel(container, canonicalLabel) && paths.indexOf(path) < 0) {{
                    paths.push(path);
                }}
            }});
            return paths.length;
        }}

        function labelsAreSame(labelA, labelB) {{
            const a = normalizeLabelArray(labelA || []);
            const b = normalizeLabelArray(labelB || []);
            if (a.length !== b.length) {{
                return false;
            }}
            for (let i = 0; i < a.length; i++) {{
                if (normalizeLabelText(a[i]) !== normalizeLabelText(b[i])) {{
                    return false;
                }}
            }}
            return true;
        }}

        function captureAnnotationSnapshot(paths) {{
            const out = [];
            const seen = new Set();
            (Array.isArray(paths) ? paths : []).forEach((path) => {{
                const normalizedPath = String(path || '').trim();
                if (!normalizedPath || seen.has(normalizedPath)) {{
                    return;
                }}
                seen.add(normalizedPath);
                out.push({{
                    path: normalizedPath,
                    labels: normalizeLabelArray(clientAnnotationLabelsByPath[normalizedPath] || []),
                }});
            }});
            return out;
        }}

        function annotationSnapshotChanged(beforeSnapshot, afterSnapshot) {{
            const before = Array.isArray(beforeSnapshot) ? beforeSnapshot : [];
            const after = Array.isArray(afterSnapshot) ? afterSnapshot : [];
            const keys = new Set();
            before.forEach((entry) => keys.add(entry.path));
            after.forEach((entry) => keys.add(entry.path));
            for (const path of keys) {{
                const beforeLabels = (before.find((entry) => entry.path === path) || {{ labels: [] }}).labels;
                const afterLabels = (after.find((entry) => entry.path === path) || {{ labels: [] }}).labels;
                if (!labelsAreSame(beforeLabels, afterLabels)) {{
                    return true;
                }}
            }}
            return false;
        }}

        function pushAnnotationHistory(beforeSnapshot, afterSnapshot, description) {{
            if (!annotationSnapshotChanged(beforeSnapshot, afterSnapshot)) {{
                return;
            }}
            annotationUndoStack.push({{
                before: beforeSnapshot,
                after: afterSnapshot,
                description: String(description || 'label change'),
            }});
            while (annotationUndoStack.length > annotationHistoryLimit) {{
                annotationUndoStack.shift();
            }}
            annotationRedoStack.length = 0;
        }}

        async function applyAnnotationSnapshot(snapshot) {{
            const entries = Array.isArray(snapshot) ? snapshot : [];
            const operations = entries.map((entry) => {{
                const labels = normalizeLabelArray(entry.labels || []);
                const query = new URLSearchParams({{
                    action: labels.length ? 'set' : 'clear',
                    paths: entry.path,
                    labels: labels.join('|'),
                }}).toString();
                return fetch(clipboardServerBase + '/annotations?' + query)
                    .then((response) => response.json())
                    .then((data) => ({{ entry, data }}));
            }});
            const results = await Promise.all(operations);
            let ok = true;
            results.forEach((result) => {{
                if (!result.data || !result.data.success) {{
                    ok = false;
                    return;
                }}
                const path = result.entry.path;
                const fallbackLabels = normalizeLabelArray(result.entry.labels || []);
                const nextLabels = result.data.annotations && Object.prototype.hasOwnProperty.call(result.data.annotations, path)
                    ? normalizeLabelArray(result.data.annotations[path])
                    : fallbackLabels;
                applyQuickLabelResponseToClient(result.data, path, nextLabels);
            }});
            updateSelectionCount('', true);
            scheduleSelectionDebugPanelRefresh();
            hydrateAutoNextLabelHints();
            return ok;
        }}

        async function undoAnnotationAction() {{
            const entry = annotationUndoStack.pop();
            if (!entry) {{
                showCopyBanner('↺', 'Nothing to undo');
                return;
            }}
            const ok = await applyAnnotationSnapshot(entry.before);
            if (ok) {{
                annotationRedoStack.push(entry);
                showCopyBanner('↺', 'Undo: ' + entry.description);
            }} else {{
                annotationUndoStack.push(entry);
                showCopyBanner('✗', 'Undo failed');
            }}
        }}

        async function redoAnnotationAction() {{
            const entry = annotationRedoStack.pop();
            if (!entry) {{
                showCopyBanner('↻', 'Nothing to redo');
                return;
            }}
            const ok = await applyAnnotationSnapshot(entry.after);
            if (ok) {{
                annotationUndoStack.push(entry);
                showCopyBanner('↻', 'Redo: ' + entry.description);
            }} else {{
                annotationRedoStack.push(entry);
                showCopyBanner('✗', 'Redo failed');
            }}
        }}

        function getIndexOfCanonicalLabel(canonicalLabel) {{
            const containers = Array.from(parent.querySelectorAll('.img-container'));
            for (let i = 0; i < containers.length; i++) {{
                if (containerHasCanonicalLabel(containers[i], canonicalLabel)) {{
                    return i;
                }}
            }}
            return -1;
        }}

        function getContiguousAutoBatchTargets(startIndex, count) {{
            const containers = Array.from(parent.querySelectorAll('.img-container'));
            const targets = [];
            if (startIndex < 0 || !Number.isFinite(count) || count <= 0) {{
                return targets;
            }}
            for (let i = 0; i < count; i++) {{
                const container = containers[startIndex + i];
                if (!container || getContainerLabels(container).length > 0) {{
                    return [];
                }}
                targets.push(container);
            }}
            return targets;
        }}

        function buildMgLabels(tableNumber, startMg, count, stationNumber) {{
            const labels = [];
            const boundedCount = Math.max(0, Math.min(Number(count) || 0, 7));
            for (let i = 0; i < boundedCount; i++) {{
                labels.push('Table ' + tableNumber + ' MG ' + (startMg + i) + ' Test Station ' + stationNumber);
            }}
            return labels;
        }}

        function buildRepeatedLabels(label, count) {{
            const labels = [];
            const boundedCount = Math.max(0, Math.min(Number(count) || 0, 5));
            for (let i = 0; i < boundedCount; i++) {{
                labels.push(label);
            }}
            return labels;
        }}

        function hasSelectedStationAnodeCounts() {{
            return Boolean(getStationAnodeCount('Test Station 1') && getStationAnodeCount('Test Station 2'));
        }}

        function getNextTableThreeBatchLabel() {{
            for (let row = 1; row <= 4; row++) {{
                const label = 'Table 3 Row ' + row;
                if (countImagesWithCanonicalLabel(label) <= 0) {{
                    return label;
                }}
            }}
            return '';
        }}

        function getTableThreeBatchCandidateForContainer(container) {{
            if (!hasSelectedStationAnodeCounts()) {{
                return null;
            }}
            if (!container || !container.dataset || getContainerLabels(container).length > 0) {{
                return null;
            }}
            const label = getNextTableThreeBatchLabel();
            if (!label) {{
                return null;
            }}
            const startIndex = getContainerIndex(container);
            const targets = getContiguousAutoBatchTargets(startIndex, 5);
            if (!targets.length) {{
                return null;
            }}
            const labels = buildRepeatedLabels(label, targets.length);
            return {{
                mode: 'batch-apply',
                milestone: 'Table 3 progressive',
                labels,
                targets,
                paths: targets.map((target) => target.dataset.path || '').filter((path) => path),
            }};
        }}

        function getAutoBatchCandidates() {{
            const stationOneCount = getStationAnodeCount('Test Station 1');
            const stationTwoCount = getStationAnodeCount('Test Station 2');
            if (!stationOneCount || !stationTwoCount) {{
                return [];
            }}
            const stationTwoStartMg = stationOneCount + 1;
            return [
                {{
                    milestone: 'Table 4 Row 3 Column A Test Station 1',
                    labels: buildMgLabels(5, 1, stationOneCount, 1),
                }},
                {{
                    milestone: 'Table 5 MG ' + stationOneCount + ' Test Station 1',
                    labels: buildMgLabels(6, 1, stationOneCount, 1),
                }},
                {{
                    milestone: 'Table 4 Row 3 Column B Test Station 2',
                    labels: buildMgLabels(5, stationTwoStartMg, stationTwoCount, 2),
                }},
                {{
                    milestone: 'Table 5 MG ' + Math.min(7, stationTwoStartMg + stationTwoCount - 1) + ' Test Station 2',
                    labels: buildMgLabels(6, stationTwoStartMg, stationTwoCount, 2),
                }},
            ];
        }}

        function getAutoBatchActionForContainer(container) {{
            if (!container || !container.dataset || getContainerLabels(container).length > 0) {{
                return null;
            }}
            const targetIndex = getContainerIndex(container);
            if (targetIndex < 0) {{
                return null;
            }}
            const candidates = getAutoBatchCandidates();
            for (let i = 0; i < candidates.length; i++) {{
                const candidate = candidates[i];
                const labels = Array.isArray(candidate.labels) ? candidate.labels : [];
                if (!labels.length || countImagesWithCanonicalLabel(candidate.milestone) <= 0) {{
                    continue;
                }}
                const alreadyFilled = labels.some((label) => countImagesWithCanonicalLabel(label) > 0);
                if (alreadyFilled) {{
                    continue;
                }}
                const milestoneIndex = getIndexOfCanonicalLabel(candidate.milestone);
                if (milestoneIndex < 0 || targetIndex !== milestoneIndex + 1) {{
                    continue;
                }}
                const targets = getContiguousAutoBatchTargets(targetIndex, labels.length);
                if (targets.length !== labels.length) {{
                    continue;
                }}
                return {{
                    mode: 'batch-apply',
                    milestone: candidate.milestone,
                    labels,
                    targets,
                    paths: targets.map((target) => target.dataset.path || '').filter((path) => path),
                }};
            }}
            return null;
        }}

        function getAutoBatchRemoveActionForContainer(container) {{
            if (!container || !container.dataset) {{
                return null;
            }}
            const targetIndex = getContainerIndex(container);
            if (targetIndex < 0) {{
                return null;
            }}
            const allContainers = Array.from(parent.querySelectorAll('.img-container'));
            const candidates = getAutoBatchCandidates();
            for (let i = 0; i < candidates.length; i++) {{
                const candidate = candidates[i];
                const labels = Array.isArray(candidate.labels) ? candidate.labels : [];
                if (!labels.length || countImagesWithCanonicalLabel(candidate.milestone) <= 0) {{
                    continue;
                }}
                const milestoneIndex = getIndexOfCanonicalLabel(candidate.milestone);
                if (milestoneIndex < 0 || targetIndex !== milestoneIndex + 1) {{
                    continue;
                }}
                const targets = [];
                const paths = [];
                const displayLabels = [];
                let matched = true;
                for (let j = 0; j < labels.length; j++) {{
                    const target = allContainers[targetIndex + j];
                    if (!target || !containerHasCanonicalLabel(target, labels[j])) {{
                        matched = false;
                        break;
                    }}
                    targets.push(target);
                    paths.push(target.dataset.path || '');
                    displayLabels.push(getCanonicalLabelOnContainer(target, labels[j]) || getDisplayLabelForCanonical(labels[j]));
                }}
                if (matched && paths.every((path) => path)) {{
                    return {{
                        mode: 'batch-remove',
                        milestone: candidate.milestone,
                        labels: displayLabels,
                        canonicalLabels: labels,
                        targets,
                        paths,
                    }};
                }}
            }}
            return null;
        }}

        function clearAutoBatchHighlights() {{
            parent.querySelectorAll('.img-container.auto-batch-label-target, .img-container.auto-batch-label-start').forEach((container) => {{
                container.classList.remove('auto-batch-label-target', 'auto-batch-label-start');
            }});
        }}

        function applyAutoBatchHighlights(action) {{
            clearAutoBatchHighlights();
            if (!action || !Array.isArray(action.targets)) {{
                return;
            }}
            action.targets.forEach((target) => {{
                target.classList.add('auto-batch-label-target');
            }});
        }}

        function getAutoNextTableFourSequences() {{
            const stationOneCount = getStationAnodeCount('Test Station 1');
            const stationTwoCount = getStationAnodeCount('Test Station 2');
            if (!stationOneCount || !stationTwoCount) {{
                return [];
            }}
            return [
                {{
                    milestone: 'Table 3 Row 4',
                    labels: [
                        'Table 4 Row 2 Column A Test Station 1',
                        'Table 4 Row 3 Column A Test Station 1',
                    ],
                }},
                {{
                    milestone: 'Table 6 MG ' + stationOneCount + ' Test Station 1',
                    labels: [
                        'Table 4 Row 2 Column B Test Station 2',
                        'Table 4 Row 3 Column B Test Station 2',
                    ],
                }},
            ];
        }}

        function getAutoNextApplyCandidate() {{
            const sequences = getAutoNextTableFourSequences();
            for (let i = 0; i < sequences.length; i++) {{
                const sequence = sequences[i];
                if (countImagesWithCanonicalLabel(sequence.milestone) <= 0) {{
                    continue;
                }}
                for (let j = 0; j < sequence.labels.length; j++) {{
                    const canonicalLabel = sequence.labels[j];
                    if (countImagesWithCanonicalLabel(canonicalLabel) <= 0) {{
                        return {{
                            mode: 'apply',
                            canonicalLabel,
                            label: getDisplayLabelForCanonical(canonicalLabel),
                        }};
                    }}
                }}
            }}
            return null;
        }}

        function getAutoNextRemoveActionForContainer(container) {{
            if (!container || !container.dataset) {{
                return null;
            }}
            const sequences = getAutoNextTableFourSequences();
            for (let i = 0; i < sequences.length; i++) {{
                const labels = Array.isArray(sequences[i].labels) ? sequences[i].labels : [];
                for (let j = 0; j < labels.length; j++) {{
                    const canonicalLabel = labels[j];
                    if (containerHasCanonicalLabel(container, canonicalLabel)) {{
                        return {{
                            mode: 'remove',
                            canonicalLabel,
                            label: getDisplayLabelForCanonical(canonicalLabel),
                            existingLabel: getCanonicalLabelOnContainer(container, canonicalLabel),
                        }};
                    }}
                }}
            }}
            return null;
        }}

        function getAutoNextActionForContainer(container) {{
            if (!container || !container.dataset) {{
                return null;
            }}
            const path = String(container.dataset.path || '');
            if (!path) {{
                return null;
            }}
            const removeAction = getAutoNextRemoveActionForContainer(container);
            if (removeAction) {{
                return removeAction;
            }}
            if (
                lastAutoNextClick.path === path
                && lastAutoNextClick.canonicalLabel
                && containerHasCanonicalLabel(container, lastAutoNextClick.canonicalLabel)
            ) {{
                return {{
                    mode: 'remove',
                    canonicalLabel: lastAutoNextClick.canonicalLabel,
                    label: getDisplayLabelForCanonical(lastAutoNextClick.canonicalLabel),
                    existingLabel: getCanonicalLabelOnContainer(container, lastAutoNextClick.canonicalLabel),
                }};
            }}
            if (getContainerLabels(container).length > 0) {{
                return null;
            }}
            return getAutoNextApplyCandidate();
        }}

        function renderAutoNextLabelHint(container) {{
            if (!container || !container.dataset) {{
                return;
            }}
            const tableThreeAction = getTableThreeBatchCandidateForContainer(container);
            const batchAction = tableThreeAction || getAutoBatchActionForContainer(container);
            const batchRemoveAction = getAutoBatchRemoveActionForContainer(container);
            const action = batchAction || batchRemoveAction || getAutoNextActionForContainer(container);
            container.classList.remove('auto-next-label-candidate', 'auto-next-label-remove');
            let hint = container.querySelector('.auto-next-label-hint');
            if (!action || (!action.label && !Array.isArray(action.labels))) {{
                if (hint) {{
                    hint.remove();
                }}
                return;
            }}
            if (!hint) {{
                hint = parent.createElement('div');
                hint.className = 'auto-next-label-hint';
                container.appendChild(hint);
            }}
            const isBatchAction = action.mode === 'batch-apply' || action.mode === 'batch-remove';
            const visibleLabel = isBatchAction
                ? (action.labels.length + ' labels: ' + (getQuickTableFourLabelText(action.labels[0]) || action.labels[0]))
                : (getQuickTableFourLabelText(action.label) || action.label);
            const isRemoveAction = action.mode === 'remove' || action.mode === 'batch-remove';
            hint.textContent = (isRemoveAction ? 'Remove: ' : 'Auto: ') + visibleLabel;
            container.classList.add('auto-next-label-candidate');
            if (isRemoveAction) {{
                container.classList.add('auto-next-label-remove');
            }}
        }}

        function hydrateAutoNextLabelHints() {{
            clearAutoBatchHighlights();
            parent.querySelectorAll('.img-container').forEach((container) => {{
                renderAutoNextLabelHint(container);
            }});
        }}

        async function applyAutoNextLabelClick(container, imgPath, action) {{
            if (!container || !imgPath || !action || !action.label) {{
                return;
            }}
            if (action.mode === 'remove') {{
                const removeLabel = action.existingLabel || action.label;
                lastAutoNextClick = {{ path: '', canonicalLabel: '' }};
                removeLabelFromImagePath(imgPath, removeLabel);
                window.setTimeout(hydrateAutoNextLabelHints, 250);
                return;
            }}
            lastAutoNextClick = {{
                path: imgPath,
                canonicalLabel: action.canonicalLabel,
            }};
            await applyQuickLabelToImage(imgPath, action.label);
            hydrateAutoNextLabelHints();
        }}

        async function applyAutoBatchLabelClick(action) {{
            if (!action || !Array.isArray(action.paths) || !Array.isArray(action.labels)) {{
                return;
            }}
            const paths = action.paths.filter((path) => path);
            const labels = action.labels.map((label) => getDisplayLabelForCanonical(label)).filter((label) => label);
            if (!paths.length || paths.length !== labels.length) {{
                return;
            }}
            const beforeHistory = captureAnnotationSnapshot(paths);
            const eventId = 'auto-batch-label-' + Date.now() + '-' + Math.floor(Math.random() * 10000);
            try {{
                const operations = paths.map((path, index) => {{
                    const query = new URLSearchParams({{
                        action: 'set',
                        paths: path,
                        labels: labels[index],
                    }}).toString();
                    return fetch(clipboardServerBase + '/annotations?' + query)
                        .then((response) => response.json())
                        .then((data) => ({{ path, label: labels[index], data }}));
                }});
                const results = await Promise.all(operations);
                let ok = true;
                results.forEach((result) => {{
                    if (!result.data || !result.data.success) {{
                        ok = false;
                        logClientEvent('auto_batch_label_failed', {{
                            event_id: eventId,
                            path: result.path,
                            label: result.label,
                            response: result.data,
                        }});
                        return;
                    }}
                    const nextLabels = result.data.annotations && Object.prototype.hasOwnProperty.call(result.data.annotations, result.path)
                        ? normalizeLabelArray(result.data.annotations[result.path])
                        : [result.label];
                    applyQuickLabelResponseToClient(result.data, result.path, nextLabels);
                }});
                if (!ok) {{
                    showCopyBanner('✗', 'Auto batch partly failed');
                    return;
                }}
                updateSelectionCount('', true);
                scheduleSelectionDebugPanelRefresh();
                hydrateAutoNextLabelHints();
                pushAnnotationHistory(beforeHistory, captureAnnotationSnapshot(paths), 'auto batch ' + labels.length);
                showCopyBanner('🏷', 'Applied ' + labels.length + ' labels');
                logClientEvent('auto_batch_label_applied', {{
                    event_id: eventId,
                    count: labels.length,
                    labels,
                    paths,
                }});
            }} catch (err) {{
                logClientEvent('auto_batch_label_error', {{
                    event_id: eventId,
                    error: err && err.message ? err.message : String(err),
                }});
                showCopyBanner('✗', 'Auto batch failed');
            }}
        }}

        async function removeAutoBatchLabelClick(action) {{
            if (!action || !Array.isArray(action.paths) || !Array.isArray(action.labels)) {{
                return;
            }}
            const paths = action.paths.filter((path) => path);
            const labels = action.labels.filter((label) => label);
            if (!paths.length || paths.length !== labels.length) {{
                return;
            }}
            const beforeHistory = captureAnnotationSnapshot(paths);
            const eventId = 'auto-batch-remove-' + Date.now() + '-' + Math.floor(Math.random() * 10000);
            try {{
                const operations = paths.map((path, index) => {{
                    const query = new URLSearchParams({{
                        action: 'remove',
                        paths: path,
                        labels: labels[index],
                    }}).toString();
                    return fetch(clipboardServerBase + '/annotations?' + query)
                        .then((response) => response.json())
                        .then((data) => ({{ path, label: labels[index], data }}));
                }});
                const results = await Promise.all(operations);
                let ok = true;
                results.forEach((result) => {{
                    if (!result.data || !result.data.success) {{
                        ok = false;
                        logClientEvent('auto_batch_remove_failed', {{
                            event_id: eventId,
                            path: result.path,
                            label: result.label,
                            response: result.data,
                        }});
                        return;
                    }}
                    const fallbackLabels = normalizeLabelArray(clientAnnotationLabelsByPath[result.path] || [])
                        .filter((item) => !labelMatchesCanonical(item, result.label));
                    const nextLabels = result.data.annotations && Object.prototype.hasOwnProperty.call(result.data.annotations, result.path)
                        ? normalizeLabelArray(result.data.annotations[result.path])
                        : fallbackLabels;
                    applyQuickLabelResponseToClient(result.data, result.path, nextLabels);
                }});
                if (!ok) {{
                    showCopyBanner('✗', 'Auto undo partly failed');
                    return;
                }}
                updateSelectionCount('', true);
                scheduleSelectionDebugPanelRefresh();
                hydrateAutoNextLabelHints();
                pushAnnotationHistory(beforeHistory, captureAnnotationSnapshot(paths), 'auto undo ' + labels.length);
                showCopyBanner('↺', 'Removed ' + labels.length + ' labels');
                logClientEvent('auto_batch_label_removed', {{
                    event_id: eventId,
                    count: labels.length,
                    labels,
                    paths,
                }});
            }} catch (err) {{
                logClientEvent('auto_batch_remove_error', {{
                    event_id: eventId,
                    error: err && err.message ? err.message : String(err),
                }});
                showCopyBanner('✗', 'Auto undo failed');
            }}
        }}

        function isUniqueExactTableLabel(label) {{
            return getTableFromLabel(label) === '4' && Boolean(parseStationFromLabel(label || ''));
        }}

        function getPathsWithLabel(label) {{
            const target = normalizeLabelText(label);
            const paths = [];
            if (!target) {{
                return paths;
            }}
            Object.keys(clientAnnotationLabelsByPath || {{}}).forEach((path) => {{
                const labels = normalizeLabelArray(clientAnnotationLabelsByPath[path] || []);
                const hasLabel = labels.some((item) => normalizeLabelText(item) === target);
                if (hasLabel && path) {{
                    if (paths.indexOf(path) < 0) {{
                        paths.push(path);
                    }}
                }}
            }});
            return paths;
        }}

        function rebuildClientAnnotationLabelCounts() {{
            const nextCounts = {{}};
            Object.keys(clientAnnotationLabelsByPath || {{}}).forEach((path) => {{
                const labels = normalizeLabelArray(clientAnnotationLabelsByPath[path] || []);
                labels.forEach((label) => {{
                    nextCounts[label] = (nextCounts[label] || 0) + 1;
                }});
            }});
            clientAnnotationLabelCounts = nextCounts;
        }}

        function setClientAnnotationPathLabels(path, labels) {{
            if (!path) {{
                return;
            }}
            clientAnnotationLabelsByPath[path] = normalizeLabelArray(labels || []);
            rebuildClientAnnotationLabelCounts();
        }}

        function applyAuthoritativeAnnotationState(state) {{
            if (!state || !state.success) {{
                return false;
            }}
            const annotations = state.annotations && typeof state.annotations === 'object' ? state.annotations : {{}};
            const labelCounts = state.label_counts && typeof state.label_counts === 'object' ? state.label_counts : null;
            clientAnnotationLabelsByPath = {{}};
            Object.keys(annotations).forEach((path) => {{
                clientAnnotationLabelsByPath[path] = normalizeLabelArray(annotations[path] || []);
            }});
            if (labelCounts) {{
                clientAnnotationLabelCounts = Object.assign({{}}, labelCounts);
            }} else {{
                rebuildClientAnnotationLabelCounts();
            }}
            return true;
        }}

        async function refreshAuthoritativeAnnotationState() {{
            try {{
                const response = await fetch(clipboardServerBase + '/annotation-state?ts=' + Date.now());
                const state = await response.json();
                if (!applyAuthoritativeAnnotationState(state)) {{
                    return false;
                }}
                parent.querySelectorAll('.img-container').forEach((container) => {{
                    renderLabelBadges(container, clientAnnotationLabelsByPath[container.dataset.path || ''] || []);
                }});
                return true;
            }} catch (err) {{
                return false;
            }}
        }}

        function findUniqueLabelConflict(requestLabels, orderedPaths) {{
            const requestCounts = new Map();
            const requestPathSets = new Map();
            requestLabels.forEach((label, index) => {{
                if (!isUniqueExactTableLabel(label)) {{
                    return;
                }}
                const key = normalizeLabelText(label);
                requestCounts.set(key, (requestCounts.get(key) || 0) + 1);
                if (!requestPathSets.has(key)) {{
                    requestPathSets.set(key, new Set());
                }}
                if (orderedPaths[index]) {{
                    requestPathSets.get(key).add(orderedPaths[index]);
                }}
            }});
            for (const [key, count] of requestCounts.entries()) {{
                const label = requestLabels.find((item) => normalizeLabelText(item) === key) || '';
                if (count > 1) {{
                    return {{ label, reason: 'selection' }};
                }}
                const selectedForLabel = requestPathSets.get(key) || new Set();
                const existingOutsideSelection = getPathsWithLabel(label)
                    .filter((path) => !selectedForLabel.has(path));
                if (existingOutsideSelection.length > 0) {{
                    return {{ label, reason: 'existing', count: existingOutsideSelection.length }};
                }}
            }}
            return null;
        }}

        function expandSelectionLabels(baseLabel, targetCount) {{
            if (!baseLabel || targetCount <= 1) {{
                return [baseLabel];
            }}
            const table = getTableFromLabel(baseLabel);
            if (!table) {{
                return [baseLabel];
            }}

            const stationSuffixes = tableStationSuffixes[table] || [];
            const tableCap = getTableSelectionCap(table);
            if (!stationSuffixes.length && tableCap) {{
                const capped = Math.max(1, Math.min(targetCount, tableCap));
                return Array.from({{length: capped}}, () => baseLabel);
            }}
            const presets = annotationPresetGroups[`Table ${{table}}`] || [];
            if (stationSuffixes.length > 0) {{
                const norm = normalizeLabelText(baseLabel);
                const baseStation = parseStationFromLabel(baseLabel || '');
                if (baseStation) {{
                    const sameStationPresets = presets.filter((item) => parseStationFromLabel(item) === baseStation);
                    const sameStationIndex = sameStationPresets.findIndex((item) => normalizeLabelText(item) === norm);
                    if (sameStationIndex >= 0) {{
                        return sameStationPresets.slice(sameStationIndex, sameStationIndex + targetCount);
                    }}
                }}
                const matchedPresetIndex = presets.findIndex((item) => normalizeLabelText(item) === norm);
                if (matchedPresetIndex >= 0) {{
                    const expandedFromPreset = presets.slice(matchedPresetIndex, matchedPresetIndex + targetCount);
                    if (expandedFromPreset.length) {{
                        return expandedFromPreset;
                    }}
                }}

                const rowMatch = /(?:row|mg|md)\\s+(\\d+)/i.exec(baseLabel || '');
                const stationMatch = /(?:test\\s*station|testation)\\s*(\\d+)/i.exec(baseLabel || '');
                if (rowMatch) {{
                    const baseRow = Number(rowMatch[1]);
                    if (Number.isInteger(baseRow) && baseRow > 0) {{
                        const rows = (annotationPresetGroups[`Table ${{table}}`] || [])
                            .map((label) => sanitizeRowName(stripTablePrefix(label)))
                            .filter((value, index, self) => value && self.indexOf(value) === index);
                        if (rows.length) {{
                            const stationStartRaw = Number(stationMatch && stationMatch[1] ? stationMatch[1] : 1);
                            const normalizedStart = Number.isInteger(stationStartRaw) ? Math.max(1, stationStartRaw) : 1;
                            const availableRows = Math.max(0, rows.length - (baseRow - 1));
                            if (availableRows <= 0) {{
                                return [baseLabel];
                            }}
                            const expanded = [];
                            const stationCount = stationSuffixes.length;
                            const stationStartIndex = (normalizedStart - 1) % stationCount;
                            const orderedStations = stationSuffixes
                                .slice(stationStartIndex)
                                .concat(stationSuffixes.slice(0, stationStartIndex));
                            for (let stationOffset = 0; stationOffset < stationCount && expanded.length < targetCount; stationOffset++) {{
                                let rowOffset = 0;
                                while (rowOffset < availableRows && expanded.length < targetCount) {{
                                    const rowIndex = (baseRow - 1) + rowOffset;
                                    if (rowIndex >= rows.length) {{
                                        break;
                                    }}
                                    const rowName = stripTablePrefix(rows[rowIndex] || '');
                                    expanded.push(`Table ${{table}} ${{rowName}} ${{orderedStations[stationOffset]}}`.trim());
                                    rowOffset += 1;
                                }}
                            }}
                            if (expanded.length) {{
                                return expanded;
                            }}
                        }}
                    }}
                }}
            }}

            if (!presets.length) {{
                return [baseLabel];
            }}
            const norm = normalizeLabelText(baseLabel);
            const startIndex = presets.findIndex((item) => normalizeLabelText(item) === norm);
            if (startIndex < 0) {{
                return [baseLabel];
            }}
            const available = Math.max(0, presets.length - startIndex);
            const maxCount = Math.min(targetCount, available);
            if (!maxCount) {{
                return [baseLabel];
            }}
            return presets.slice(startIndex, startIndex + maxCount);
        }}

        function clampPresetIndex(index, length) {{
            if (!length) return -1;
            const normalized = Number(index);
            if (Number.isNaN(normalized)) return 0;
            return ((normalized % length) + length) % length;
        }}

        function safeJsonParse(raw) {{
            try {{
                return JSON.parse(raw || '{{}}');
            }} catch (err) {{
                return {{}};
            }}
        }}

        function getFolderAnnotationState() {{
            const key = presetStateStorageKey;
            try {{
                const raw = parent.defaultView.localStorage.getItem(key);
                const data = safeJsonParse(raw);
                return (data && typeof data === 'object') ? data : {{}};
            }} catch (err) {{
                return {{}};
            }}
        }}

        function saveFolderAnnotationState(nextState) {{
            try {{
                parent.defaultView.localStorage.setItem(presetStateStorageKey, JSON.stringify(nextState || {{}}));
            }} catch (err) {{
                // localStorage unavailable on this session
            }}
        }}

        function getFolderPresetCursor() {{
            const state = getFolderAnnotationState();
            const list = Array.isArray(annotationFlatPresets) ? annotationFlatPresets : [];
            const tableFirstIndex = list.findIndex((item) => item.indexOf('Table 3 ') === 0);
            if (!activeFolderPath) return tableFirstIndex >= 0 ? tableFirstIndex : 0;
            const folderEntry = state[activeFolderPath];
            const cursor = folderEntry && Number.isInteger(folderEntry.tableCursor) ? folderEntry.tableCursor : null;
            if (cursor !== null) {{
                return clampPresetIndex(cursor, list.length);
            }}
            return tableFirstIndex >= 0 ? tableFirstIndex : 0;
        }}

        function getFolderInstantOffState() {{
            if (!activeFolderPath) return null;
            try {{
                const raw = parent.defaultView.localStorage.getItem(instantOffStatusStorageKey);
                const state = safeJsonParse(raw);
                const stored = state && typeof state === 'object' ? state[activeFolderPath] : null;
                if (typeof stored === 'string' && instantOffStatusChoices.includes(stored)) {{
                    return stored;
                }}
            }} catch (err) {{
                return null;
            }}
            return null;
        }}

        function saveFolderInstantOffState(nextStatus) {{
            if (!activeFolderPath || !nextStatus) return;
            try {{
                const raw = parent.defaultView.localStorage.getItem(instantOffStatusStorageKey);
                let data = safeJsonParse(raw);
                if (typeof data !== 'object' || data === null || Array.isArray(data)) {{
                    data = {{}};
                }}
                data[activeFolderPath] = nextStatus;
                parent.defaultView.localStorage.setItem(instantOffStatusStorageKey, JSON.stringify(data));
            }} catch (err) {{
                return;
            }}
        }}

        function setFolderInstantOffStatus(nextStatus) {{
            if (!instantOffStatusChoices.includes(nextStatus)) {{
                return false;
            }}
            if (activeFolderInstantOffStatus === nextStatus) {{
                return false;
            }}
            activeFolderInstantOffStatus = nextStatus;
            saveFolderInstantOffState(nextStatus);
            return true;
        }}

        function getInitialInstantOffStatus() {{
            if (!instantOffStatusChoices.length) {{
                return '';
            }}
            if (typeof folderInstantOffStatus === 'string' && instantOffStatusChoices.includes(folderInstantOffStatus)) {{
                return folderInstantOffStatus;
            }}
            return instantOffStatusChoices[0];
        }}

        function buildCandidateServerUrls() {{
            const candidates = [];
            const seen = new Set();
            const protocolPriority = ['http:'];
            if (window.location.protocol === 'https:') {{
                // Keep HTTPS as fallback for secure-page deployments, but always prefer HTTP
                // since the clipboard helper is intentionally started as HTTP.
                protocolPriority.push('https:');
            }}
            const hosts = [];
            const hostSet = new Set();
            const addHost = (value) => {{
                if (!value || hostSet.has(value)) return;
                hostSet.add(value);
                hosts.push(value);
            }};
            addHost(window.location.hostname);
            addHost('127.0.0.1');
            addHost('localhost');
            const ports = clipboardPorts && clipboardPorts.length ? clipboardPorts : [{CLIPBOARD_PORT}];

            for (const proto of protocolPriority) {{
                for (const host of hosts) {{
                    const normalized = normalizeHost(host);
                    for (const port of ports) {{
                        const url = `${{proto}}//${{normalized}}:${{port}}`;
                        if (!seen.has(url)) {{
                            seen.add(url);
                            candidates.push(url);
                        }}
                    }}
                }}
            }}
            return candidates;
        }}

        function setClipboardServer(base) {{
            clipboardServerBase = base;
            clipboardServerUrl = base + '/copy?path=';
            clipboardIndexUrl = base + '/index';
            clipboardStartDragUrl = base + '/start-drag?paths=';
        }}

        function setClipboardServerFromCandidate(candidate, selected = false) {{
            const candidateBase = candidate;
            setClipboardServer(candidateBase);
            if (selected) {{
                clipboardApiState.ready = true;
                clipboardApiState.checkedAt = Date.now();
                clipboardApiState.lastError = '';
            }}
        }}

        // Update copy status in the folder banner (leftmost element)
        function showCopyBanner(icon, msg) {{
            const folderBanner = parent.getElementById('folder-banner');
            if (!folderBanner) return;

            let copyStatus = folderBanner.querySelector('.copy-status');
            if (!copyStatus) {{
                copyStatus = document.createElement('span');
                copyStatus.className = 'copy-status';
                copyStatus.style.cssText = `
                    opacity: 0.7;
                    font-size: 0.65rem;
                    padding-right: 8px;
                    border-right: 1px solid rgba(255, 255, 255, 0.2);
                    margin-right: 6px;
                `;
                // Insert at the very beginning of the banner
                folderBanner.insertBefore(copyStatus, folderBanner.firstChild);
            }}
            copyStatus.textContent = icon + ' ' + msg;
        }}

        // Initialize copy status in folder banner
        function initCopyStatus() {{
            const folderBanner = parent.getElementById('folder-banner');
            if (!folderBanner || folderBanner.querySelector('.copy-status')) return;

            const copyStatus = document.createElement('span');
            copyStatus.className = 'copy-status';
            copyStatus.style.cssText = `
                opacity: 0.5;
                font-size: 0.65rem;
                padding-right: 8px;
                border-right: 1px solid rgba(255, 255, 255, 0.2);
                margin-right: 6px;
            `;
            copyStatus.textContent = '○ hover to copy';
            folderBanner.insertBefore(copyStatus, folderBanner.firstChild);
        }}

        async function ensureClipboardServerReady(action = 'request', force = false) {{
            const now = Date.now();
            if (!force && clipboardApiState.ready !== null && clipboardServerBase && now - clipboardApiState.checkedAt < 2500) {{
                return clipboardApiState.ready;
            }}
            const candidates = buildCandidateServerUrls();
            for (let i = 0; i < candidates.length; i++) {{
                const candidate = candidates[i];
                const candidateIndexUrl = candidate + '/index';
                const controller = new AbortController();
                const timeout = setTimeout(() => controller.abort(), 1200);
                const started = Date.now();
                try {{
                    const response = await fetch(candidateIndexUrl, {{
                        method: 'GET',
                        cache: 'no-store',
                        signal: controller.signal
                    }});
                    clearTimeout(timeout);
                    const elapsed = Date.now() - started;
                    if (response.ok) {{
                        setClipboardServerFromCandidate(candidate, true);
                        logClientEvent('clipboard_server_resolved', {{
                            action,
                            candidate,
                            attempt: i + 1,
                            elapsed_ms: elapsed
                        }});
                        logClientEvent('clipboard_server_health_client', {{
                            action,
                            ready: true,
                            candidate,
                            status: response.status,
                            attempt: i + 1,
                            elapsed_ms: elapsed
                        }});
                        return true;
                    }}
                    clipboardApiState.ready = false;
                    clipboardApiState.checkedAt = now;
                    clipboardApiState.lastError = `health_${{response.status}}`;
                    logClientEvent('clipboard_server_health_client', {{
                        action,
                        ready: false,
                        candidate,
                        status: response.status,
                        error: clipboardApiState.lastError,
                        attempt: i + 1,
                        elapsed_ms: elapsed
                    }});
                }} catch (err) {{
                    clearTimeout(timeout);
                    clipboardApiState.ready = false;
                    clipboardApiState.checkedAt = now;
                    clipboardApiState.lastError = err && err.name === 'AbortError' ? 'timeout' : String(err.message || err);
                    logClientEvent('clipboard_server_health_client', {{
                        action,
                        ready: false,
                        candidate,
                        error: clipboardApiState.lastError,
                        attempt: i + 1
                    }});
                }}
            }}
            return false;
        }}

        function applyBrightness(value) {{
            const parsed = Math.min(260, Math.max(20, parseInt(value, 10) || defaultBrightness));
            parent.documentElement.style.setProperty('--image-grid-brightness', parsed + '%');
            const slider = parent.getElementById('image-brightness-slider');
            const valueLabel = parent.getElementById('image-brightness-value');
            if (slider) {{
                slider.value = String(parsed);
            }}
            if (valueLabel) {{
                valueLabel.textContent = parsed + '%';
            }}
            try {{
                parent.defaultView.localStorage.setItem(brightnessStorageKey, String(parsed));
            }} catch (err) {{
                // localStorage blocked or unavailable
            }}
        }}

        function setupBrightnessControl() {{
            let control = parent.getElementById('image-brightness-control');
            const stored = (() => {{
                try {{
                    const raw = parent.defaultView.localStorage.getItem(brightnessStorageKey);
                    const preset = parent.defaultView.localStorage.getItem(brightnessPresetKey);
                    const resolved = raw !== null ? raw : preset;
                    return resolved ? parseInt(resolved, 10) : defaultBrightness;
                }} catch (err) {{
                    return defaultBrightness;
                }}
            }})();

            if (!control) {{
                control = parent.createElement('div');
                control.id = 'image-brightness-control';
                control.className = 'brightness-control';
                control.innerHTML = `
                    <button type="button" class="brightness-toggle" id="image-brightness-toggle" title="Adjust image brightness" aria-label="Image brightness">☼</button>
                    <div class="brightness-panel" id="image-brightness-panel">
                        <input id="image-brightness-slider" class="brightness-slider" type="range" min="20" max="260" step="1" />
                        <span id="image-brightness-value" class="brightness-value"></span>
                        <button type="button" class="brightness-action" id="image-brightness-save" title="Save current brightness as preset">Save preset</button>
                        <button type="button" class="brightness-action" id="image-brightness-reset" title="Reset brightness to default">Reset to default</button>
                    </div>
                `;
                parent.body.appendChild(control);

                const slider = control.querySelector('#image-brightness-slider');
                const saveBtn = control.querySelector('#image-brightness-save');
                const resetBtn = control.querySelector('#image-brightness-reset');
                slider.addEventListener('input', function() {{
                    applyBrightness(this.value);
                }});
                saveBtn.addEventListener('click', function(e) {{
                    e.preventDefault();
                    e.stopPropagation();
                    const slider = parent.getElementById('image-brightness-slider');
                    if (!slider) return;
                    try {{
                        const raw = parseInt(slider.value, 10);
                        const parsed = Math.min(260, Math.max(20, raw || defaultBrightness));
                        parent.defaultView.localStorage.setItem(brightnessPresetKey, String(parsed));
                        showCopyBanner('☆', 'Saved brightness preset: ' + parsed + '%');
                    }} catch (err) {{
                        showCopyBanner('✗', 'Unable to save preset');
                    }}
                }});
                resetBtn.addEventListener('click', function(e) {{
                    e.preventDefault();
                    e.stopPropagation();
                    applyBrightness(defaultBrightness);
                    try {{
                        parent.defaultView.localStorage.setItem(brightnessStorageKey, String(defaultBrightness));
                    }} catch (err) {{
                        // localStorage blocked or unavailable
                    }}
                    showCopyBanner('↺', 'Brightness reset to default');
                }});
            }}

            applyBrightness(stored);
            const slider = parent.getElementById('image-brightness-slider');
            const valueLabel = parent.getElementById('image-brightness-value');
            if (slider) {{
                slider.value = String(Math.min(260, Math.max(20, stored)));
            }}
            if (valueLabel) {{
                valueLabel.textContent = Math.min(260, Math.max(20, stored)) + '%';
            }}
        }}

        function startAdjacentFolderThumbnailPreload() {{
            const urls = Array.isArray(adjacentFolderPreloadThumbnailUrls)
                ? adjacentFolderPreloadThumbnailUrls.filter((url) => typeof url === 'string' && url)
                : [];
            if (!urls.length) {{
                return;
            }}
            const preloadKey = '__reportLabelerAdjacentFolderPreload';
            const state = hostWindow[preloadKey] || {{
                seen: Object.create(null),
                inflight: Object.create(null),
            }};
            if (!state.seen || typeof state.seen !== 'object') {{
                state.seen = Object.create(null);
            }}
            if (!state.inflight || typeof state.inflight !== 'object') {{
                state.inflight = Object.create(null);
            }}
            hostWindow[preloadKey] = state;
            const queue = urls.filter((url) => !state.seen[url] && !state.inflight[url]);
            if (!queue.length) {{
                return;
            }}
            const concurrency = 3;
            let active = 0;
            let cursor = 0;
            const pump = () => {{
                if (!isActiveScriptInstance()) {{
                    return;
                }}
                while (active < concurrency && cursor < queue.length) {{
                    const url = queue[cursor];
                    cursor += 1;
                    active += 1;
                    const img = new Image();
                    state.inflight[url] = img;
                    const done = () => {{
                        active = Math.max(0, active - 1);
                        delete state.inflight[url];
                        state.seen[url] = Date.now();
                        pump();
                    }};
                    img.onload = done;
                    img.onerror = done;
                    img.decoding = 'async';
                    img.loading = 'eager';
                    img.src = url;
                }}
            }};
            const start = () => {{
                if (isActiveScriptInstance()) {{
                    pump();
                }}
            }};
            if (hostWindow.requestIdleCallback) {{
                hostWindow.requestIdleCallback(start, {{ timeout: 1200 }});
            }} else {{
                window.setTimeout(start, 450);
            }}
        }}

        // Try to init immediately, and also poll until banner exists
        initCopyStatus();
        setupBrightnessControl();
        startAdjacentFolderThumbnailPreload();
        const initInterval = setActiveInterval(() => {{
            if (parent.getElementById('folder-banner')) {{
                initCopyStatus();
                setupBrightnessControl();
                clearInterval(initInterval);
            }}
        }}, 100);

        // Copy via background server (no page reload, works without focus)
        async function copyViaServer(imgPath, container, source = 'unknown') {{
            try {{
                const response = await fetch(clipboardServerUrl + encodeURIComponent(imgPath) + '&source=' + source);
                const result = await response.json();
                const icon = result.success ? '✓' : '✗';
                const msg = result.success ? 'Copied: ' + result.name : 'Failed: ' + result.name;
                showCopyBanner(icon, msg);

                // Visual feedback on image
                if (container) {{
                    container.classList.add('copy-flash');
                    setTimeout(() => {{
                        if (container && container.isConnected) {{
                            container.classList.remove('copy-flash');
                        }}
                    }}, 180);
                }}
                return result.success;
            }} catch (err) {{
                console.error('Clipboard server error:', err);
                showCopyBanner('✗', 'Server error');
                return false;
            }}
        }}

        // Rotate via background server - instant visual rotation
        async function rotateViaServer(imgPath, container) {{
            try {{
                // Instantly rotate the image visually (CSS transform)
                const img = container.querySelector('img');
                if (img) {{
                    // Track rotation state on the element
                    const currentRotation = parseInt(img.dataset.rotation || '0');
                    const newRotation = currentRotation + 90;
                    img.dataset.rotation = newRotation;
                    img.style.transition = 'transform 0.2s ease';
                    img.style.transform = `rotate(${{newRotation}}deg)`;
                }}

                // Save rotation on server in background
                const response = await fetch(clipboardServerBase + '/rotate?path=' + encodeURIComponent(imgPath));
                const result = await response.json();
                const icon = result.success ? '↻' : '✗';
                const msg = result.success ? 'Rotated: ' + result.name : 'Failed: ' + result.name;
                showCopyBanner(icon, msg);

                return result.success;
            }} catch (err) {{
                console.error('Rotate server error:', err);
                showCopyBanner('✗', 'Server error');
                return false;
            }}
        }}

        function getContainerPath(target) {{
            const container = target && target.closest ? target.closest('.img-container') : null;
            return container && container.dataset ? container.dataset.path : null;
        }}

        function closeLightbox() {{
            parent.querySelectorAll('.lightbox-overlay, .lightbox-viewer, .lightbox-close').forEach(el => el.remove());
            if (parent.__imgLightboxEscHandler) {{
                parent.removeEventListener('keydown', parent.__imgLightboxEscHandler, true);
                parent.__imgLightboxEscHandler = null;
            }}
        }}

        async function navigateLightbox(direction) {{
            try {{
                const endpoint = direction === 'prev' ? '/prev' : '/next';
                const response = await fetch(clipboardServerBase + endpoint);
                const data = await response.json();
                if (data && data.path) {{
                    showLightbox(data.path);
                    if (data.name) {{
                        showCopyBanner('→', data.name);
                    }}
                }}
            }} catch (err) {{
                showCopyBanner('✗', 'Image navigation failed');
            }}
        }}

        // Enlarge image in lightbox
        function showLightbox(imgPath) {{
            if (!imgPath) return;

            const imageUrl = clipboardServerBase + '/image?path=' + encodeURIComponent(imgPath);

            // Replace currently opened image on each click.
            closeLightbox();

            const overlay = document.createElement('div');
            overlay.className = 'lightbox-overlay';

            const viewer = document.createElement('div');
            viewer.className = 'lightbox-viewer';
            viewer.innerHTML = `
                <img class="lightbox-image" src="${{imageUrl}}" />
            `;

            const closeButton = document.createElement('span');
            closeButton.className = 'lightbox-close';
            closeButton.textContent = '×';
            closeButton.setAttribute('aria-label', 'Close image preview');
            closeButton.addEventListener('click', function(e) {{
                e.preventDefault();
                e.stopPropagation();
                closeLightbox();
            }});
            viewer.appendChild(closeButton);

            const closeOnEsc = function(e) {{
                if (e.key === 'Escape') {{
                    closeLightbox();
                }}
            }};

            if (parent.__imgLightboxEscHandler) {{
                parent.removeEventListener('keydown', parent.__imgLightboxEscHandler, true);
            }}
            parent.__imgLightboxEscHandler = closeOnEsc;
            parent.addEventListener('keydown', closeOnEsc, true);

            parent.body.appendChild(overlay);
            parent.body.appendChild(viewer);
        }}

        // Wire up hover/click handlers for image containers
        function bindImageContainerInteractions() {{
            const targetDocs = [];
            const resolvedPrimary = resolveInteractionDocument();
            if (currentDocument && currentDocument.querySelectorAll && !targetDocs.includes(currentDocument)) {{
                targetDocs.push(currentDocument);
            }}
            if (resolvedPrimary && resolvedPrimary !== currentDocument) {{
                targetDocs.push(resolvedPrimary);
            }}

            const ensureDelegatedSelectionHandlers = (targetDoc) => {{
                if (!targetDoc || !targetDoc.addEventListener || targetDoc.__reportLabelerDelegatedHandlersBound) {{
                    return;
                }}
                targetDoc.__reportLabelerDelegatedHandlersBound = true;
                targetDoc.addEventListener('click', function(e) {{
                    const clickTarget = e && e.target ? (e.target.nodeType === 3 ? e.target.parentElement : e.target) : null;
                    const container = clickTarget && clickTarget.closest ? clickTarget.closest('.img-container') : null;
                    if (!container) {{
                        return;
                    }}
                    handleContainerClick(e);
                }}, true);
            }};

            const bindForDoc = (targetDoc) => {{
                if (!targetDoc || !targetDoc.querySelectorAll || !targetDoc.body) {{
                    return;
                }}
                const containers = targetDoc.querySelectorAll('.img-container');
                if (!containers.length) {{
                    return;
                }}
                containers.forEach((container) => {{
                    const copyBtn = container.querySelector('.copy-btn');
                    const rotateBtn = container.querySelector('.rotate-btn');
                    const enlargeBtn = container.querySelector('.enlarge-btn');
                    const imgPath = getContainerPath(container);

                    if (container.dataset.reportLabelerClickBound !== '1') {{
                        container.dataset.reportLabelerClickBound = '1';
                        container.addEventListener('click', function(e) {{
                            return handleContainerClick(e);
                        }}, true);
                    }}

                    // Bind action buttons once per container
                    if (container.dataset.reportLabelerBound !== '1') {{
                        container.dataset.reportLabelerBound = '1';

                        // Click handler for copy button
                        if (copyBtn) {{
                            copyBtn.addEventListener('click', function(e) {{
                                e.preventDefault();
                                e.stopPropagation();
                                if (!imgPath) return;
                                copyViaServer(imgPath, container, 'button');
                            }});
                        }}

                        // Click handler for rotate button
                        if (rotateBtn) {{
                            rotateBtn.addEventListener('click', function(e) {{
                                e.preventDefault();
                                e.stopPropagation();
                                e.stopImmediatePropagation();
                                if (!imgPath) return;
                                rotateViaServer(imgPath, container);
                                return false;
                            }});
                            // Prevent any focus-related scrolling
                            rotateBtn.addEventListener('mousedown', function(e) {{
                                e.preventDefault();
                            }});
                            rotateBtn.addEventListener('focus', function(e) {{
                                e.preventDefault();
                                this.blur();
                            }});
                        }}

                        // Click handler for enlarge button
                        if (enlargeBtn) {{
                            enlargeBtn.addEventListener('click', function(e) {{
                                e.preventDefault();
                                e.stopPropagation();
                                e.stopImmediatePropagation();
                                if (!imgPath) return;
                                showLightbox(imgPath);
                                return false;
                            }});
                            enlargeBtn.addEventListener('mousedown', function(e) {{
                                e.preventDefault();
                            }});
                            enlargeBtn.addEventListener('focus', function(e) {{
                                e.preventDefault();
                                this.blur();
                            }});
                        }}

                        if (imgPath) {{
                            container.style.cursor = 'pointer';
                        }}

                        syncQuickLabelActions(container);
                        bindQuickLabelButtonEvents(container, imgPath);
                    }}

                    // Track hover for Hammerspoon (always enabled)
                    if (imgPath) {{
                        if (container.dataset.reportLabelerHoverBound !== '1') {{
                            container.dataset.reportLabelerHoverBound = '1';
                    container.addEventListener('mouseenter', function() {{
                        if (lastHoveredPath === imgPath) return;
                        lastHoveredPath = imgPath;
                        const batchAction = getTableThreeBatchCandidateForContainer(container) || getAutoBatchActionForContainer(container);
                        if (batchAction) {{
                            applyAutoBatchHighlights(batchAction);
                        }} else {{
                            clearAutoBatchHighlights();
                        }}

                        // Always track hover for Hammerspoon's /copy-last
                        fetch(clipboardServerBase + '/hover?path=' + encodeURIComponent(imgPath))
                            .catch(() => {{}});

                                // Auto-copy if enabled
                                if (autoCopyEnabled) {{
                                    clearTimeout(hoverDebounce);
                                    hoverDebounce = setTimeout(() => {{
                                        copyViaServer(imgPath, container, 'auto');
                                    }}, 300);
                                }}
                            }});

                            container.addEventListener('mouseleave', function() {{
                                clearTimeout(hoverDebounce);
                            }});
                        }}
                    }}

                    if (container.dataset.reportLabelerAutoBatchDblBound !== '1') {{
                        container.dataset.reportLabelerAutoBatchDblBound = '1';
                        container.addEventListener('dblclick', handleContainerDoubleClick, true);
                    }}

                    if (container.dataset.reportLabelerDragBound !== '1') {{
                        container.dataset.reportLabelerDragBound = '1';
                        container.setAttribute('draggable', 'true');
                        container.addEventListener('dragstart', async function(e) {{
                            // If dragging an unselected item, select it first
                            if (!selectedPaths.has(imgPath)) {{
                                selectOnly(container, imgPath);
                            }}

                            // Get all selected paths
                            const paths = Array.from(selectedPaths);
                            const nativeDragEventId = 'dragstart-' + Date.now() + '-' + Math.floor(Math.random() * 10000);
                            logClientEvent('dragstart_action', {{
                                event_id: nativeDragEventId,
                                count: paths.length,
                                start_path: paths[0] || null,
                                end_path: paths[paths.length - 1] || null
                            }});
                            const readyForDrag = await ensureClipboardServerReady('dragstart', true);
                            if (!readyForDrag) {{
                                logClientEvent('dragstart_not_ready', {{
                                    event_id: nativeDragEventId,
                                    reason: clipboardApiState.lastError || 'server unavailable'
                                }});
                                showCopyBanner('✗', 'Drag helper unavailable');
                                return;
                            }}

                            if (paths.length === 0) {{
                                logClientEvent('dragstart_aborted', {{ event_id: nativeDragEventId, reason: 'no_selection' }});
                                showCopyBanner('✗', 'No files selected for drag');
                                return;
                            }}

                            // Copy files to clipboard via server (so user can Cmd+V paste)
                            const pathsParam = paths.join('|');
                            fetch(clipboardServerBase + '/copy-files-to-clipboard?paths=' + encodeURIComponent(pathsParam))
                                .then(r => r.json())
                                .then(result => {{
                                    logClientEvent('dragstart_server_response', {{
                                        event_id: nativeDragEventId,
                                        success: result.success,
                                        return_code: result.return_code,
                                        error: result.error,
                                        count: result.count
                                    }});
                                    if (result.success) {{
                                        showCopyBanner('📋', result.count + ' file(s) ready - Cmd+V to paste');
                                    }} else {{
                                        showCopyBanner('✗', 'Prepare failed');
                                    }}
                                }})
                                .catch((err) => {{
                                    logClientEvent('dragstart_server_error', {{
                                        event_id: nativeDragEventId,
                                        error: err.message
                                    }});
                                    showCopyBanner('✗', 'Prepare failed');
                                }});

                            // Set drag data - use text/uri-list for file paths
                            const uriList = paths.map(p => 'file://' + p).join('\\n');
                            e.dataTransfer.setData('text/uri-list', uriList);
                            e.dataTransfer.setData('text/plain', paths.join('\\n'));

                            e.dataTransfer.effectAllowed = 'copy';

                            // Create custom drag image showing count
                            const dragEl = targetDoc.createElement('div');
                            dragEl.style.cssText = 'position:absolute;left:-9999px;padding:8px 16px;background:#4CAF50;color:white;border-radius:4px;font-size:14px;';
                            dragEl.textContent = paths.length + ' image' + (paths.length > 1 ? 's' : '');
                            targetDoc.body.appendChild(dragEl);
                            e.dataTransfer.setDragImage(dragEl, 0, 0);
                            setTimeout(() => dragEl.remove(), 0);

                            showCopyBanner('⇄', 'Dragging ' + paths.length + ' - Cmd+V to paste anywhere');
                        }});

                        container.addEventListener('dragend', function(e) {{
                            showCopyBanner('📋', 'Files in clipboard - Cmd+V to paste');
                        }});
                    }}
                }});
            }};

            targetDocs.forEach((targetDoc) => {{
                ensureDelegatedSelectionHandlers(targetDoc);
                bindForDoc(targetDoc);
            }});
        }}

        bindImageContainerInteractions();

        function isActionButton(target) {{
            const targetEl = (target && target.nodeType === 3) ? target.parentElement : target;
            if (!targetEl) return false;
            if (targetEl.classList && (
                targetEl.classList.contains('copy-btn') ||
                targetEl.classList.contains('rotate-btn') ||
                targetEl.classList.contains('enlarge-btn') ||
                targetEl.classList.contains('quick-label-btn') ||
                targetEl.classList.contains('label-chip') ||
                targetEl.classList.contains('label-chip-x')
            )) return true;
            return !!(targetEl.closest && (
                targetEl.closest('.copy-btn') ||
                targetEl.closest('.rotate-btn') ||
                targetEl.closest('.enlarge-btn') ||
                targetEl.closest('.quick-label-btn') ||
                targetEl.closest('.label-chip') ||
                targetEl.closest('.label-chip-x')
            ));
        }}

        function getQuickTableFourLabelText(rawLabel) {{
            const normalized = String(rawLabel || '').trim();
            if (!normalized) {{
                return '';
            }}
            return normalized
                .replace(/\\s+/g, ' ')
                .replace(/^Table\\s+4\\s+/i, 'T4 ')
                .replace(/\\s+Test\\s+Station\\s+(\\d+)/i, ' TS$1')
                .trim();
        }}

        function syncQuickLabelActions(container) {{
            if (!container || !container.dataset) {{
                return;
            }}
            if (!tableFourQuickLabels.length) {{
                return;
            }}
            const existing = container.querySelector('.quick-label-actions');
            if (existing) {{
                existing.innerHTML = tableFourQuickLabels.map((label) => {{
                    const safeLabel = escapeHtmlAttribute(label);
                    const visibleLabel = escapeHtml(getQuickTableFourLabelText(label));
                    return '<button type="button" class="quick-label-btn" title="Apply/remove ' + safeLabel + '" data-quick-label="' + safeLabel + '">' + visibleLabel + '</button>';
                }}).join('');
                existing.querySelectorAll('.quick-label-btn').forEach((btn) => {{
                    btn.dataset.quickLabelBound = '0';
                }});
                return;
            }}
            const quickBar = parent.createElement('div');
            quickBar.className = 'quick-label-actions';
            quickBar.innerHTML = tableFourQuickLabels.map((label) => {{
                const safeLabel = escapeHtmlAttribute(label);
                const visibleLabel = escapeHtml(getQuickTableFourLabelText(label));
                return '<button type="button" class="quick-label-btn" title="Apply/remove ' + safeLabel + '" data-quick-label="' + safeLabel + '">' + visibleLabel + '</button>';
            }}).join('');
            container.appendChild(quickBar);
        }}

        function getConflictPathsForUniqueLabel(label, targetPath) {{
            if (!isUniqueExactTableLabel(label)) {{
                return [];
            }}
            const normalizedTargetPath = String(targetPath || '').trim();
            const targetLabel = normalizeLabelText(label);
            const conflicts = [];
            Object.keys(clientAnnotationLabelsByPath || {{}}).forEach((path) => {{
                if (!path || path === normalizedTargetPath) {{
                    return;
                }}
                const labels = normalizeLabelArray(clientAnnotationLabelsByPath[path] || []);
                if (labels.some((item) => normalizeLabelText(item) === targetLabel)) {{
                    conflicts.push(path);
                }}
            }});
            return conflicts;
        }}

        function applyQuickLabelResponseToClient(data, path, fallbackLabels) {{
            if (data && data.label_counts && typeof data.label_counts === 'object') {{
                clientAnnotationLabelCounts = Object.assign({{}}, data.label_counts);
            }}
            const annotations = data && data.annotations && typeof data.annotations === 'object' ? data.annotations : null;
            const nextLabels = annotations && Object.prototype.hasOwnProperty.call(annotations, path)
                ? normalizeLabelArray(annotations[path])
                : normalizeLabelArray(fallbackLabels || []);
            setClientAnnotationPathLabels(path, nextLabels);
            const container = getContainerByPath(path);
            if (container) {{
                renderLabelBadges(container, nextLabels);
            }}
            return nextLabels;
        }}

        async function removeQuickLabelConflicts(conflictPaths, label, eventId) {{
            if (!Array.isArray(conflictPaths) || !conflictPaths.length) {{
                return true;
            }}
            const targetLabel = normalizeLabelText(label);
            const results = await Promise.all(conflictPaths.map(async (conflictPath) => {{
                const query = new URLSearchParams({{
                    action: 'remove',
                    paths: conflictPath,
                    labels: label,
                }}).toString();
                const response = await fetch(clipboardServerBase + '/annotations?' + query);
                const data = await response.json();
                return {{ path: conflictPath, data }};
            }}));
            let ok = true;
            results.forEach((result) => {{
                if (!result.data || !result.data.success) {{
                    ok = false;
                    logClientEvent('quick_label_conflict_remove_failed', {{
                        event_id: eventId,
                        path: result.path,
                        label,
                        response: result.data,
                    }});
                    return;
                }}
                const fallbackLabels = normalizeLabelArray(clientAnnotationLabelsByPath[result.path] || [])
                    .filter((item) => normalizeLabelText(item) !== targetLabel);
                applyQuickLabelResponseToClient(result.data, result.path, fallbackLabels);
            }});
            return ok;
        }}

        async function applyQuickLabelToImage(path, nextLabel) {{
            if (!isActiveScriptInstance()) {{
                return;
            }}
            const normalizedPath = String(path || '').trim();
            const normalizedLabel = String(nextLabel || '').trim();
            if (!normalizedPath || !normalizedLabel) {{
                return;
            }}
            const current = normalizeLabelArray(clientAnnotationLabelsByPath[normalizedPath] || []);
            const hasLabel = current.some((label) => isLabelSetMatched(label, normalizedLabel));
            const action = hasLabel ? 'remove' : 'set';
            const conflictPaths = action === 'set' ? getConflictPathsForUniqueLabel(normalizedLabel, normalizedPath) : [];
            const historyPaths = [normalizedPath].concat(conflictPaths || []);
            const beforeHistory = captureAnnotationSnapshot(historyPaths);
            const eventId = 'quick-label-' + Date.now() + '-' + Math.floor(Math.random() * 10000);
            const query = new URLSearchParams({{
                action: action,
                paths: normalizedPath,
                labels: normalizedLabel,
            }}).toString();
            try {{
                if (conflictPaths.length) {{
                    const removedConflicts = await removeQuickLabelConflicts(conflictPaths, normalizedLabel, eventId);
                    if (!removedConflicts) {{
                        showCopyBanner('✗', 'Quick label move failed');
                        return;
                    }}
                }}
                const response = await fetch(clipboardServerBase + '/annotations?' + query);
                const data = await response.json();
                if (!data || !data.success) {{
                    logClientEvent('quick_label_update_failed', {{
                        event_id: eventId,
                        path: normalizedPath,
                        action: action,
                        label: normalizedLabel,
                        response: data,
                    }});
                    showCopyBanner('✗', 'Quick label failed');
                    return;
                }}
                const nextLabels = data.annotations && Object.prototype.hasOwnProperty.call(data.annotations, normalizedPath)
                    ? normalizeLabelArray(data.annotations[normalizedPath])
                        : (action === 'remove'
                            ? current.filter((item) => !isLabelSetMatched(item, normalizedLabel))
                            : [normalizedLabel]);
                applyQuickLabelResponseToClient(data, normalizedPath, nextLabels);
                pushAnnotationHistory(beforeHistory, captureAnnotationSnapshot(historyPaths), (action === 'remove' ? 'remove ' : 'apply ') + normalizedLabel);
                if (selectedPaths.has(normalizedPath)) {{
                    updateSelectionCount();
                }}
                updateSelectionCount('', true);
                scheduleSelectionDebugPanelRefresh();
                showCopyBanner('🏷', (action === 'remove' ? 'Removed ' : 'Applied ') + normalizedLabel);
                logClientEvent('quick_label_updated', {{
                    event_id: eventId,
                    path: normalizedPath,
                    action,
                    label: normalizedLabel,
                }});
            }} catch (err) {{
                logClientEvent('quick_label_update_error', {{
                    event_id: eventId,
                    path: normalizedPath,
                    action: action,
                    label: normalizedLabel,
                    error: err.message,
                }});
                showCopyBanner('✗', 'Quick label failed');
                console.error('Quick label error:', err);
            }}
        }}

        function bindQuickLabelButtonEvents(container, imgPath) {{
            if (!container || !container.querySelectorAll) {{
                return;
            }}
            container.querySelectorAll('.quick-label-btn').forEach((btn) => {{
                if (!btn || btn.dataset.quickLabelBound === '1') {{
                    return;
                }}
                btn.dataset.quickLabelBound = '1';
                btn.addEventListener('click', function(e) {{
                    if (e && isLabelRenameShortcut(e)) {{
                        return;
                    }}
                    e.preventDefault();
                    e.stopPropagation();
                    e.stopImmediatePropagation();
                    const rawLabel = decodeLabelData(this.getAttribute('data-quick-label') || '');
                    if (!imgPath || !rawLabel) {{
                        return;
                    }}
                    applyQuickLabelToImage(imgPath, rawLabel);
                }});
                btn.addEventListener('dblclick', function(e) {{
                    if (!isLabelRenameShortcut(e)) {{
                        return;
                    }}
                    const rawLabel = decodeLabelData(this.getAttribute('data-quick-label') || '');
                    if (!rawLabel) {{
                        return;
                    }}
                    e.preventDefault();
                    e.stopPropagation();
                    e.stopImmediatePropagation();
                    startRenameFromPresetLabel(rawLabel);
                }});
                btn.addEventListener('mousedown', function(e) {{
                    e.preventDefault();
                }});
            }});
        }}

        // ===== MULTI-SELECT SYSTEM =====
        let selectedPaths = new Set();
        const imageJumpFlashTimers = new WeakMap();
        let lastClickedIndex = -1;
        let focusedIndex = -1;
        let anchorIndex = -1;  // The starting point for shift-selection (like macOS)
        let lastShiftAnchor = -1;  // Track last shift range for smart clear
        let lastShiftEnd = -1;
        let barPresetSyncFn = null;
        let barApplyAnnotation = null;
        let selectedPresetIndex = 0;
        let activeFolderInstantOffStatus = getInitialInstantOffStatus();
        const instantOffChoiceClassMap = {{
            'Yes Video Exists': 'yes-video',
            'No Video Exists': 'no-video'
        }};

        function renderPresetGroupRows() {{
            return annotationPresetEntries.map((entry) => {{
                const groupName = entry[0] || '';
                const labels = Array.isArray(entry[1]) ? entry[1] : [];
                const table = entry[3] || '';
                const station = entry[4] || '';
                const selectedCount = Number(entry[2] || 0);
                const needsAnodeCount = tableNeedsAnodeCount(table);
                const groupClass = needsAnodeCount && !selectedCount ? 'annotation-group pending' : 'annotation-group';
                const labelButtons = labels.map((label) => {{
                    const escaped = escapeHtml(label);
                    const active = label === (selectedPresetIndex >= 0 && selectedPresetIndex < annotationFlatPresets.length ? annotationFlatPresets[selectedPresetIndex] : '') ? ' active' : '';
                    const usedCount = countImagesWithLabel(label);
                    const usedClass = usedCount > 0 ? ' used' : '';
                    const isDuplicate = isUniqueExactTableLabel(label) && usedCount > 1;
                    const duplicateClass = isDuplicate ? ' duplicate' : '';
                    const usageAlpha = usedCount > 0 ? (0.26 + Math.min(usedCount, 12) * 0.035).toFixed(2) : '0';
                    const usagePayload = encodeURIComponent(label);
                    const overlapBadge = isDuplicate
                        ? '<span class="annotation-preset-overlap" role="button" tabindex="0" '
                            + 'title="Inspect overlapping images for this label" '
                            + 'aria-label="Inspect overlapping images for this label: ' + escapeHtmlAttribute(escaped) + '" '
                            + 'data-preset-overlap="' + escapeHtmlAttribute(usagePayload) + '">overlap</span>'
                        : '';
                    const usageBadge = usedCount > 0
                        ? '<span class="annotation-preset-usage" role="button" tabindex="0" '
                            + 'title="Inspect images with this label" '
                            + 'aria-label="Inspect images with this label: ' + escapeHtmlAttribute(escaped) + '" '
                            + 'data-preset-usage="' + escapeHtmlAttribute(usagePayload) + '">' + usedCount + '</span>'
                        : '';
                    return '<button type="button" class="annotation-preset' + active + usedClass + duplicateClass + '" style="--usage-alpha:' + usageAlpha + '" data-preset="' + escaped + '">' + escaped + overlapBadge + usageBadge + '</button>';
                }}).join('');
                return '<div class="' + groupClass + '"><span class="annotation-group-title">' + escapeHtml(groupName) + '</span>' + labelButtons + '</div>';
            }}).join('');
        }}

        function renderFolderAnodeControls() {{
            const stations = ['Test Station 1', 'Test Station 2'];
            const hasAnodeTables = tablePresetOrder
                .map((tableName) => getTableFromLabel(tableName))
                .some((tableName) => tableName && tableNeedsAnodeCount(tableName));
            if (!hasAnodeTables) {{
                return '';
            }}
            const rows = stations.map((station) => {{
                const activeCount = getStationAnodeCount(station);
                const buttons = tableStationAnodeOptions.map((option) => {{
                    const active = Number(activeCount) === option ? ' active' : '';
                    return '<button type="button" class="anode-count-btn table-anode-btn' + active + '" ' +
                        'data-station="' + escapeHtml(station) + '" ' +
                        'data-count="' + option + '">' + option + '</button>';
                }}).join('');
                return '<span class="table-anode-row">' +
                        '<span class="table-anode-title">' + escapeHtml(station) + ' anodes:</span>' +
                        '<span class="table-anode-options">' + buttons + '</span>' +
                       '</span>';
            }}).join('');
            return '<span class="table-anode-rows">' + rows + '</span>';
        }}

        function refreshAnnotationPresets() {{
            rebuildAnnotationPresets();
            const currentCursor = getFolderPresetCursor();
            const tableFirstIndex = annotationFlatPresets.findIndex((item) => item.indexOf('Table 3 ') === 0);
            selectedPresetIndex = clampPresetIndex(currentCursor >= 0 ? currentCursor : tableFirstIndex, annotationFlatPresets.length);
            updateFolderPresetCursor();
        }}

        refreshAnnotationPresets();
        if (selectedPresetIndex < 0) {{
            selectedPresetIndex = 0;
        }}

        function updateFolderPresetCursor() {{
            if (!activeFolderPath) return;
            const raw = getFolderAnnotationState();
            raw[activeFolderPath] = raw[activeFolderPath] || {{}};
            raw[activeFolderPath].tableCursor = clampPresetIndex(selectedPresetIndex, annotationFlatPresets.length);
            saveFolderAnnotationState(raw);
        }}

        function setAnnotationPresetIndex(index) {{
            selectedPresetIndex = clampPresetIndex(index, annotationFlatPresets.length);
            updateFolderPresetCursor();
            return selectedPresetIndex;
        }}

        function getActivePresetLabel() {{
            if (!annotationFlatPresets.length) return '';
            return annotationFlatPresets[selectedPresetIndex];
        }}

        function cycleAnnotationPreset(step) {{
            if (!annotationFlatPresets.length) return '';
            const nextIndex = clampPresetIndex(selectedPresetIndex + step, annotationFlatPresets.length);
            setAnnotationPresetIndex(nextIndex);
            if (barPresetSyncFn) {{
                barPresetSyncFn();
            }}
            return getActivePresetLabel();
        }}

        function getContainerIndex(container) {{
            const allContainers = Array.from(parent.querySelectorAll('.img-container'));
            return allContainers.indexOf(container);
        }}

        function getContainerByIndex(index) {{
            const allContainers = parent.querySelectorAll('.img-container');
            return allContainers[index] || null;
        }}

        function getContainerByPath(targetPath) {{
            const allContainers = parent.querySelectorAll('.img-container');
            for (let i = 0; i < allContainers.length; i++) {{
                if (allContainers[i].dataset.path === targetPath) {{
                    return allContainers[i];
                }}
            }}
            const fallbackName = targetPath ? String(targetPath).split('/').pop() : '';
            if (!fallbackName) {{
                return null;
            }}
            for (let i = 0; i < allContainers.length; i++) {{
                const current = allContainers[i].dataset.path || '';
                if (current === fallbackName || current.endsWith('/' + fallbackName)) {{
                    return allContainers[i];
                }}
            }}
            return null;
        }}

        function parseLabelList(raw) {{
            if (!raw) return [];
            try {{
                const parsed = JSON.parse(raw);
                if (Array.isArray(parsed)) {{
                    const out = [];
                    const seen = new Set();
                    for (let i = 0; i < parsed.length; i++) {{
                        const item = String(parsed[i] || '').trim();
                        if (!item || seen.has(item)) continue;
                        seen.add(item);
                        out.push(item);
                    }}
                    return out;
                }}
            }} catch (err) {{
                return [];
            }}
            return [];
        }}

        function normalizeLabelArray(rawOrLabels) {{
            const labels = Array.isArray(rawOrLabels) ? rawOrLabels : parseLabelList(rawOrLabels);
            if (!Array.isArray(labels) || !labels.length) {{
                return [];
            }}
            const out = [];
            const seen = new Set();
            for (let i = 0; i < labels.length; i++) {{
                const item = String(labels[i] || '').trim();
                if (!item || seen.has(item)) continue;
                seen.add(item);
                out.push(item);
            }}
            return out;
        }}

        function normalizeSingleLabelArray(rawOrLabels) {{
            const labels = normalizeLabelArray(rawOrLabels);
            if (!Array.isArray(labels) || !labels.length) {{
                return [];
            }}
            const first = String(labels[0] || '').trim();
            return first ? [first] : [];
        }}

        const annotationRowColorMap = {{
            1: 'hsl(0, 0%, 12%)',
            2: 'hsl(0, 84%, 56%)',
            3: 'hsl(24, 94%, 53%)',
            4: 'hsl(48, 96%, 53%)',
            5: 'hsl(142, 70%, 45%)',
            6: 'hsl(30, 45%, 38%)',
            7: 'hsl(267, 84%, 51%)',
        }};

        function labelColorFromName(label) {{
            if (!label) return 'hsl(152, 72%, 42%)';
            const match = /(?:Row|MG|MD)\\s+(\\d+)/i.exec(label);
            if (match) {{
                const rowColor = annotationRowColorMap[match[1]];
                if (rowColor) return rowColor;
            }}
            let hash = 0;
            for (let i = 0; i < label.length; i++) {{
                hash = ((hash * 31) + label.charCodeAt(i)) >>> 0;
            }}
            const hue = hash % 360;
            return 'hsl(' + hue + ',72%,42%)';
        }}

        function labelGroupKey(label) {{
            const table = getTableFromLabel(label);
            if (!table) return '';
            const station = parseStationFromLabel(label || '');
            if (station) return `Table ${{table}} ${{station}}`;
            const rowMatch = /(?:Row|MG|MD)\\s+(\\d+)/i.exec(label || '');
            return rowMatch ? `Table ${{table}} Row ${{rowMatch[1]}}` : `Table ${{table}}`;
        }}

        function labelGroupColorFromName(label) {{
            const key = labelGroupKey(label) || label || '';
            const groupPalette = {{
                'Table 3 Row 1': 'hsl(205, 82%, 46%)',
                'Table 3 Row 2': 'hsl(180, 72%, 34%)',
                'Table 3 Row 3': 'hsl(330, 72%, 48%)',
                'Table 3 Row 4': 'hsl(38, 88%, 46%)',
                'Table 4 Test Station 1': 'hsl(210, 78%, 48%)',
                'Table 4 Test Station 2': 'hsl(29, 86%, 48%)',
                'Table 5 Test Station 1': 'hsl(156, 74%, 34%)',
                'Table 6 Test Station 1': 'hsl(188, 78%, 36%)',
                'Table 5 Test Station 2': 'hsl(345, 72%, 48%)',
                'Table 6 Test Station 2': 'hsl(265, 68%, 54%)',
            }};
            if (groupPalette[key]) return groupPalette[key];
            let hash = 0;
            for (let i = 0; i < key.length; i++) {{
                hash = ((hash * 33) + key.charCodeAt(i)) >>> 0;
            }}
            return 'hsl(' + (hash % 360) + ',68%,44%)';
        }}

        function normalizeHslColor(color) {{
            const match = /^hsl\\((\\d+),\\s*([\\d.]+)%,\\s*([\\d.]+)%\\)/.exec(color || '');
            if (!match) return [152, 72, 42];
            return [Number(match[1]), Number(match[2]), Number(match[3])];
        }}

        function chipColorFromName(label, index, total) {{
            const base = normalizeHslColor(labelColorFromName(label));
            const delta = (index - (Math.max(1, total) - 1) / 2) * 6;
            const lightness = Math.min(72, Math.max(30, base[2] + delta));
            return `hsl(${{base[0]}}, ${{base[1]}}%, ${{lightness}}%)`;
        }}

        function escapeHtmlAttribute(raw) {{
            return String(raw || '')
                .replace(/&/g, '&amp;')
                .replace(/"/g, '&#34;')
                .replace(/'/g, '&#39;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;');
        }}

        function renderLabelBadges(container, labels) {{
            const finalLabels = normalizeLabelArray(Array.isArray(labels) ? labels : []);
            const badgeContainer = container.querySelector('.label-badges');
            if (!badgeContainer) return;

            if (finalLabels.length > 0) {{
                container.classList.add('labeled');
                const baseColor = labelColorFromName(finalLabels[0]);
                const groupColor = labelGroupColorFromName(finalLabels[0]);
                container.style.setProperty('--annotation-color', baseColor);
                container.style.setProperty('--annotation-group-color', groupColor);
                container.dataset.annotationColor = baseColor;
                container.dataset.annotationGroupColor = groupColor;
                const maxBadges = Math.min(finalLabels.length, 3);
                const chipBadges = [];
                for (let i = 0; i < maxBadges; i++) {{
                    const labelText = finalLabels[i] || '';
                    const safeLabel = escapeHtml(labelText);
                    const safeAttr = escapeHtmlAttribute(labelText);
                    const chipColor = chipColorFromName(labelText, i, maxBadges);
                    chipBadges.push(
                        '<span class="label-chip" style="--chip-color: ' + chipColor + '" data-label="' + safeAttr + '">' +
                            '<span class="label-chip-text">' + safeLabel + '</span>' +
                            '<span class="label-chip-x" title="Remove label">×</span>' +
                        '</span>'
                    );
                }}
                if (finalLabels.length > maxBadges) {{
                    chipBadges.push('<span class="label-chip-more">+' + (finalLabels.length - maxBadges) + '</span>');
                }}
                badgeContainer.innerHTML = chipBadges.join('');
            }} else {{
                container.classList.remove('labeled');
                container.style.removeProperty('--annotation-color');
                container.style.removeProperty('--annotation-group-color');
                container.dataset.annotationColor = '';
                container.dataset.annotationGroupColor = '';
                badgeContainer.innerHTML = '';
            }}
            container.dataset.labels = JSON.stringify(finalLabels);
        }}

        function hydrateLabelBadges() {{
            parent.querySelectorAll('.img-container').forEach(container => {{
                const labels = normalizeLabelArray(clientAnnotationLabelsByPath[container.dataset.path || ''] || []);
                renderLabelBadges(container, labels);
            }});
        }}

        function escapeHtml(raw) {{
            return String(raw || '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }}

        const DRAG_BATCH = 20;

        function logClientEvent(type, detail) {{
            const payload = typeof detail === 'string' ? detail : JSON.stringify(detail || {{}});
            const logUrl = clipboardServerBase + '/log?type=' + encodeURIComponent(type) + '&detail=' + encodeURIComponent(payload);
            fetch(logUrl)
                .catch(() => {{}});
        }}

        async function doDrag(paths) {{
            if (paths.length === 0) return;
            const eventId = 'drag-' + Date.now() + '-' + Math.floor(Math.random() * 10000);
            logClientEvent('drag_button_click', {{
                event_id: eventId,
                count: paths.length,
                start_path: paths[0] || null,
                end_path: paths[paths.length - 1] || null
            }});
            const helperReady = await ensureClipboardServerReady('drag_button', true);
            if (!helperReady) {{
                const reason = clipboardApiState.lastError || 'server unavailable';
                logClientEvent('drag_server_not_ready', {{
                    event_id: eventId,
                    reason
                }});
                showCopyBanner('✗', 'Drag helper unavailable: ' + reason);
                return;
            }}
            try {{
                const response = await fetch(clipboardStartDragUrl + encodeURIComponent(paths.join('|')));
                const body = await response.text();
                let result = {{}};
                try {{
                    result = JSON.parse(body);
                }} catch (err) {{
                    logClientEvent('drag_server_parse_error', {{
                        event_id: eventId,
                        error: err.message,
                        raw_body: body.slice(0, 240)
                    }});
                    showCopyBanner('✗', 'Server parse error');
                    return;
                }}
                logClientEvent('drag_server_response', {{
                    event_id: eventId,
                    success: result.success,
                    count: result.count,
                    method: result.method || null,
                    return_code: result.return_code,
                    error: result.error,
                    request_id: result.request_id
                }});
                if (result.success) {{
                    showCopyBanner('📂', result.count + ' in Finder — drag to upload');
                }} else {{
                    const reason = result.error || 'Reveal failed';
                    showCopyBanner('✗', 'Reveal failed: ' + reason);
                }}
            }} catch (err) {{
                logClientEvent('drag_server_error', {{
                    event_id: eventId,
                    error: err.message
                }});
                console.error('Drag error:', err);
                showCopyBanner('✗', 'Server error: ' + err.message);
            }}
        }}

        function getSelectionAnnotationState() {{
            const selectedContainers = [];
            selectedPaths.forEach((path) => {{
                const container = getContainerByPath(path);
                if (container) {{
                    selectedContainers.push(container);
                }}
            }});

            if (!selectedContainers.length) {{
                return {{
                    mode: 'none',
                    labels: [],
                    mixed: false,
                    displayLabel: '',
                    message: '',
                }};
            }}

            const parsedLabels = selectedContainers.map((container) => normalizeLabelArray(container.dataset.labels || '[]'));
            const hasAnyLabel = parsedLabels.some((labels) => labels.length > 0);
            if (!hasAnyLabel) {{
                return {{
                    mode: 'unlabeled',
                    labels: [],
                    mixed: false,
                    displayLabel: '',
                    message: '',
                }};
            }}

            const reference = parsedLabels[0] || [];
            const same = parsedLabels.every((labels) => {{
                if (labels.length !== reference.length) return false;
                for (let i = 0; i < reference.length; i++) {{
                    if (labels[i] !== reference[i]) return false;
                }}
                return true;
            }});

            if (!same) {{
                return {{
                    mode: 'mixed',
                    labels: reference,
                    mixed: true,
                    displayLabel: '',
                    message: 'Mixed labels in selection',
                }};
            }}

            return {{
                mode: 'single',
                labels: reference,
                mixed: false,
                displayLabel: reference[0] || '',
                message: '',
            }};
        }}

        function isExpandedLabelSelectionMatch(baseLabel) {{
            const paths = getSelectedPathsInOrder();
            if (!paths.length) return false;
            const expected = expandSelectionLabels(baseLabel, paths.length);
            const current = getOrderedSelectionLabels();
            const allCurrentEqual = current.every((label) => label === baseLabel);
            if (allCurrentEqual && expected.length >= paths.length) {{
                return true;
            }}
            if (expected.length !== paths.length) {{
                return false;
            }}
            for (let i = 0; i < expected.length; i++) {{
                if ((current[i] || '') !== expected[i]) {{
                    return false;
                }}
            }}
            return true;
        }}

        function getMissingSlotItems() {{
            const labels = Array.isArray(annotationFlatPresets) ? annotationFlatPresets : [];
            return labels.map((label, index) => {{
                const slotKey = requiredSlotKeyForLabel(label);
                const emptyRecord = slotKey && folderEmptySlotState ? folderEmptySlotState[slotKey] : null;
                const count = countImagesWithLabel(label);
                const empty = Boolean(emptyRecord);
                return {{
                    index,
                    label,
                    slotKey,
                    count,
                    empty,
                    state: empty ? 'empty' : (count > 0 ? 'filled' : 'missing'),
                }};
            }}).filter((item) => item.empty || item.count <= 0);
        }}

        function getMissingSlotSummary() {{
            const items = getMissingSlotItems();
            const missingCount = items.filter((item) => item.state === 'missing').length;
            const emptyCount = items.filter((item) => item.state === 'empty').length;
            return {{
                items,
                missingCount,
                emptyCount,
                active: missingCount > 0,
            }};
        }}

        function renderMissingSlotsIndicator() {{
            const summary = getMissingSlotSummary();
            const missingTitle = summary.missingCount > 0
                ? 'Inspect required table slots without an image and mark them empty (-)'
                : 'No unhandled missing table slots';
            const emptyTitle = summary.emptyCount > 0
                ? 'Inspect table slots explicitly marked empty (-)'
                : 'No table slots are marked empty (-)';
            return '<button type="button" class="missing-slots-btn ' + (summary.missingCount > 0 ? 'active' : 'complete') + '" '
                + 'data-missing-kind="missing" title="' + escapeHtmlAttribute(missingTitle) + '">Missing: ' + summary.missingCount + '</button>'
                + '<button type="button" class="missing-slots-btn ' + (summary.emptyCount > 0 ? 'active empty-active' : 'complete empty-none') + '" '
                + 'data-missing-kind="empty" title="' + escapeHtmlAttribute(emptyTitle) + '">Empty (-): ' + summary.emptyCount + '</button>';
        }}

        function openMissingSlotsPanel() {{
            setSelectionDebugPanelMode('missing-slots');
            setSelectionDebugPanelVisible(true);
        }}

        async function setRequiredSlotEmpty(slotKey, label, empty) {{
            const normalizedSlotKey = normalizeRequiredSlotKey(slotKey || requiredSlotKeyForLabel(label));
            if (!activeFolderPath || !normalizedSlotKey) {{
                return;
            }}
            try {{
                const query = new URLSearchParams({{
                    action: 'set-empty-slot',
                    folder: activeFolderPath,
                    slot_key: normalizedSlotKey,
                    label: String(label || ''),
                    empty: empty ? '1' : '0',
                }}).toString();
                const response = await fetch(clipboardServerBase + '/folder-state?' + query);
                const data = await response.json().catch(() => ({{}}));
                if (!data || !data.success) {{
                    showCopyBanner('✗', 'Could not save empty slot');
                    return;
                }}
                const nextState = data.state && data.state.empty_slots ? data.state.empty_slots : {{}};
                folderEmptySlotState = normalizeFolderEmptySlotState(nextState);
                showCopyBanner(empty ? '-' : '✓', empty ? 'Marked empty' : 'Cleared empty');
                refreshAnnotationPresets();
                updateSelectionCount('', true);
                if (isSelectionDebugPanelVisible()) {{
                    renderSelectionDebugPanel();
                }}
            }} catch (err) {{
                showCopyBanner('✗', 'Empty-slot save failed');
                logClientEvent('empty_slot_persist_error', {{
                    folder: activeFolderPath,
                    slot_key: normalizedSlotKey,
                    error: err && err.message ? err.message : String(err),
                }});
            }}
        }}

        function bindMissingSlotsIndicator(scope) {{
            const root = scope && scope.querySelector ? scope : parent;
            if (!root.querySelectorAll) {{
                return;
            }}
            root.querySelectorAll('.missing-slots-btn').forEach((button) => {{
                if (!button || button.dataset.missingSlotsBound === '1') {{
                    return;
                }}
                button.dataset.missingSlotsBound = '1';
                const open = (event) => {{
                    if (event) {{
                        event.preventDefault();
                        event.stopPropagation();
                    }}
                    openMissingSlotsPanel();
                    if (isSelectionDebugPanelVisible()) {{
                        renderSelectionDebugPanel();
                    }}
                }};
                button.addEventListener('click', open);
                button.addEventListener('keydown', function(event) {{
                    if (event.key === 'Enter' || event.key === ' ') {{
                        open(event);
                    }}
                }});
            }});
        }}

        function syncAnnotationPresetButtons(presetButtons, activeLabel) {{
            if (!presetButtons || !presetButtons.length) return;
            presetButtons.forEach((btn) => {{
                btn.classList.toggle('active', Boolean(activeLabel) && btn.dataset.preset === activeLabel);
            }});
        }}

        function renderGlobalOverlapIndicator() {{
            const summary = getGlobalOverlapSummary();
            const active = Boolean(summary.active);
            const count = Number(summary.count || 0);
            const label = active ? ('Overlaps: on ' + count) : 'Overlaps: off';
            const title = active
                ? 'Inspect all images with overlapping labels or duplicate unique-slot labels'
                : 'No overlapping labels detected in this folder';
            return '<button type="button" class="global-overlap-btn ' + (active ? 'active' : 'inactive') + '" '
                + 'data-global-overlap-count="' + count + '" '
                + 'title="' + escapeHtmlAttribute(title) + '">' + escapeHtml(label) + '</button>';
        }}

        function bindGlobalOverlapIndicator(scope) {{
            const root = scope && scope.querySelector ? scope : parent;
            const button = root.querySelector ? root.querySelector('.global-overlap-btn') : null;
            if (!button || button.dataset.globalOverlapBound === '1') {{
                return;
            }}
            button.dataset.globalOverlapBound = '1';
            const open = (event) => {{
                if (event) {{
                    event.preventDefault();
                    event.stopPropagation();
                }}
                const summary = getGlobalOverlapSummary();
                if (!summary.active) {{
                    hideSelectionDebugPanel();
                    showCopyBanner('✓', 'No overlapping labels');
                    return;
                }}
                openGlobalOverlapPanel();
                if (isSelectionDebugPanelVisible()) {{
                    renderSelectionDebugPanel();
                }}
            }};
            button.addEventListener('mouseenter', function() {{
                if (getGlobalOverlapSummary().active) {{
                    highlightGlobalOverlapImages();
                }}
            }});
            button.addEventListener('mouseleave', function() {{
                clearLabelInspectionHighlights();
            }});
            button.addEventListener('focus', function() {{
                if (getGlobalOverlapSummary().active) {{
                    highlightGlobalOverlapImages();
                }}
            }});
            button.addEventListener('blur', function() {{
                clearLabelInspectionHighlights();
            }});
            button.addEventListener('click', open);
            button.addEventListener('keydown', function(event) {{
                if (event.key === 'Enter' || event.key === ' ') {{
                    open(event);
                }}
            }});
        }}

        function bindSelectionBarFoldButton(bar) {{
            if (!bar || !bar.querySelector) {{
                return;
            }}
            const foldButton = bar.querySelector('.selection-bar-fold-toggle');
            if (!foldButton || foldButton.dataset.foldBound === '1') {{
                return;
            }}
            foldButton.dataset.foldBound = '1';
            const storageKey = 'report-labeler-selection-bar-folded-v1';
            const setFolded = (nextFolded) => {{
                if (parent.body) {{
                    parent.body.classList.toggle('selection-bar-folded', Boolean(nextFolded));
                }}
                try {{
                    parent.defaultView.localStorage.setItem(storageKey, Boolean(nextFolded) ? '1' : '0');
                }} catch (err) {{
                    // Non-critical: folding still works even if localStorage is unavailable.
                }}
            }};
            const syncLabel = () => {{
                const folded = parent.body && parent.body.classList.contains('selection-bar-folded');
                foldButton.textContent = folded ? '⌃' : '⌄';
                foldButton.setAttribute('title', folded ? 'Show label bar' : 'Hide label bar');
            }};
            try {{
                if (parent.defaultView.localStorage.getItem(storageKey) === '1' && parent.body) {{
                    parent.body.classList.add('selection-bar-folded');
                }}
            }} catch (err) {{
                // Non-critical: default to expanded.
            }}
              foldButton.addEventListener('click', function(event) {{
                  event.preventDefault();
                  event.stopPropagation();
                  const folded = parent.body && parent.body.classList.contains('selection-bar-folded');
                  setFolded(!folded);
                  syncLabel();
                  if (parent.defaultView && typeof parent.defaultView.__reportLabelerSyncBottomDockLayout === 'function') {{
                      parent.defaultView.__reportLabelerSyncBottomDockLayout();
                  }}
              }});
            if (bar.dataset.foldRestoreBound !== '1') {{
                bar.dataset.foldRestoreBound = '1';
                bar.addEventListener('click', function(event) {{
                    if (!parent.body || !parent.body.classList.contains('selection-bar-folded')) {{
                        return;
                    }}
                    event.preventDefault();
                    event.stopPropagation();
                      setFolded(false);
                      syncLabel();
                      if (parent.defaultView && typeof parent.defaultView.__reportLabelerSyncBottomDockLayout === 'function') {{
                          parent.defaultView.__reportLabelerSyncBottomDockLayout();
                      }}
                  }});
              }}
            syncLabel();
        }}

        function updateSelectionCount(forcedLabel = null, skipAuthoritativeRefresh = false) {{
            let bar = parent.querySelector('.selection-bar');
            if (!bar) {{
                bar = document.createElement('div');
                bar.className = 'selection-bar';
                parent.body.appendChild(bar);
            }}
              if (bar.parentNode !== parent.body) {{
                  parent.body.appendChild(bar);
              }}
              bar.style.display = 'flex';
              if (parent.defaultView && typeof parent.defaultView.__reportLabelerSyncBottomDockLayout === 'function') {{
                  parent.defaultView.__reportLabelerSyncBottomDockLayout();
              }}

              syncSelectionStateWithDom();
            const count = selectedPaths.size;
            const instantOffStatusValue = instantOffStatusChoices.includes(activeFolderInstantOffStatus)
                ? activeFolderInstantOffStatus
                : (instantOffStatusChoices.length ? instantOffStatusChoices[0] : '');
            const instantOffPrimaryLabel = instantOffStatusChoices[0];
            const instantOffVideoName = folderInstantOffVideo && instantOffStatusValue === instantOffPrimaryLabel
                ? folderInstantOffVideo
                : '';
            const instantOffStatusClass = instantOffChoiceClassMap[instantOffStatusValue] || '';
            const instantOffRow = instantOffStatusValue
                ? '<div class="instant-off-row"><span class="instant-off-title">Instant Off:</span><span class="instant-off-options">' +
                    '<span class="instant-off-btn ' + instantOffStatusClass + ' active">' +
                    escapeHtml(instantOffStatusValue + (instantOffVideoName ? (' (' + instantOffVideoName + ')') : '')) + '</span>' +
                  '</span></div>'
                : '';
            const tableAnodeControls = renderFolderAnodeControls();
            const globalOverlapIndicator = renderGlobalOverlapIndicator();
            const missingSlotsIndicator = renderMissingSlotsIndicator();

            if (count === 0) {{
                const presetRows = renderPresetGroupRows();
                bar.innerHTML = `
                    <button type="button" class="selection-bar-fold-toggle" title="Hide/show label bar">⌄</button>
                    <span class="count-text">0 selected</span>
                    ${{instantOffRow}}
                    ${{tableAnodeControls}}
                    ${{globalOverlapIndicator}}
                    ${{missingSlotsIndicator}}
                    <button class="copy-selected-btn" disabled>Copy</button>
                    <button class="selection-inspect-btn" disabled>Inspect</button>
                    <span class="annotation-mixed-note" style="display:none;"></span>
                    <input class="annotation-input" placeholder="Type custom label (Enter to apply)" value="" disabled />
                    <div class="annotation-presets">${{presetRows}}</div>
                    <button class="clear-btn">✕</button>
                `;

                const copyButton = bar.querySelector('.copy-selected-btn');
                const inspectButton = bar.querySelector('.selection-inspect-btn');
                const clearButton = bar.querySelector('.clear-btn');
                if (copyButton) {{
                    copyButton.addEventListener('click', function() {{
                        showCopyBanner('✗', 'Select image(s) first');
                    }});
                }}
                if (inspectButton) {{
                    inspectButton.addEventListener('click', function() {{
                        showCopyBanner('✗', 'Select image(s) first');
                    }});
                }}
                bindAnnotationPresetUsageButtons(bar);
                bindAnnotationPresetOverlapButtons(bar);
                bindGlobalOverlapIndicator(bar);
                bindMissingSlotsIndicator(bar);
                bindSelectionBarFoldButton(bar);
                hydrateAutoNextLabelHints();
                if (clearButton) {{
                    clearButton.addEventListener('click', function() {{
                        clearSelection();
                    }});
                }}

                bar.querySelectorAll('.annotation-preset').forEach((btn) => {{
                    if (!btn || btn.dataset.presetBoundIdle === '1') {{
                        return;
                    }}
                    btn.dataset.presetBoundIdle = '1';
                    btn.addEventListener('click', function() {{
                        const label = this.dataset.preset;
                        if (!label) {{
                            return;
                        }}
                        const presetIndex = annotationFlatPresets.indexOf(label);
                        if (presetIndex >= 0) {{
                            setAnnotationPresetIndex(presetIndex);
                        }}
                    }});
                }});

                bar.querySelectorAll('.table-anode-btn').forEach((button) => {{
                    button.addEventListener('click', function() {{
                        const station = (this.dataset.station || '').trim();
                        const count = Number(this.dataset.count);
                        if (!station || !Number.isInteger(count)) {{
                            return;
                        }}
                        const current = getStationAnodeCount(station);
                        const next = current === count ? 0 : count;
                        setStationAnodeCount(station, next);
                        refreshAnnotationPresets();
                        updateSelectionCount();
                    }});
                }});

                barApplyAnnotation = null;
                bar.classList.add('visible');
                if (isSelectionDebugPanelVisible() && selectionDebugPanelMode === 'selection') {{
                    hideSelectionDebugPanel();
                }} else {{
                    scheduleSelectionDebugPanelRefresh();
                }}
                return;
            }}

            const state = getSelectionAnnotationState();
            const selectedLabelDisplay = state.mode === 'single' ? state.displayLabel : '';
            const stationAwarePresetLabel = annotationFlatPresets.indexOf(selectedLabelDisplay) >= 0
                ? selectedLabelDisplay
                : normalizeStationLabel(selectedLabelDisplay);
            const forcedActive = typeof forcedLabel === 'string' && forcedLabel ? forcedLabel : '';
            const selectedLabel = selectedLabelDisplay;
            const selectedIsPreset = stationAwarePresetLabel && annotationFlatPresets.indexOf(stationAwarePresetLabel) >= 0;
            if (!forcedActive && state.mode === 'single' && selectedIsPreset) {{
                setAnnotationPresetIndex(annotationFlatPresets.indexOf(stationAwarePresetLabel));
            }}
            const defaultInputValue = state.mode === 'single' && state.labels.length > 0 ? selectedLabel : (state.mode === 'unlabeled' ? getActivePresetLabel() : '');
            const inputValue = forcedActive || defaultInputValue;
            const selectionMessage = state.mode === 'mixed' ? state.message : '';

            const numBatches = Math.ceil(count / DRAG_BATCH);
            let dragBtns = '';
            if (numBatches === 1) {{
                dragBtns = '<button class="drag-btn" data-batch="0" data-start="0" data-end="' + count + '">Drag (' + count + ')</button>';
            }} else {{
                for (let i = 0; i < numBatches; i++) {{
                    const start = i * DRAG_BATCH;
                    const end = Math.min(start + DRAG_BATCH, count);
                    const batchSize = end - start;
                    dragBtns += '<button class="drag-btn" data-batch="' + (i + 1) + '" data-start="' + start + '" data-end="' + end + '">Drag ' + (i + 1) + ' (' + batchSize + ')</button>';
                }}
            }}

            const effectivePresetHighlight = state.mode === 'single' && selectedIsPreset
                ? stationAwarePresetLabel
                : getActivePresetLabel();
            const presetHighlightLabel = effectivePresetHighlight || '';
            const presetRows = renderPresetGroupRows();

            bar.innerHTML = `
                <button type="button" class="selection-bar-fold-toggle" title="Hide/show label bar">⌄</button>
                <span class="count-text">${{count}} selected</span>
                ${{dragBtns}}
                ${{instantOffRow}}
                ${{tableAnodeControls}}
                ${{globalOverlapIndicator}}
                ${{missingSlotsIndicator}}
                <button class="copy-selected-btn">Copy</button>
                <button class="selection-inspect-btn">Inspect</button>
                <span class="annotation-mixed-note" role="button" tabindex="${{selectionMessage ? '0' : '-1'}}" title="Inspect selected images" style="display:${{selectionMessage ? 'inline-flex' : 'none'}};">${{selectionMessage}}</span>
                <input class="annotation-input" placeholder="Type custom label (Enter to apply)" value="${{inputValue}}" />
                <div class="annotation-presets">${{presetRows}}</div>
                <button class="clear-btn">✕</button>
            `;

            const annotationInput = bar.querySelector('.annotation-input');
            const presetButtons = Array.from(bar.querySelectorAll('.annotation-preset'));
            const tableAnodeButtons = Array.from(bar.querySelectorAll('.table-anode-btn'));
            const inspectBtn = bar.querySelector('.selection-inspect-btn');
            const mixedNote = bar.querySelector('.annotation-mixed-note');
            bindAnnotationPresetUsageButtons(bar);
            bindAnnotationPresetOverlapButtons(bar);
            bindGlobalOverlapIndicator(bar);
            bindMissingSlotsIndicator(bar);
            bindSelectionBarFoldButton(bar);
            hydrateAutoNextLabelHints();
            if (!skipAuthoritativeRefresh) {{
                refreshAuthoritativeAnnotationState().then((changed) => {{
                    if (changed && selectedPaths.size > 0) {{
                        updateSelectionCount(forcedLabel, true);
                    }}
                }});
            }}
            const syncPresetButtons = () => syncAnnotationPresetButtons(
                presetButtons,
                state.mode === 'mixed' ? '' : (forcedActive || getActivePresetLabel())
            );
            tableAnodeButtons.forEach((button) => {{
                button.addEventListener('click', function() {{
                    const station = (this.dataset.station || '').trim();
                    const count = Number(this.dataset.count);
                    if (!station || !Number.isInteger(count)) {{
                        return;
                    }}
                    const current = getStationAnodeCount(station);
                    const next = current === count ? 0 : count;
                    setStationAnodeCount(station, next);
                    refreshAnnotationPresets();
                    updateSelectionCount();
                }});
            }});
            if (inspectBtn) {{
                inspectBtn.addEventListener('click', function() {{
                    if (isSelectionDebugPanelVisible() && selectionDebugPanelMode === 'selection') {{
                        hideSelectionDebugPanel();
                        return;
                    }}
                    openSelectionDebugPanel();
                    if (isSelectionDebugPanelVisible()) {{
                        renderSelectionDebugPanel();
                    }}
                }});
            }}
            if (mixedNote) {{
                const openMixedSelectionPanel = (event) => {{
                    if (event) {{
                        event.preventDefault();
                        event.stopPropagation();
                    }}
                    openSelectionDebugPanel();
                    if (isSelectionDebugPanelVisible()) {{
                        renderSelectionDebugPanel();
                    }}
                }};
                mixedNote.addEventListener('click', openMixedSelectionPanel);
                mixedNote.addEventListener('keydown', (event) => {{
                    if (event.key === 'Enter' || event.key === ' ') {{
                        openMixedSelectionPanel(event);
                    }}
                }});
            }}
            async function applyAnnotationAction(explicitAction = null, explicitLabel = null) {{
                if (!isActiveScriptInstance()) {{
                    return;
                }}
                const stateNow = getSelectionAnnotationState();
                const nextLabel = (explicitLabel !== null
                    ? explicitLabel
                    : ((annotationInput ? annotationInput.value.trim() : '') || getActivePresetLabel() || '')
                ).trim();
                const selectedOrderedPaths = getSelectedPathsInOrder();
                if (!selectedOrderedPaths.length) {{
                    return;
                }}
                if (!nextLabel) {{
                    showCopyBanner('🏷', 'Choose a label first');
                    return;
                }}

                const tableSelectionCap = getTableSelectionCap(getTableFromLabel(nextLabel));
                const stationSelectionCap = getStationSelectionCap(nextLabel);
                const effectiveSelectionCap = tableSelectionCap || stationSelectionCap || null;
                const orderedPaths = effectiveSelectionCap
                    ? selectedOrderedPaths.slice(0, effectiveSelectionCap)
                    : selectedOrderedPaths.slice();
                const ignoredSelectionCount = selectedOrderedPaths.length - orderedPaths.length;
                const expandedLabels = expandSelectionLabels(nextLabel, orderedPaths.length);
                const isTableSelectionLabel = Boolean(getTableFromLabel(nextLabel));
                const hasSequencedTableLabels = isTableSelectionLabel && !tableSelectionCap && expandedLabels.length > 1;
                const sequencedMatchCount = hasSequencedTableLabels
                    ? Math.min(orderedPaths.length, expandedLabels.length)
                    : orderedPaths.length;
                const targetPaths = orderedPaths.slice(0, sequencedMatchCount);
                const sequencedIgnoredSelectionCount = orderedPaths.length - targetPaths.length;
                const effectiveIgnoredSelectionCount = ignoredSelectionCount + sequencedIgnoredSelectionCount;
                const requestLabels = targetPaths.map(() => nextLabel);
                const normalizedAction = (explicitAction || '').toLowerCase();
                const action = normalizedAction
                    ? normalizedAction
                    : (isExpandedLabelSelectionMatch(nextLabel) ? 'remove' : 'set');
                if (!targetPaths.length) {{
                    return;
                }}
                if (hasSequencedTableLabels && expandedLabels.length < orderedPaths.length) {{
                    showCopyBanner(
                        '🏷',
                        'Only ' + expandedLabels.length + ' labels matched for ' + nextLabel + '; '
                        + sequencedIgnoredSelectionCount + ' image(s) skipped'
                    );
                }}

                if (hasSequencedTableLabels) {{
                    requestLabels.length = 0;
                    requestLabels.push(...expandedLabels.slice(0, sequencedMatchCount));
                }}
                if (action !== 'remove') {{
                    const uniqueConflict = findUniqueLabelConflict(requestLabels, targetPaths);
                    if (uniqueConflict) {{
                        const suffix = uniqueConflict.reason === 'selection'
                            ? ' already appears inside this selection'
                            : ' already exists on another image';
                        showCopyBanner('🏷', 'Max 1 image for ' + uniqueConflict.label + '; ' + suffix);
                        return;
                    }}
                }}

                const beforeHistory = captureAnnotationSnapshot(targetPaths);
                const eventId = 'annotate-' + Date.now() + '-' + Math.floor(Math.random() * 10000);
                const requestSuccess = targetPaths.map(() => false);
                const requests = targetPaths.map((path, index) => {{
                    const label = requestLabels[index];
                    if (!label) {{
                        return Promise.resolve({{
                            path: path || '',
                            label: '',
                            requestIndex: index,
                            data: {{ success: false, error: 'missing_label' }},
                        }});
                    }}
                    return fetch(clipboardServerBase + '/annotations?' + new URLSearchParams({{
                        action,
                        paths: path,
                        labels: label,
                    }}).toString()).then(async (response) => {{
                        const data = await response.json();
                        return {{ path, label, requestIndex: index, data }};
                    }});
                }});

                try {{
                    const results = await Promise.all(requests);
                    let totalUpdated = 0;
                    let anySuccess = false;
                    let anyFailed = false;

                    results.forEach((result) => {{
                        const response = result.data;
                        const pathPayload = result.path || '';
                        const labelPayload = result.label || expandedLabels[0];
                        const requestIndex = Number.isInteger(result.requestIndex) ? result.requestIndex : -1;
                        const hasValidRequestIndex = requestIndex >= 0 && requestIndex < requestSuccess.length;

                        if (!response || !response.success) {{
                            anyFailed = true;
                            return;
                        }}

                        anySuccess = true;
                        if (hasValidRequestIndex) {{
                            requestSuccess[requestIndex] = true;
                        }}
                        totalUpdated += Number(response.updated_count || 0);
                        if (response.label_counts && typeof response.label_counts === 'object') {{
                            clientAnnotationLabelCounts = Object.assign({{}}, response.label_counts);
                        }}

                        const annotations = (response.annotations && typeof response.annotations === 'object') ? response.annotations : null;
                        if (annotations && Object.keys(annotations).length > 0) {{
                            if (pathPayload) {{
                                const container = getContainerByPath(pathPayload);
                                if (container) {{
                                    const labels = annotations[pathPayload] || [labelPayload];
                                    setClientAnnotationPathLabels(pathPayload, labels);
                                    renderLabelBadges(container, labels);
                                }}
                            }}
                        }} else if (pathPayload) {{
                            anyFailed = true;
                        }}
                    }});

                    if (anySuccess) {{
                        targetPaths.forEach((path, index) => {{
                            if (!requestSuccess[index]) return;
                            const targetLabel = requestLabels[index];
                            const container = getContainerByPath(path);
                            if (!container) return;
                            renderLabelBadges(container, clientAnnotationLabelsByPath[path] || []);
                        }});
                    }}

                    if (anyFailed && !anySuccess) {{
                        logClientEvent('annotation_apply_failed', {{
                            event_id: eventId,
                            action,
                            label: nextLabel,
                            count: targetPaths.length,
                        }});
                        showCopyBanner('✗', 'Annotation failed');
                        return;
                    }}

	                    updateSelectionCount(nextLabel);
                    pushAnnotationHistory(beforeHistory, captureAnnotationSnapshot(targetPaths), (action === 'remove' ? 'remove ' : 'apply ') + nextLabel);

	                    const actionText = action === 'remove' ? 'Removed' : 'Updated';
	                    const ignoredSuffix = effectiveIgnoredSelectionCount > 0 ? ' (' + effectiveIgnoredSelectionCount + ' ignored)' : '';
	                    showCopyBanner('🏷', actionText + ' ' + totalUpdated + ' image(s)' + ignoredSuffix);
                    logClientEvent('annotation_apply', {{
                        event_id: eventId,
                        action,
                        label: nextLabel,
                        state: stateNow.mode,
                        count: targetPaths.length,
                        updated: totalUpdated,
                        expanded: expandedLabels,
                    }});
                }} catch (err) {{
                    logClientEvent('annotation_apply_error', {{
                        event_id: eventId,
                        action,
                        error: err.message,
                    }});
                    console.error('Annotation error:', err);
                    showCopyBanner('✗', 'Annotation failed');
                }}
            }}

            presetButtons.forEach((btn) => {{
                btn.addEventListener('dblclick', function(e) {{
                    if (!isLabelRenameShortcut(e)) {{
                        return;
                    }}
                    const label = this.dataset.preset;
                    if (!label) {{
                        return;
                    }}
                    e.preventDefault();
                    e.stopPropagation();
                    startRenameFromPresetLabel(label);
                }});
                btn.addEventListener('click', function(e) {{
                    if (e && isLabelRenameShortcut(e)) {{
                        return;
                    }}
                    if (e && e.detail > 1) {{
                        return;
                    }}
                    const label = this.dataset.preset;
                    const presetIndex = annotationFlatPresets.indexOf(label);
                    const selectedLabels = getOrderedSelectionLabels();
                    const removeCurrent = (selectedLabels.length === 1 && selectedLabels[0] === label)
                        || isExpandedLabelSelectionMatch(label)
                        || (selectedLabels.length === 1 && isExpandedLabelSelectionMatch(normalizeStationLabel(label)));
                    if (presetIndex >= 0) {{
                        setAnnotationPresetIndex(presetIndex);
                        syncPresetButtons();
                    }}
                    if (annotationInput) {{
                        annotationInput.value = label;
                    }}
                    if (selectedPaths.size > 0) {{
                        const action = removeCurrent ? 'remove' : 'set';
                        applyAnnotationAction(action, label);
                    }}
                }});
            }});
            syncPresetButtons();
            barPresetSyncFn = syncPresetButtons;

            bar.querySelectorAll('.drag-btn').forEach((btn) => {{
                btn.addEventListener('click', function() {{
                    const paths = Array.from(selectedPaths);
                    const start = parseInt(this.dataset.start);
                    const end = parseInt(this.dataset.end);
                    doDrag(paths.slice(start, end));
                    const clicks = (parseInt(this.dataset.clicks) || 0) + 1;
                    this.dataset.clicks = clicks;
                    this.classList.remove('clicked-1', 'clicked-2');
                    if (clicks >= 2) {{
                        this.classList.add('clicked-2');
                    }} else {{
                        this.classList.add('clicked-1');
                    }}
                }});
            }});

            if (annotationInput) {{
                annotationInput.addEventListener('keydown', function(e) {{
                    if (e.key === 'Enter') {{
                        e.preventDefault();
                        applyAnnotationAction();
                    }}
                }});
            }}

            bar.querySelector('.copy-selected-btn').addEventListener('click', async function() {{
                const paths = Array.from(selectedPaths);
                const eventId = 'copy-' + Date.now() + '-' + Math.floor(Math.random() * 10000);
                logClientEvent('copy_button_click', {{
                    event_id: eventId,
                    count: paths.length,
                    start_path: paths[0] || null,
                    end_path: paths[paths.length - 1] || null
                }});
                try {{
                    const response = await fetch(clipboardServerBase + '/copy-files-to-clipboard?paths=' + encodeURIComponent(paths.join('|')));
                    const result = await response.json();
                    logClientEvent('copy_button_response', {{
                        event_id: eventId,
                        success: result.success,
                        return_code: result.return_code,
                        error: result.error,
                        request_id: result.request_id
                    }});
                    if (result.success) {{
                        showCopyBanner('📋', result.count + ' file(s) copied');
                    }}
                }} catch (err) {{
                    logClientEvent('copy_button_error', {{
                        event_id: eventId,
                        error: err.message
                    }});
                    showCopyBanner('✗', 'Failed to copy');
                }}
            }});

            bar.querySelector('.clear-btn').addEventListener('click', function() {{
                clearSelection();
            }});

            barApplyAnnotation = applyAnnotationAction;
            bar.classList.add('visible');
            scheduleSelectionDebugPanelRefresh();
        }}

        function toggleSelect(container, imgPath) {{
            if (selectedPaths.has(imgPath)) {{
                selectedPaths.delete(imgPath);
                container.classList.remove('selected');
            }} else {{
                selectedPaths.add(imgPath);
                container.classList.add('selected');
            }}
            updateSelectionCount();
        }}

        function selectOnly(container, imgPath) {{
            // Clear all selections
            parent.querySelectorAll('.img-container.selected').forEach(el => {{
                el.classList.remove('selected');
            }});
            selectedPaths.clear();
            // Select this one
            selectedPaths.add(imgPath);
            container.classList.add('selected');
            updateSelectionCount();
        }}

        function selectRange(fromIndex, toIndex) {{
            const start = Math.min(fromIndex, toIndex);
            const end = Math.max(fromIndex, toIndex);
            for (let i = start; i <= end; i++) {{
                const container = getContainerByIndex(i);
                if (container) {{
                    const path = container.dataset.path;
                    if (path) {{
                        selectedPaths.add(path);
                        container.classList.add('selected');
                    }}
                }}
            }}
            updateSelectionCount();
        }}

        function clearSelection() {{
            parent.querySelectorAll('.img-container.selected').forEach(el => {{
                el.classList.remove('selected');
            }});
            selectedPaths.clear();
            lastShiftAnchor = -1;
            lastShiftEnd = -1;
            updateSelectionCount();
        }}

        function removeLabelFromImagePath(path, label) {{
            if (!isActiveScriptInstance()) {{
                return;
            }}
            const normalizedPath = (path || '').trim();
            const normalizedLabel = (label || '').trim();
            if (!normalizedPath || !normalizedLabel) {{
                return;
            }}
            const beforeHistory = captureAnnotationSnapshot([normalizedPath]);
            const eventId = 'remove-chip-' + Date.now() + '-' + Math.floor(Math.random() * 10000);
            const query = new URLSearchParams({{
                action: 'remove',
                paths: normalizedPath,
                labels: normalizedLabel,
            }}).toString();

            fetch(clipboardServerBase + '/annotations?' + query).then(async (response) => {{
                const data = await response.json();
                if (!data || !data.success) {{
                    logClientEvent('label_chip_remove_failed', {{
                        event_id: eventId,
                        path: normalizedPath,
                        label: normalizedLabel,
                        response: data,
                    }});
                    showCopyBanner('✗', 'Remove failed');
                    return;
                }}

                if (data.label_counts && typeof data.label_counts === 'object') {{
                    clientAnnotationLabelCounts = Object.assign({{}}, data.label_counts);
                }}

                const nextLabels = data.annotations && data.annotations[normalizedPath]
                    ? normalizeLabelArray(data.annotations[normalizedPath])
                    : [];
                setClientAnnotationPathLabels(normalizedPath, nextLabels);
                const container = getContainerByPath(normalizedPath);
                if (container) {{
                    renderLabelBadges(container, nextLabels);
                }}
                pushAnnotationHistory(beforeHistory, captureAnnotationSnapshot([normalizedPath]), 'remove ' + normalizedLabel);
                updateSelectionCount('', true);
                hydrateAutoNextLabelHints();
                scheduleSelectionDebugPanelRefresh();
                logClientEvent('label_chip_removed', {{
                    event_id: eventId,
                    path: normalizedPath,
                    label: normalizedLabel,
                    remaining: nextLabels.length,
                }});
                showCopyBanner('🏷', 'Removed ' + normalizedLabel);
            }}).catch((err) => {{
                logClientEvent('label_chip_remove_error', {{
                    event_id: eventId,
                    path: normalizedPath,
                    label: normalizedLabel,
                    error: err.message,
                }});
                showCopyBanner('✗', 'Remove failed');
                console.error('Label remove error:', err);
            }});
        }}

        hydrateLabelBadges();
        hydrateAutoNextLabelHints();

        function setFocus(index) {{
            // Remove old focus indicator
            parent.querySelectorAll('.img-container.focused').forEach(el => {{
                el.classList.remove('focused');
                el.style.boxShadow = '';
            }});
            focusedIndex = index;
            const container = getContainerByIndex(index);
            if (container) {{
                container.classList.add('focused');
                container.style.boxShadow = 'inset 0 0 0 2px #2196F3';
                container.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
            }}
        }}

        // Handle click for selection
        function handleContainerClick(e) {{
            if (!isActiveScriptInstance()) {{
                return;
            }}
            if (!e || e.__reportLabelerContainerHandled) {{
                return;
            }}
            e.__reportLabelerContainerHandled = true;
            const clickTarget = e.target && e.target.nodeType === 3 ? e.target.parentElement : e.target;
            const container = clickTarget && clickTarget.closest ? clickTarget.closest('.img-container') : null;
            if (!container) return;
            const imgPath = container.dataset.path;
            if (!imgPath) return;
            const labelDeleteTarget = clickTarget && clickTarget.closest ? clickTarget.closest('.label-chip-x') : null;
            if (labelDeleteTarget) {{
                const chip = clickTarget.closest('.label-chip');
                const label = chip && chip.dataset ? chip.dataset.label : '';
                if (label) {{
                    e.preventDefault();
                    e.stopPropagation();
                    removeLabelFromImagePath(imgPath, label);
                    return;
                }}
            }}
            if (isActionButton(clickTarget)) return;

            const autoNextAction = (e.metaKey || e.ctrlKey || e.shiftKey)
                ? null
                : (
                    getAutoBatchRemoveActionForContainer(container)
                    || getTableThreeBatchCandidateForContainer(container)
                    || getAutoBatchActionForContainer(container)
                    || getAutoNextActionForContainer(container)
                );
            if (autoNextAction) {{
                e.preventDefault();
                e.stopPropagation();
                const index = getContainerIndex(container);
                if (selectedPaths.size) {{
                    clearSelection();
                }}
                lastClickedIndex = index;
                anchorIndex = index;
                focusedIndex = index;
                if (autoNextAction.mode === 'batch-remove') {{
                    removeAutoBatchLabelClick(autoNextAction);
                }} else if (autoNextAction.mode === 'batch-apply') {{
                    applyAutoBatchLabelClick(autoNextAction);
                }} else {{
                    applyAutoNextLabelClick(container, imgPath, autoNextAction);
                }}
                return;
            }}

            e.preventDefault();
            e.stopPropagation();

            const index = getContainerIndex(container);

            if (e.metaKey || e.ctrlKey) {{
                // Cmd/Ctrl+click: toggle selection
                toggleSelect(container, imgPath);
                lastClickedIndex = index;
                anchorIndex = index;
            }} else if (e.shiftKey && anchorIndex >= 0) {{
                // Shift+click: range selection from anchor
                updateShiftSelection(index);
            }} else if (selectedPaths.has(imgPath)) {{
                selectedPaths.delete(imgPath);
                container.classList.remove('selected');
                lastClickedIndex = index;
                anchorIndex = index;
                lastShiftAnchor = -1;
                lastShiftEnd = -1;
                if (!selectedPaths.size) {{
                    lastShiftAnchor = -1;
                    lastShiftEnd = -1;
                    anchorIndex = -1;
                }}
                updateSelectionCount();
            }} else {{
                // Regular click: single select (replaces selection) + copy or lightbox replace
                selectOnly(container, imgPath);
                lastClickedIndex = index;
                anchorIndex = index;  // Set new anchor
                lastShiftAnchor = -1;  // Reset shift tracking
                lastShiftEnd = -1;
                if (parent.querySelector('.lightbox-viewer')) {{
                    showLightbox(imgPath);
                }} else {{
                    copyViaServer(imgPath, container, 'click');
                }}
            }}

            focusedIndex = index;
        }}

        function handleContainerDoubleClick(e) {{
            if (!isActiveScriptInstance()) {{
                return;
            }}
            const clickTarget = e && e.target && e.target.nodeType === 3 ? e.target.parentElement : (e ? e.target : null);
            const container = clickTarget && clickTarget.closest ? clickTarget.closest('.img-container') : null;
            if (!container || isActionButton(clickTarget)) {{
                return;
            }}
            const action = getAutoBatchRemoveActionForContainer(container);
            if (!action) {{
                return;
            }}
            e.preventDefault();
            e.stopPropagation();
            if (e.stopImmediatePropagation) {{
                e.stopImmediatePropagation();
            }}
            if (selectedPaths.size) {{
                clearSelection();
            }}
            clearAutoBatchHighlights();
            removeAutoBatchLabelClick(action);
        }}

        window.__reportLabelerContainerClick = function(evt, node) {{
            const resolved = evt && evt.target ? evt : {{
                target: node || null,
                metaKey: false,
                ctrlKey: false,
                shiftKey: false,
                preventDefault: () => {{}},
                stopPropagation: () => {{}},
                stopImmediatePropagation: () => {{}},
            }};
            if (node) {{
                resolved.target = node;
            }}
            if (!resolved.target) {{
                return;
            }}
            return handleContainerClick(resolved);
        }};

        // Smart shift selection: preserves Cmd+clicked items, only clears previous shift range
        function updateShiftSelection(newIndex) {{
            if (anchorIndex < 0) anchorIndex = focusedIndex >= 0 ? focusedIndex : 0;

            // Only clear PREVIOUS shift range if same anchor (handles contract/extend)
            // If anchor changed (via Cmd+click), keep existing selections (additive)
            if (lastShiftAnchor >= 0 && lastShiftEnd >= 0 && lastShiftAnchor === anchorIndex) {{
                const prevStart = Math.min(lastShiftAnchor, lastShiftEnd);
                const prevEnd = Math.max(lastShiftAnchor, lastShiftEnd);
                for (let i = prevStart; i <= prevEnd; i++) {{
                    const c = getContainerByIndex(i);
                    if (c) {{
                        const p = c.dataset.path;
                        if (p) {{
                            selectedPaths.delete(p);
                            c.classList.remove('selected');
                        }}
                    }}
                }}
            }}

            // Select new shift range
            const start = Math.min(anchorIndex, newIndex);
            const end = Math.max(anchorIndex, newIndex);
            for (let i = start; i <= end; i++) {{
                const container = getContainerByIndex(i);
                if (container) {{
                    const path = container.dataset.path;
                    if (path) {{
                        selectedPaths.add(path);
                        container.classList.add('selected');
                    }}
                }}
            }}

            // Track this shift operation
            lastShiftAnchor = anchorIndex;
            lastShiftEnd = newIndex;
            updateSelectionCount();
        }}

        function isEditableShortcutTarget(target) {{
            const el = target && target.nodeType === 3 ? target.parentElement : target;
            if (!el) {{
                return false;
            }}
            const tag = String(el.tagName || '').toLowerCase();
            return tag === 'input' || tag === 'textarea' || tag === 'select' || Boolean(el.isContentEditable);
        }}

        // Keyboard navigation for selection
        function handleKeyNav(e) {{
            if (!isActiveScriptInstance()) {{
                return;
            }}
            const key = String(e.key || '').toLowerCase();
            const commandLike = (e.metaKey || e.ctrlKey) && !e.altKey;
            if (commandLike && !isEditableShortcutTarget(e.target)) {{
                if (key === 'z') {{
                    e.preventDefault();
                    e.stopPropagation();
                    if (e.shiftKey) {{
                        redoAnnotationAction();
                    }} else {{
                        undoAnnotationAction();
                    }}
                    return;
                }}
            }}
            const allContainers = parent.querySelectorAll('.img-container');
            const totalImages = allContainers.length;
            if (totalImages === 0) return;
            const lightboxOpen = !!parent.querySelector('.lightbox-viewer');

            if (lightboxOpen) {{
                if (e.key === 'Escape') {{
                    e.preventDefault();
                    closeLightbox();
                    return;
                }}
                if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {{
                    e.preventDefault();
                    navigateLightbox(e.key === 'ArrowLeft' ? 'prev' : 'next');
                    return;
                }}
            }}

            // Cmd/Ctrl+Arrow cycles through visible annotation presets when a selection exists
            if (selectedPaths.size > 0 && (e.metaKey || e.ctrlKey) && !e.shiftKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {{
                const bar = parent.querySelector('.selection-bar.visible');
                if (bar) {{
                    const next = cycleAnnotationPreset(e.key === 'ArrowRight' ? 1 : -1);
                    const barInput = bar.querySelector('.annotation-input');
                    if (barInput) {{
                        barInput.value = next || '';
                    }}
                    if (barPresetSyncFn) {{
                        barPresetSyncFn();
                    }}
                    if (barApplyAnnotation && selectedPaths.size > 0) {{
                        barApplyAnnotation();
                    }}
                    e.preventDefault();
                    return;
                }}
            }}

            const tag = e.target.tagName.toUpperCase();
            if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) return;

            // Arrow keys with Shift for macOS-like selection
            if (e.key === 'ArrowRight' || e.key === 'ArrowLeft' || e.key === 'ArrowUp' || e.key === 'ArrowDown') {{
                // Skip Shift+Arrow if folder nav is enabled (handled by banner handler instead)
                if (e.shiftKey && shiftArrowFolderNav && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) return;
                if (!e.shiftKey && !e.metaKey && !e.ctrlKey) return; // Only handle with modifier

                e.preventDefault();

                // Initialize focus if not set
                if (focusedIndex < 0) focusedIndex = 0;

                let newIndex = focusedIndex;
                const cols = 4; // Approximate columns per row

                if (e.key === 'ArrowRight') newIndex = Math.min(focusedIndex + 1, totalImages - 1);
                else if (e.key === 'ArrowLeft') newIndex = Math.max(focusedIndex - 1, 0);
                else if (e.key === 'ArrowDown') newIndex = Math.min(focusedIndex + cols, totalImages - 1);
                else if (e.key === 'ArrowUp') newIndex = Math.max(focusedIndex - cols, 0);

                if (newIndex !== focusedIndex) {{
                    const container = getContainerByIndex(newIndex);
                    if (container) {{
                        if (e.shiftKey) {{
                            // Shift+Arrow: macOS-like extend/contract selection from anchor
                            updateShiftSelection(newIndex);
                        }} else if (e.metaKey || e.ctrlKey) {{
                            // Cmd/Ctrl+Arrow: move focus without selecting
                            anchorIndex = newIndex;  // Reset anchor
                        }}
                        setFocus(newIndex);
                        updateSelectionCount();
                    }}
                }}
            }}

            // Cmd+A: Select all
            if ((e.metaKey || e.ctrlKey) && e.key === 'a') {{
                e.preventDefault();
                allContainers.forEach(container => {{
                    const path = container.dataset.path;
                    if (path) {{
                        selectedPaths.add(path);
                        container.classList.add('selected');
                    }}
                }});
                updateSelectionCount();
            }}

            // Escape: Clear selection
            if (e.key === 'Escape') {{
                clearSelection();
                anchorIndex = -1;
            }}
        }}

        function bindTopLevelListener(targetDoc, key, eventType, handler, options) {{
            if (!targetDoc || !targetDoc.addEventListener) {{
                return;
            }}
            const wrapperHandler = function(event) {{
                if (!isActiveScriptInstance()) {{
                    return;
                }}
                return handler(event);
            }};
            const existing = targetDoc[key];
            if (existing) {{
                targetDoc.removeEventListener(eventType, existing, options);
            }}
            targetDoc[key] = wrapperHandler;
            targetDoc.addEventListener(eventType, wrapperHandler, options);
        }}
        let interactionDocPrimary = parent;
        function rebindInteractionListeners() {{
            const nextPrimary = resolveInteractionDocument();
            if (nextPrimary && nextPrimary !== interactionDocPrimary) {{
                interactionDocPrimary = nextPrimary;
                parent = nextPrimary;
            }}
            if (!interactionDocPrimary) {{
                interactionDocPrimary = parent;
            }}

            const bindTargets = [];
            if (interactionDocPrimary) {{
                bindTargets.push(interactionDocPrimary);
            }}
            if (currentDocument && currentDocument !== interactionDocPrimary) {{
                bindTargets.push(currentDocument);
            }}
            bindTargets.forEach((targetDoc) => {{
                bindTopLevelListener(targetDoc, '__imgCopyClickHandler', 'click', handleContainerClick, true);
                bindTopLevelListener(targetDoc, '__imgKeyNavHandler', 'keydown', handleKeyNav, true);
            }});
            bindImageContainerInteractions();
        }}
        rebindInteractionListeners();
        setActiveInterval(rebindInteractionListeners, 700);
        updateSelectionCount();
        // Rename writes happen only via explicit /rename-label calls. Replaying a
        // browser rename map on folder navigation would create a second source of truth.

        // Expose showCopyBanner globally
        window.showCopyBanner = showCopyBanner;

        // Poll for current index and scroll/highlight (for keyboard nav from Word)
        let lastHighlightedIndex = -1;
        async function pollAndHighlight() {{
            try {{
                const response = await fetch(clipboardServerBase + '/index');
                const data = await response.json();
                if (data.index !== lastHighlightedIndex && data.index >= 0) {{
                    lastHighlightedIndex = data.index;

                    // Remove old highlights
                    parent.querySelectorAll('.img-container.highlighted').forEach(el => {{
                        el.classList.remove('highlighted');
                        el.style.outline = '';
                        el.style.outlineOffset = '';
                    }});

                    // Find and highlight current image
                    const containers = parent.querySelectorAll('.img-container');
                    if (containers[data.index]) {{
                        const container = containers[data.index];
                        container.classList.add('highlighted');
                        container.style.outline = '1px solid rgba(255, 255, 255, 0.4)';
                        container.style.outlineOffset = '-1px';

                        // Only scroll into view when auto-copy is disabled (keyboard nav mode)
                        if (!autoCopyEnabled) {{
                            container.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                        }}

                        // Update banner
                        showCopyBanner('→', (data.index + 1) + '/' + data.total + ': ' + data.name);
                    }}
                }}
            }} catch (e) {{
                // Server not available, ignore
            }}
        }}

        // Poll every 300ms
        setActiveInterval(pollAndHighlight, 300);
    }})();
    </script>
    """
    components.html(js_code, height=0)

    # === Persist UI state ===
    current_state = _gather_current_state()
    _save_ui_last_state(current_state)

    # Handle deferred "Set as default"
    if st.session_state.get("_save_defaults"):
        _save_ui_defaults(current_state)
        set_default_parent(strip_quotes(parent_dir_str))
        del st.session_state._save_defaults
        st.toast("Defaults saved!")


if __name__ == "__main__":
    main()
