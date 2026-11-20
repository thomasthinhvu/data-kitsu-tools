"""Tests for Kitsu REST client, task/comment updates, Excel/OCR import, and JPG capture."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PySide6.QtGui import QImage

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_kitsu_tools as dkt
import kitsu_client as kc
import shot_import as si


class _MockKitsuHandler(BaseHTTPRequestHandler):
    posted_comments: list[dict] = []
    uploaded_attachments: list[bytes] = []

    def log_message(self, format, *args):  # noqa: A002
        return

    def _send_json(self, status: int, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802
        self.send_response(200 if self.path == "/api" else 404)
        self.end_headers()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        if self.path == "/api/auth/login":
            data = json.loads(raw.decode("utf-8") or "{}")
            if not data:
                self._send_json(400, {"message": "Missing credentials"})
                return
            if data.get("email") == "artist@studio.com" and data.get("password") == "secret":
                self._send_json(
                    200,
                    {
                        "login": True,
                        "access_token": "tok_access_1",
                        "refresh_token": "tok_refresh_1",
                        "user": {"id": "u1", "full_name": "Thomas Artist", "email": "artist@studio.com"},
                    },
                )
            else:
                self._send_json(401, {"login": False, "message": "Invalid credentials"})
            return

        if self.path == "/api/actions/tasks/t_comp_1/comment":
            data = json.loads(raw.decode("utf-8"))
            self.__class__.posted_comments.append(data)
            self._send_json(200, {"id": "cmt_1", "text": data.get("comment", "")})
            return

        if self.path == "/api/actions/tasks/t_comp_1/comments/cmt_1/add-attachment":
            self.__class__.uploaded_attachments.append(raw)
            self._send_json(200, {"id": "att_1"})
            return

        self._send_json(404, {"message": "Not found"})

    def do_GET(self):  # noqa: N802
        auth = self.headers.get("Authorization", "")
        if self.path == "/api/auth/refresh-token":
            if auth == "Bearer tok_refresh_1":
                self._send_json(200, {"access_token": "tok_access_2"})
            else:
                self._send_json(401, {"message": "Bad refresh token"})
            return
        if self.path == "/api/auth/logout":
            self._send_json(200, {"logout": True})
            return
        if auth not in ("Bearer tok_access_1", "Bearer tok_access_2"):
            self._send_json(401, {"message": "Unauthorized"})
            return

        if self.path == "/api/auth/authenticated":
            self._send_json(
                200,
                {
                    "authenticated": True,
                    "user": {"id": "u1", "full_name": "Thomas Artist", "email": "artist@studio.com"},
                },
            )
        elif self.path == "/api/data/projects/open":
            self._send_json(200, [{"id": "p1", "name": "DemoFilm"}])
        elif self.path == "/api/data/projects/p1/episodes":
            self._send_json(200, [{"id": "ep1", "name": "EP01"}])
        elif self.path == "/api/data/projects/p1/sequences":
            self._send_json(200, [{"id": "seq1", "name": "R1_SC03", "parent_id": "ep1"}])
        elif self.path in ("/api/data/projects/p1/task-types", "/api/data/task-types"):
            self._send_json(
                200,
                [
                    {"id": "tt_prep", "name": "Prepare"},
                    {"id": "tt_comp", "name": "Compositing"},
                    {"id": "tt_anim", "name": "Animation"},
                ],
            )
        elif self.path in ("/api/data/projects/p1/settings/task-status", "/api/data/task-status"):
            self._send_json(
                200,
                [
                    {"id": "st_todo", "name": "Todo", "short_name": "todo"},
                    {"id": "st_wip", "name": "WIP", "short_name": "wip"},
                    {"id": "st_dc", "name": "ĐợiCheck", "short_name": "doicheck"},
                ],
            )
        elif self.path == "/api/data/persons":
            self._send_json(200, [{"id": "u1", "full_name": "Thomas Artist"}])
        elif self.path == "/api/data/projects/p1/tasks":
            self._send_json(
                200,
                [
                    {
                        "id": "t_prep_1",
                        "entity_id": "s1",
                        "task_type_id": "tt_prep",
                        "task_status_id": "st_wip",
                        "assignees": ["u1"],
                    },
                    {
                        "id": "t_comp_1",
                        "entity_id": "s1",
                        "task_type_id": "tt_comp",
                        "task_status_id": "st_todo",
                        "assignees": ["u1"],
                    },
                ],
            )
        elif self.path == "/api/data/shots/s1/tasks":
            self._send_json(
                200,
                [
                    {"id": "t_prep_1", "task_type_id": "tt_prep", "task_status_id": "st_wip"},
                    {"id": "t_comp_1", "task_type_id": "tt_comp", "task_status_id": "st_todo"},
                ],
            )
        elif self.path == "/api/data/projects/p1/shots":
            self._send_json(
                200,
                [
                    {
                        "id": "s1",
                        "name": "010",
                        "parent_id": "seq1",
                        "nb_frames": 48,
                        "canceled": False,
                        "data": {"fps": 24, "resolution": "1920x1080"},
                    },
                    {
                        "id": "s2",
                        "name": "SH020",
                        "parent_id": "seq1",
                        "nb_frames": None,
                        "canceled": False,
                        "data": {"frame_in": 1001, "frame_out": 1024},
                    },
                    {
                        "id": "s3",
                        "name": "030",
                        "parent_id": "seq1",
                        "nb_frames": 12,
                        "canceled": True,
                        "data": {},
                    },
                ],
            )
        else:
            self._send_json(404, {"message": "Not found"})


class KitsuClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _MockKitsuHandler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_normalize_url(self):
        self.assertEqual(kc.normalize_api_url("https://kitsu.studio.com/"), "https://kitsu.studio.com/api")
        self.assertEqual(kc.normalize_api_url("http://127.0.0.1:8000/api/"), "http://127.0.0.1:8000/api")
        self.assertEqual(kc.normalize_api_url("kitsu.local"), "https://kitsu.local/api")

    def test_login_fetch_tasks_and_publish_update(self):
        _MockKitsuHandler.posted_comments.clear()
        _MockKitsuHandler.uploaded_attachments.clear()

        client = kc.KitsuClient(self.base_url)
        self.assertTrue(client.host_is_up())
        self.assertTrue(client.validate_url())

        with self.assertRaises(kc.KitsuAuthError):
            client.login("artist@studio.com", "wrong")

        client.login("artist@studio.com", "secret")
        user = client.get_current_user()
        self.assertEqual(user["full_name"], "Thomas Artist")

        records = client.get_shots("p1")
        self.assertEqual([r["name"] for r in records], ["010", "SH020"])
        self.assertEqual(records[0]["frames"], 48)
        self.assertEqual(records[1]["frames"], 24)
        self.assertEqual(records[0]["assignees"], ["Thomas Artist"])
        self.assertEqual(records[0]["tasks"]["Prepare"]["status"], "WIP")
        self.assertEqual(records[0]["tasks"]["Compositing"]["status"], "Todo")

        shots = dkt.shots_from_kitsu(records)
        self.assertEqual([s.name for s in shots], ["R1_SC03_010", "SH020"])
        self.assertEqual(shots[0].kitsu_id, "s1")

        # Test frame 1 JPG capture + Kitsu Compositing -> ĐợiCheck + comment + attachment
        with tempfile.TemporaryDirectory() as td:
            src_png = Path(td) / "frame_0001.png"
            out_jpg = Path(td) / "frame1.jpg"
            img = QImage(32, 32, QImage.Format.Format_RGB32)
            img.fill(0xFF336699)
            self.assertTrue(img.save(str(src_png), "PNG"))

            captured = si.capture_first_frame_jpg([src_png], out_jpg)
            self.assertIsNotNone(captured)
            self.assertTrue(out_jpg.is_file() and out_jpg.stat().st_size > 0)

            client.update_shot_task_on_publish(
                project_id="p1",
                shot_id="s1",
                dest_path=r"W:\PUBLISH\261120\R1_SC03_010",
                attachment_path=captured,
            )

        self.assertEqual(len(_MockKitsuHandler.posted_comments), 1)
        self.assertEqual(_MockKitsuHandler.posted_comments[0]["task_status_id"], "st_dc")
        self.assertIn(r"W:\PUBLISH\261120\R1_SC03_010", _MockKitsuHandler.posted_comments[0]["comment"])
        self.assertEqual(len(_MockKitsuHandler.uploaded_attachments), 1)
        self.assertIn(b"frame1.jpg", _MockKitsuHandler.uploaded_attachments[0])

    def test_token_refresh_on_401(self):
        client = kc.KitsuClient(self.base_url)
        client.set_token({"access_token": "expired_tok", "refresh_token": "tok_refresh_1"})
        projects = client.get_projects()
        self.assertEqual(projects[0]["name"], "DemoFilm")
        self.assertEqual(client.access_token, "tok_access_2")

    def test_table_and_excel_shot_import(self):
        # Shotgun + FileMaker + Scene-grouped + Digitopia start/end frame patterns
        shotgun_rows = [
            ["Shot Code", "Frame Count"],
            ["shot_01", "1-286"],
            ["shot_02", "1-388"],
        ]
        self.assertEqual(
            si.parse_shots_from_table(shotgun_rows),
            [
                {"name": "shot_01", "frames": 286, "seq": ""},
                {"name": "shot_02", "frames": 388, "seq": ""},
            ],
        )

        digitopia_rows = [
            ["Shot", "Description", "start frame", "end frame"],
            ["005_MAMMOTH_0020", "Libra", "1", "86"],
            ["006_LFIG_0070", "Eyes", "93", "148"],
        ]
        self.assertEqual(
            si.parse_shots_from_table(digitopia_rows),
            [
                {"name": "005_MAMMOTH_0020", "frames": 86, "seq": ""},
                {"name": "006_LFIG_0070", "frames": 56, "seq": ""},
            ],
        )

        scene_rows = [
            ["Shot", "Description"],
            ["Scene 001 - ESTB belfast", ""],
            ["Shot_001", "Drone"],
            ["Scene 002 - Inshops", ""],
            ["Shot_001", "Car"],
        ]
        self.assertEqual(
            si.parse_shots_from_table(scene_rows),
            [
                {"name": "Scene_001_Shot_001", "frames": None, "seq": "Scene_001"},
                {"name": "Scene_002_Shot_001", "frames": None, "seq": "Scene_002"},
            ],
        )

        # Minimal .xlsx reader test
        with tempfile.TemporaryDirectory() as td:
            xlsx = Path(td) / "shots.xlsx"
            with zipfile.ZipFile(xlsx, "w") as zf:
                zf.writestr(
                    "xl/sharedStrings.xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    "<si><t>vfx_name</t></si><si><t>duration</t></si>"
                    "<si><t>MK8010</t></si><si><t>MK8020</t></si>"
                    "</sst>",
                )
                zf.writestr(
                    "xl/worksheets/sheet1.xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    "<sheetData>"
                    '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                    '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>129</v></c></row>'
                    '<row r="3"><c r="A3" t="s"><v>3</v></c><c r="B3"><v>94</v></c></row>'
                    "</sheetData></worksheet>",
                )
            rows = si.read_excel_rows(xlsx)
            self.assertEqual(
                si.parse_shots_from_table(rows),
                [
                    {"name": "MK8010", "frames": 129, "seq": ""},
                    {"name": "MK8020", "frames": 94, "seq": ""},
                ],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
