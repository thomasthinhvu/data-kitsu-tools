#!/usr/bin/env python3
"""
Lightweight Kitsu (Zou) REST API client for Data Kitsu Tools 2.0.

Uses only the Python standard library (urllib + json + ssl) so no extra
packages beyond PySide6 are required. Modeled on the Prism Kitsu plugin
(gazu endpoints, token/session handling, episode/sequence/shot hierarchy,
and self-signed SSL fallback).
"""
from __future__ import annotations

import http.client
import json
import mimetypes
import os
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


class KitsuError(Exception):
    """Base error for Kitsu API operations."""


class KitsuConnectionError(KitsuError):
    """Raised when the Kitsu server cannot be reached or returns an HTTP 5xx error."""


class KitsuAuthError(KitsuError):
    """Raised when authentication fails or the session/token is invalid or expired."""


def normalize_api_url(url: str) -> str:
    """Normalize a Kitsu server URL to its ``.../api`` base URL."""
    cleaned = (url or "").strip().rstrip("/\\")
    if not cleaned:
        raise ValueError("Kitsu URL cannot be empty.")
    if not cleaned.startswith(("http://", "https://")):
        cleaned = "https://" + cleaned
    if not cleaned.endswith("/api"):
        cleaned = cleaned + "/api"
    return cleaned


def extract_shot_frames(kshot: dict[str, Any]) -> int | None:
    """Extract frame count from a Kitsu shot payload.

    Checks ``nb_frames`` first (matching Kitsu CSV ``Frames``), then falls back
    to ``data.frame_in`` / ``data.frame_out`` (matching Prism's Kitsu plugin).
    """
    nb_frames = kshot.get("nb_frames")
    if nb_frames is not None and nb_frames != "":
        try:
            val = int(float(nb_frames))
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass

    data = kshot.get("data") or {}
    if isinstance(data, dict):
        f_in = data.get("frame_in")
        f_out = data.get("frame_out")
        if f_in not in (None, "") and f_out not in (None, ""):
            try:
                start = int(float(f_in))
                end = int(float(f_out))
                if end >= start:
                    return end - start + 1
            except (TypeError, ValueError):
                pass
    return None


def _sort_by_name(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda x: str(x.get("name") or "").lower())


class KitsuClient:
    """Minimal REST client for Kitsu / Zou ``/api`` endpoints."""

    USER_AGENT = "DataKitsuTools/2.0"

    def __init__(
        self,
        host: str = "",
        verify_ssl: bool = True,
        timeout: float = 15.0,
    ) -> None:
        self.api_url = normalize_api_url(host) if host and host.strip() else ""
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.user: dict[str, Any] | None = None
        self._conn_lock = threading.Lock()
        self._conn: http.client.HTTPConnection | None = None
        self._conn_key: tuple[str, int, str, bool] | None = None

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._conn_key = None

    def set_host(self, host: str, verify_ssl: bool | None = None) -> str:
        self.api_url = normalize_api_url(host)
        if verify_ssl is not None:
            self.verify_ssl = verify_ssl
        with self._conn_lock:
            self._close_conn()
        return self.api_url

    def set_token(self, token: str | dict[str, Any] | None) -> None:
        if not token:
            self.access_token = None
            self.refresh_token = None
            return
        if isinstance(token, dict):
            self.access_token = token.get("access_token") or None
            self.refresh_token = token.get("refresh_token") or None
        else:
            self.access_token = str(token).strip() or None
            self.refresh_token = None

    def get_tokens(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.access_token:
            out["access_token"] = self.access_token
        if self.refresh_token:
            out["refresh_token"] = self.refresh_token
        return out

    def _ssl_context(self) -> ssl.SSLContext | None:
        if self.verify_ssl:
            return None
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _http_call(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
    ) -> tuple[int, bytes]:
        """Execute an HTTP request reusing a thread-safe Keep-Alive connection."""
        parsed = urllib.parse.urlparse(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
        port = parsed.port or (443 if scheme == "https" else 80)
        req_path = parsed.path or "/"
        if parsed.query:
            req_path += "?" + parsed.query
        key = (scheme, port, host, self.verify_ssl)
        req_headers = dict(headers)
        req_headers.setdefault("Connection", "keep-alive")

        with self._conn_lock:
            for attempt in range(2):
                if self._conn is None or self._conn_key != key:
                    self._close_conn()
                    if scheme == "https":
                        self._conn = http.client.HTTPSConnection(
                            host, port, timeout=self.timeout, context=self._ssl_context()
                        )
                    else:
                        self._conn = http.client.HTTPConnection(
                            host, port, timeout=self.timeout
                        )
                    self._conn_key = key
                try:
                    self._conn.request(method, req_path, body=body, headers=req_headers)
                    resp = self._conn.getresponse()
                    raw = resp.read()
                    return resp.status, raw
                except (http.client.HTTPException, OSError) as exc:
                    self._close_conn()
                    if attempt == 1:
                        raise KitsuConnectionError(
                            f"Could not connect to Kitsu ({url}): {exc}"
                        ) from exc
        raise KitsuConnectionError(f"Could not connect to Kitsu ({url}).")

    def _raw_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        use_auth: bool = True,
    ) -> tuple[int, Any]:
        if not self.api_url:
            raise KitsuConnectionError("Kitsu server URL is not configured.")

        rel = path.lstrip("/")
        url = f"{self.api_url}/{rel}" if rel else self.api_url
        headers = {
            "User-Agent": self.USER_AGENT,
            "Accept": "application/json",
        }
        if use_auth and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if extra_headers:
            headers.update(extra_headers)

        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        status, raw = self._http_call(method, url, body, headers)
        text = raw.decode("utf-8", errors="replace") if raw else ""
        try:
            data = json.loads(text) if text.strip() else None
        except ValueError:
            data = {"message": text}
        return status, data

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        retry_refresh: bool = True,
    ) -> Any:
        status, data = self._raw_request(method, path, payload=payload)
        if status == 401 and retry_refresh and self.refresh_token:
            if self.refresh_access_token():
                status, data = self._raw_request(method, path, payload=payload)

        if 200 <= status < 300:
            return data

        msg = ""
        if isinstance(data, dict):
            msg = str(data.get("message") or data.get("error") or "")
        if status in (401, 403):
            raise KitsuAuthError(msg or f"Authentication failed (HTTP {status}).")
        if status >= 500:
            raise KitsuConnectionError(msg or f"Kitsu server error (HTTP {status}).")
        raise KitsuError(msg or f"Kitsu API error (HTTP {status}).")

    def host_is_up(self) -> bool:
        """Return True if the Kitsu API root responds."""
        if not self.api_url:
            return False
        try:
            status, _ = self._raw_request("HEAD", "", use_auth=False)
            if status in (200, 204):
                return True
            status, _ = self._raw_request("GET", "", use_auth=False)
            return 200 <= status < 400
        except KitsuConnectionError:
            return False

    def validate_url(self) -> bool:
        """Verify that the configured URL points to a valid Kitsu ``/api/auth/login``."""
        if not self.api_url:
            return False
        status, _ = self._raw_request("POST", "auth/login", payload={}, use_auth=False)
        return status in (200, 400, 401, 422)

    def login(self, email: str, password: str, totp: str | None = None) -> dict[str, Any]:
        """Authenticate with email + password (+ optional TOTP) and store tokens."""
        payload: dict[str, Any] = {"email": email.strip(), "password": password}
        if totp and totp.strip():
            payload["totp"] = totp.strip()

        status, data = self._raw_request("POST", "auth/login", payload=payload, use_auth=False)
        if status in (401, 403) or (isinstance(data, dict) and data.get("login") is False):
            msg = data.get("message") if isinstance(data, dict) else None
            raise KitsuAuthError(msg or "Invalid Kitsu email, password, or TOTP code.")
        if not (200 <= status < 300) or not isinstance(data, dict):
            msg = data.get("message") if isinstance(data, dict) else None
            raise KitsuError(msg or f"Login failed (HTTP {status}).")

        self.access_token = data.get("access_token") or None
        self.refresh_token = data.get("refresh_token") or None
        self.user = data.get("user") if isinstance(data.get("user"), dict) else None
        if not self.access_token:
            raise KitsuAuthError("Kitsu login response did not include an access token.")
        return data

    def refresh_access_token(self) -> bool:
        """Attempt to refresh the access token using ``refresh_token``."""
        if not self.refresh_token:
            return False
        try:
            status, data = self._raw_request(
                "GET",
                "auth/refresh-token",
                extra_headers={"Authorization": f"Bearer {self.refresh_token}"},
                use_auth=False,
            )
        except KitsuConnectionError:
            return False
        if 200 <= status < 300 and isinstance(data, dict) and data.get("access_token"):
            self.access_token = data["access_token"]
            return True
        return False

    def get_current_user(self) -> dict[str, Any] | None:
        """Return the authenticated user dict, or ``None`` if not authenticated."""
        if not self.access_token:
            return None
        try:
            data = self._request("GET", "auth/authenticated")
        except KitsuAuthError:
            self.user = None
            return None
        if isinstance(data, dict) and data.get("authenticated"):
            user = data.get("user")
            self.user = user if isinstance(user, dict) else None
            return self.user
        self.user = None
        return None

    def logout(self) -> None:
        """Log out from Kitsu and clear local tokens."""
        try:
            if self.access_token:
                self._raw_request("GET", "auth/logout")
        except KitsuError:
            pass
        finally:
            self.access_token = None
            self.refresh_token = None
            self.user = None

    def get_projects(self, open_only: bool = True) -> list[dict[str, Any]]:
        """Return projects sorted by name."""
        endpoint = "data/projects/open" if open_only else "data/projects"
        data = self._request("GET", endpoint)
        return _sort_by_name(data if isinstance(data, list) else [])

    def get_episodes(self, project_id: str) -> list[dict[str, Any]]:
        """Return episodes for a project, sorted by name."""
        pid = urllib.parse.quote(str(project_id), safe="")
        data = self._request("GET", f"data/projects/{pid}/episodes")
        return _sort_by_name(data if isinstance(data, list) else [])

    def get_sequences(self, project_id: str, episode_id: str | None = None) -> list[dict[str, Any]]:
        """Return sequences for a project or a specific episode, sorted by name."""
        if episode_id:
            eid = urllib.parse.quote(str(episode_id), safe="")
            data = self._request("GET", f"data/episodes/{eid}/sequences")
        else:
            pid = urllib.parse.quote(str(project_id), safe="")
            data = self._request("GET", f"data/projects/{pid}/sequences")
        return _sort_by_name(data if isinstance(data, list) else [])

    def get_shots(
        self,
        project_id: str,
        episode_id: str | None = None,
        sequence_id: str | None = None,
        include_canceled: bool = False,
        include_tasks: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetch shots for a project / episode / sequence and enrich with seq, frame & task data."""
        episodes = self.get_episodes(project_id)
        ep_by_id = {ep.get("id"): ep.get("name", "") for ep in episodes if ep.get("id")}

        sequences = self.get_sequences(project_id, episode_id=episode_id)
        seq_by_id: dict[str, dict[str, Any]] = {
            seq["id"]: seq for seq in sequences if seq.get("id")
        }

        if sequence_id:
            sid = urllib.parse.quote(str(sequence_id), safe="")
            raw_shots = self._request("GET", f"data/sequences/{sid}/shots")
        elif episode_id:
            eid = urllib.parse.quote(str(episode_id), safe="")
            raw_shots = self._request("GET", f"data/episodes/{eid}/shots")
        else:
            pid = urllib.parse.quote(str(project_id), safe="")
            raw_shots = self._request("GET", f"data/projects/{pid}/shots")

        if not isinstance(raw_shots, list):
            return []

        tasks_by_shot: dict[str, dict[str, dict[str, Any]]] = {}
        assignees_by_shot: dict[str, set[str]] = {}
        assignee_ids_by_shot: dict[str, set[str]] = {}
        if include_tasks:
            tasks_by_shot, assignees_by_shot, assignee_ids_by_shot = self._build_shot_task_map(
                project_id
            )

        results: list[dict[str, Any]] = []
        for kshot in raw_shots:
            if not isinstance(kshot, dict):
                continue
            if not include_canceled and kshot.get("canceled"):
                continue

            parent_id = kshot.get("parent_id") or kshot.get("sequence_id")
            seq_obj = seq_by_id.get(parent_id, {})
            seq_name = str(seq_obj.get("name") or kshot.get("sequence_name") or "")
            ep_id = seq_obj.get("parent_id") or kshot.get("episode_id")
            ep_name = str(ep_by_id.get(ep_id, "") or kshot.get("episode_name") or "")

            shot_id = str(kshot.get("id") or "")
            extra = kshot.get("data") if isinstance(kshot.get("data"), dict) else {}
            results.append(
                {
                    "id": shot_id,
                    "name": str(kshot.get("name") or "").strip(),
                    "sequence": seq_name,
                    "episode": ep_name,
                    "frames": extract_shot_frames(kshot),
                    "fps": extra.get("fps"),
                    "resolution": extra.get("resolution") or "",
                    "thumbnail_id": str(kshot.get("preview_file_id") or ""),
                    "tasks": tasks_by_shot.get(shot_id, {}),
                    "assignees": sorted(assignees_by_shot.get(shot_id, set())),
                    "assignee_ids": sorted(assignee_ids_by_shot.get(shot_id, set())),
                }
            )

        results.sort(key=lambda r: (r["episode"].lower(), r["sequence"].lower(), r["name"].lower()))
        return results

    def download_thumbnail(self, preview_file_id: str) -> bytes | None:
        """Download a shot's thumbnail PNG bytes from Kitsu."""
        if not preview_file_id or not self.api_url:
            return None
        pid = urllib.parse.quote(str(preview_file_id), safe="")
        url = f"{self.api_url}/pictures/thumbnails/preview-files/{pid}.png"
        headers = {"User-Agent": self.USER_AGENT}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        try:
            status, raw = self._http_call("GET", url, None, headers)
            if 200 <= status < 300 and raw:
                return raw
        except KitsuError:
            pass
        return None

    def get_task_types(self, project_id: str | None = None) -> list[dict[str, Any]]:
        """Return task types (all or for a specific project)."""
        if project_id:
            pid = urllib.parse.quote(str(project_id), safe="")
            try:
                data = self._request("GET", f"data/projects/{pid}/task-types")
                if isinstance(data, list) and data:
                    return _sort_by_name(data)
            except KitsuError:
                pass
        data = self._request("GET", "data/task-types")
        return _sort_by_name(data if isinstance(data, list) else [])

    def get_task_statuses(self, project_id: str | None = None) -> list[dict[str, Any]]:
        """Return task statuses (project-specific if available, else global)."""
        if project_id:
            pid = urllib.parse.quote(str(project_id), safe="")
            try:
                data = self._request("GET", f"data/projects/{pid}/settings/task-status")
                if isinstance(data, list) and data:
                    return _sort_by_name(data)
            except KitsuError:
                pass
        data = self._request("GET", "data/task-status")
        return _sort_by_name(data if isinstance(data, list) else [])

    def get_persons(self) -> list[dict[str, Any]]:
        """Return active persons in Kitsu (empty list if endpoint is restricted)."""
        try:
            data = self._request("GET", "data/persons")
        except KitsuError:
            return []
        if not isinstance(data, list):
            return []
        for p in data:
            if not p.get("full_name"):
                first = str(p.get("first_name") or "").strip()
                last = str(p.get("last_name") or "").strip()
                p["full_name"] = f"{first} {last}".strip() or p.get("email") or ""
        return sorted(data, key=lambda x: str(x.get("full_name") or "").lower())

    def get_project_tasks(self, project_id: str) -> list[dict[str, Any]]:
        """Return all tasks for a project."""
        pid = urllib.parse.quote(str(project_id), safe="")
        data = self._request("GET", f"data/projects/{pid}/tasks")
        return data if isinstance(data, list) else []

    def get_shot_tasks(self, shot_id: str) -> list[dict[str, Any]]:
        """Return all tasks linked to a specific shot."""
        sid = urllib.parse.quote(str(shot_id), safe="")
        data = self._request("GET", f"data/shots/{sid}/tasks")
        return data if isinstance(data, list) else []

    def _build_shot_task_map(
        self, project_id: str
    ) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, set[str]], dict[str, set[str]]]:
        task_types = {tt["id"]: tt for tt in self.get_task_types(project_id) if tt.get("id")}
        statuses = {st["id"]: st for st in self.get_task_statuses(project_id) if st.get("id")}
        persons = {p["id"]: p for p in self.get_persons() if p.get("id")}
        try:
            tasks = self.get_project_tasks(project_id)
        except KitsuError:
            tasks = []

        tasks_by_shot: dict[str, dict[str, dict[str, Any]]] = {}
        assignees_by_shot: dict[str, set[str]] = {}
        assignee_ids_by_shot: dict[str, set[str]] = {}
        for t in tasks:
            if not isinstance(t, dict):
                continue
            eid = str(t.get("entity_id") or "")
            if not eid:
                continue
            tt = task_types.get(t.get("task_type_id"), {})
            tt_name = str(tt.get("name") or t.get("task_type_name") or "").strip()
            if not tt_name:
                continue
            st = statuses.get(t.get("task_status_id"), {})
            st_name = str(st.get("name") or t.get("task_status_name") or "").strip()
            st_short = str(st.get("short_name") or "").strip()

            t_assignees: list[str] = []
            t_assignee_ids: list[str] = []
            for pid in t.get("assignees") or []:
                pid_str = str(pid.get("id") if isinstance(pid, dict) else pid)
                if not pid_str:
                    continue
                t_assignee_ids.append(pid_str)
                assignee_ids_by_shot.setdefault(eid, set()).add(pid_str)
                person = persons.get(pid_str)
                pname = str(person.get("full_name") if person else pid_str).strip()
                if pname:
                    t_assignees.append(pname)
                    assignees_by_shot.setdefault(eid, set()).add(pname)

            tasks_by_shot.setdefault(eid, {})[tt_name] = {
                "id": str(t.get("id") or ""),
                "type": tt_name,
                "status": st_name,
                "status_short": st_short,
                "status_id": str(t.get("task_status_id") or ""),
                "assignees": t_assignees,
                "assignee_ids": t_assignee_ids,
            }
        return tasks_by_shot, assignees_by_shot, assignee_ids_by_shot

    @staticmethod
    def find_task_status(statuses: list[dict[str, Any]], target: str) -> dict[str, Any] | None:
        """Find a Kitsu task status by name or short_name (case/space-insensitive)."""
        norm = lambda s: "".join(ch.lower() for ch in str(s or "") if not ch.isspace())
        wanted = norm(target)
        for st in statuses:
            if norm(st.get("name")) == wanted or norm(st.get("short_name")) == wanted:
                return st
        return None

    def _upload_multipart(self, path: str, file_path: Path) -> Any:
        if not self.api_url:
            raise KitsuConnectionError("Kitsu server URL is not configured.")
        boundary = f"----DataKitsuBoundary{uuid.uuid4().hex}"
        mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        file_bytes = Path(file_path).read_bytes()
        filename = Path(file_path).name

        part_header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        part_footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = part_header + file_bytes + part_footer

        rel = path.lstrip("/")
        url = f"{self.api_url}/{rel}"

        def _send() -> tuple[int, Any]:
            headers = {
                "User-Agent": self.USER_AGENT,
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            }
            if self.access_token:
                headers["Authorization"] = f"Bearer {self.access_token}"
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            ctx = self._ssl_context()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    return resp.status, (json.loads(raw) if raw.strip() else None)
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                try:
                    return exc.code, json.loads(raw) if raw.strip() else {"message": exc.reason}
                except ValueError:
                    return exc.code, {"message": raw or str(exc.reason)}
            except (urllib.error.URLError, OSError) as exc:
                raise KitsuConnectionError(f"Upload failed ({url}): {exc}") from exc

        status, data = _send()
        if status == 401 and self.refresh_token and self.refresh_access_token():
            status, data = _send()
        if 200 <= status < 300:
            return data
        msg = data.get("message") if isinstance(data, dict) else ""
        if status in (401, 403):
            raise KitsuAuthError(msg or f"Authentication failed (HTTP {status}).")
        raise KitsuError(msg or f"Attachment upload failed (HTTP {status}).")

    def add_preview(
        self,
        task_id: str,
        comment_id: str,
        preview_path: str | Path,
        revision: int | None = None,
        set_main: bool = True,
    ) -> dict[str, Any]:
        """Create and upload a Revision preview (MP4 or JPG) onto a Kitsu task comment."""
        tid = urllib.parse.quote(str(task_id), safe="")
        cid = urllib.parse.quote(str(comment_id), safe="")
        body: dict[str, Any] = {}
        if revision is not None:
            body["revision"] = int(revision)
        preview_obj = self._request(
            "POST",
            f"actions/tasks/{tid}/comments/{cid}/add-preview",
            payload=body,
        )
        if not isinstance(preview_obj, dict) or not preview_obj.get("id"):
            return {}
        pid = urllib.parse.quote(str(preview_obj["id"]), safe="")
        uploaded = self._upload_multipart(f"pictures/preview-files/{pid}", Path(preview_path))
        if set_main:
            try:
                self._request("PUT", f"actions/preview-files/{pid}/set-main-preview", payload={})
            except KitsuError:
                pass
        return uploaded if isinstance(uploaded, dict) else preview_obj

    def add_comment(
        self,
        task_id: str,
        task_status_id: str,
        comment: str = "",
        preview_path: str | Path | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Post a comment + status update on a Kitsu task and publish a Revision preview."""
        tid = urllib.parse.quote(str(task_id), safe="")
        payload = {
            "task_status_id": task_status_id,
            "comment": comment,
            "checklist": [],
            "links": [],
        }
        created = self._request("POST", f"actions/tasks/{tid}/comment", payload=payload)
        if isinstance(created, dict) and created.get("id"):
            cid = str(created["id"])
            if preview_path and Path(preview_path).is_file():
                self.add_preview(
                    str(task_id), cid, Path(preview_path), revision=revision, set_main=True
                )
        return created if isinstance(created, dict) else {}

    def update_shot_task_on_publish(
        self,
        project_id: str,
        shot_id: str,
        dest_path: str,
        task_type_name: str = "Compositing",
        status_name: str = "ĐợiCheck",
        preview_path: str | Path | None = None,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Change a shot's task status and publish destination path + Revision preview."""
        if not shot_id:
            raise KitsuError("Shot has no Kitsu ID.")
        task_types = self.get_task_types(project_id)
        tt_by_id = {tt["id"]: str(tt.get("name") or "") for tt in task_types if tt.get("id")}
        tasks = self.get_shot_tasks(shot_id)

        wanted_tt = task_type_name.strip().lower()
        target_task = None
        for t in tasks:
            tt_name = tt_by_id.get(t.get("task_type_id"), "")
            if tt_name.strip().lower() == wanted_tt:
                target_task = t
                break
        if not target_task:
            raise KitsuError(f'Task column "{task_type_name}" not found on shot.')

        statuses = self.get_task_statuses(project_id)
        status_obj = self.find_task_status(statuses, status_name)
        if not status_obj:
            raise KitsuError(f'Task status "{status_name}" not found in Kitsu project.')

        return self.add_comment(
            task_id=str(target_task["id"]),
            task_status_id=str(status_obj["id"]),
            comment=str(dest_path),
            preview_path=preview_path,
            revision=revision,
        )
