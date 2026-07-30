"""Background Google Drive uploader for camera-stream recordings.

Reads OAuth config from the environment (GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET /
GDRIVE_REFRESH_TOKEN, injected via camera-stream.service's EnvironmentFile). If any
of those are unset, enqueue() becomes a silent no-op so the camera pipeline keeps
running fine without Drive configured. Uses resumable upload (not simple/multipart)
since continuous chunks run ~100MB and the Pi's uplink shouldn't be assumed fast or
reliable.
"""
import json
import logging
import os
import queue
import threading
import time

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
ROOT_FOLDER_NAME = "Camera Stream Backups"
SUBFOLDERS = ("snapshots", "recordings", "continuous")
UPLOAD_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drive_upload_state.json")

CLIENT_ID = os.environ.get("GDRIVE_CLIENT_ID")
CLIENT_SECRET = os.environ.get("GDRIVE_CLIENT_SECRET")
REFRESH_TOKEN = os.environ.get("GDRIVE_REFRESH_TOKEN")
CONFIGURED = bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN)

_token_lock = threading.Lock()
_access_token = None
_token_expiry = 0.0

_folder_lock = threading.Lock()
_folder_ids = {}  # (parent_id, name) -> id, memoized for this process's lifetime

_upload_queue = queue.Queue()
_warned_unconfigured = False


def _load_enabled():
    try:
        with open(STATE_FILE) as f:
            return bool(json.load(f).get("enabled", True))
    except (OSError, ValueError):
        return True


def _save_enabled(enabled):
    with open(STATE_FILE, "w") as f:
        json.dump({"enabled": enabled}, f)


_enabled_lock = threading.Lock()
_enabled = _load_enabled()


def is_enabled():
    with _enabled_lock:
        return _enabled


def set_enabled(enabled):
    global _enabled
    with _enabled_lock:
        _enabled = enabled
    _save_enabled(enabled)


def status():
    return {
        "configured": CONFIGURED,
        "enabled": is_enabled(),
        "queue_size": _upload_queue.qsize(),
    }


def _get_access_token():
    global _access_token, _token_expiry
    with _token_lock:
        if _access_token and time.time() < _token_expiry - 300:
            return _access_token
        resp = requests.post(TOKEN_URL, data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": REFRESH_TOKEN,
            "grant_type": "refresh_token",
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        _access_token = data["access_token"]
        _token_expiry = time.time() + data.get("expires_in", 3600)
        return _access_token


def _drive_request(method, url, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {_get_access_token()}"
    return requests.request(method, url, headers=headers, timeout=30, **kwargs)


def _find_folder(name, parent_id):
    query = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    query += f" and '{parent_id}' in parents" if parent_id else " and 'root' in parents"
    resp = _drive_request("GET", DRIVE_FILES_URL, params={"q": query, "fields": "files(id,name)"})
    resp.raise_for_status()
    files = resp.json().get("files", [])
    return files[0]["id"] if files else None


def _create_folder(name, parent_id):
    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]
    resp = _drive_request("POST", DRIVE_FILES_URL, json=metadata, params={"fields": "id"})
    resp.raise_for_status()
    return resp.json()["id"]


def _ensure_folder(name, parent_id=None):
    key = (parent_id, name)
    with _folder_lock:
        if key in _folder_ids:
            return _folder_ids[key]
        folder_id = _find_folder(name, parent_id) or _create_folder(name, parent_id)
        _folder_ids[key] = folder_id
        return folder_id


def _subfolder_id(subfolder):
    root_id = _ensure_folder(ROOT_FOLDER_NAME)
    return _ensure_folder(subfolder, root_id)


def _upload_once(local_path, subfolder):
    folder_id = _subfolder_id(subfolder)
    metadata = {"name": os.path.basename(local_path), "parents": [folder_id]}
    size = os.path.getsize(local_path)

    init_resp = _drive_request(
        "POST", DRIVE_UPLOAD_URL, params={"uploadType": "resumable"}, json=metadata,
    )
    init_resp.raise_for_status()
    session_url = init_resp.headers["Location"]

    with open(local_path, "rb") as f:
        put_resp = requests.put(
            session_url,
            data=f,
            headers={
                "Authorization": f"Bearer {_get_access_token()}",
                "Content-Length": str(size),
            },
            timeout=900,
        )
    put_resp.raise_for_status()
    return put_resp.json()["id"]


def upload_file(local_path, subfolder, delete_after=False):
    for attempt in range(1, UPLOAD_RETRIES + 1):
        try:
            _upload_once(local_path, subfolder)
            logging.info("uploaded %s to Drive/%s/%s", local_path, ROOT_FOLDER_NAME, subfolder)
            if delete_after:
                os.remove(local_path)
            return True
        except Exception as e:
            logging.warning("Drive upload attempt %d/%d failed for %s: %s",
                             attempt, UPLOAD_RETRIES, local_path, e)
            if attempt < UPLOAD_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    logging.error("giving up uploading %s to Drive after %d attempts", local_path, UPLOAD_RETRIES)
    return False


def _worker():
    while True:
        local_path, subfolder, delete_after = _upload_queue.get()
        try:
            upload_file(local_path, subfolder, delete_after)
        except Exception:
            logging.exception("unexpected error uploading %s", local_path)
        finally:
            _upload_queue.task_done()


def enqueue(local_path, subfolder, delete_after=False):
    global _warned_unconfigured
    if not CONFIGURED:
        if not _warned_unconfigured:
            logging.warning("Drive upload not configured (GDRIVE_* env vars unset), skipping %s", local_path)
            _warned_unconfigured = True
        return
    if not is_enabled():
        return
    _upload_queue.put((local_path, subfolder, delete_after))


def list_files(subfolder):
    if not CONFIGURED:
        return []
    folder_id = _subfolder_id(subfolder)
    resp = _drive_request("GET", DRIVE_FILES_URL, params={
        "q": f"'{folder_id}' in parents and trashed = false",
        "fields": "files(id,name,size,createdTime,webViewLink)",
        "orderBy": "createdTime desc",
        "pageSize": 200,
    })
    resp.raise_for_status()
    files = resp.json().get("files", [])
    for f in files:
        f["subfolder"] = subfolder
    return files


def list_all_files():
    files = []
    for subfolder in SUBFOLDERS:
        files.extend(list_files(subfolder))
    files.sort(key=lambda f: f.get("createdTime", ""), reverse=True)
    return files


def delete_file(file_id):
    if not CONFIGURED:
        raise RuntimeError("Google Drive is not configured")
    resp = _drive_request("DELETE", f"{DRIVE_FILES_URL}/{file_id}")
    resp.raise_for_status()


if CONFIGURED:
    threading.Thread(target=_worker, daemon=True, name="gdrive-uploader").start()
