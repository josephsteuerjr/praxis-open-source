import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest import mock

import body_client
import run_context


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class _RawResponse(_Response):
    def read(self, amount=None):
        payload = bytes(self.payload)
        return payload if amount is None else payload[:amount]


class BodyClientCase(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "PRAXIS_BODY_CONTROLLER_TOKEN": "controller-secret",
            "PRAXIS_BODY_DEVICE": "windows-pc",
            "PRAXIS_BODY_BRIDGE_URL": "http://bridge.local",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_submit_preserves_system_execution_and_request_id(self):
        seen = {}

        def opened(request, timeout=0):
            seen["url"] = request.full_url
            seen["payload"] = json.loads(request.data)
            return _Response({"ok": True, "queued": True,
                              "request_id": seen["payload"]["request_id"],
                              "operation_id": seen["payload"]["operation_id"]})

        with mock.patch("urllib.request.urlopen", side_effect=opened):
            result = body_client.submit("process.start", {"program": "whoami.exe"},
                                        execution="system", request_id="stable-id",
                                        operation_id="op-stable")
        self.assertTrue(result["ok"])
        self.assertEqual(seen["payload"]["execution"], "system")
        self.assertEqual(seen["payload"]["request_id"], "stable-id")
        self.assertIn("/v1/controller/windows-pc/invoke", seen["url"])

    def test_call_sends_an_absolute_wire_deadline_for_long_local_routes(self):
        with mock.patch.object(body_client, "submit", return_value={
            "ok": True, "request_id": "deadline-request", "operation_id": "deadline-operation",
        }) as submitted, mock.patch.object(body_client, "response", return_value={
            "ok": True,
            "response": {
                "type": "result", "ok": True, "request_id": "deadline-request",
                "operation_id": "deadline-operation", "result": {"value": 1},
            },
        }):
            result = body_client.call("fs.export", {"path": r"C:\large.bin"}, timeout=600)

        self.assertTrue(result["ok"])
        wire = submitted.call_args.kwargs["deadline"]
        parsed = body_client.dt.datetime.fromisoformat(wire.replace("Z", "+00:00"))
        remaining = (parsed - body_client.dt.datetime.now(body_client.dt.timezone.utc)).total_seconds()
        self.assertGreater(remaining, 590)
        self.assertLessEqual(remaining, 600)

    def test_failed_artifact_fetch_removes_partial_stage(self):
        payload = b"x" * (128 * 1024)
        artifact = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
            "name": "failed.bin",
        }
        calls = 0

        def opened(_request, timeout=0):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _RawResponse(payload[:64 * 1024])
            raise urllib.error.URLError("connection lost")

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch("urllib.request.urlopen", side_effect=opened):
            destination = Path(directory) / "failed.bin"
            result = body_client.fetch_artifact(
                artifact, destination, chunk_size=64 * 1024,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "artifact_download")
            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(directory).glob("*.part")), [])

    def test_empty_artifact_fetch_is_hash_verified_and_committed(self):
        artifact = {
            "sha256": hashlib.sha256(b"").hexdigest(),
            "size": 0,
            "name": "empty.bin",
        }
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch("urllib.request.urlopen") as opened:
            destination = Path(directory) / "empty.bin"
            result = body_client.fetch_artifact(artifact, destination)
            self.assertTrue(result["ok"])
            self.assertEqual(destination.read_bytes(), b"")
            opened.assert_not_called()

    def test_artifact_fetch_bounds_each_http_read_and_rejects_oversized_chunk(self):
        payload = b"z" * (64 * 1024)
        artifact = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
            "name": "bounded.bin",
        }
        amounts = []

        class OversizedResponse(_RawResponse):
            def read(self, amount=None):
                amounts.append(amount)
                return b"z" * int(amount or 0)

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch("urllib.request.urlopen", return_value=OversizedResponse(payload)):
            destination = Path(directory) / "bounded.bin"
            result = body_client.fetch_artifact(
                artifact, destination, chunk_size=64 * 1024,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(amounts, [64 * 1024 + 1])
            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(directory).glob("*.part")), [])

    def test_native_desktop_wrappers_use_typed_capabilities(self):
        with mock.patch.object(body_client, "call", return_value={"ok": True}) as called:
            body_client.desktop_status()
            body_client.desktop_window_list()
            body_client.desktop_window_activate("0x2a")
            body_client.desktop_input_perform([{"type": "text", "text": "hello"}])
            body_client.desktop_screen_capture()
            body_client.desktop_clipboard_read()
            body_client.desktop_clipboard_write("value")
            body_client.os_process_list()

        self.assertEqual(
            [(item.args[0], item.args[1]) for item in called.call_args_list],
            [
                ("desktop.status", {}),
                ("desktop.window.list", {"offset": 0, "limit": 2048,
                                         "visible_only": True, "title_contains": ""}),
                ("desktop.window.activate", {"hwnd": "0x2a", "restore": True,
                                             "timeout_ms": 1500}),
                ("desktop.input.perform", {
                    "events": [{"type": "text", "text": "hello"}],
                    "inter_event_delay_ms": 0,
                }),
                ("desktop.screen.capture", {"target": "desktop"}),
                ("desktop.clipboard.read", {"limit_chars": 1_000_000}),
                ("desktop.clipboard.write", {"text": "value"}),
                ("os.process.list", {"offset": 0, "limit": 2048, "name_contains": ""}),
            ],
        )
        self.assertTrue(all(item.kwargs["execution"] == "interactive"
                            for item in called.call_args_list))

    def test_status_probe_falls_back_to_boot_time_system_service(self):
        system = {
            "ok": True,
            "identity": {"kind": "system", "integrity": "system"},
            "manifest": {"execution_contexts": [
                {"kind": "interactive", "available": False},
                {"kind": "system", "available": True},
            ]},
        }
        with mock.patch.object(body_client, "call", side_effect=[
            {"ok": False, "code": "interactive_router", "error": "no session host"},
            system,
        ]) as called:
            probe = body_client.status_probe(timeout=2)

        self.assertTrue(probe["ok"])
        self.assertEqual(probe["probe_execution"], "system")
        self.assertEqual(probe["interactive_error"], "no session host")
        self.assertEqual(
            [item.kwargs["execution"] for item in called.call_args_list],
            ["interactive", "system"],
        )

        with mock.patch.object(body_client, "status_probe", return_value=probe):
            line = body_client.state_line()
        self.assertNotIn("offline", line)
        self.assertIn("interactive=not connected", line)
        self.assertIn("system=ready", line)

    def test_unconfigured_client_fails_without_network(self):
        with mock.patch.dict(os.environ, {"PRAXIS_BODY_CONTROLLER_TOKEN": ""}):
            with mock.patch("urllib.request.urlopen") as opened:
                result = body_client.submit("body.status")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "unavailable")
        opened.assert_not_called()

    def test_observation_context_receives_one_structured_call_result(self):
        seen = []

        def sink(context, capability, args, execution, result):
            seen.append((context, capability, args, execution, result))

        with mock.patch.object(body_client, "submit", return_value={
            "ok": True, "request_id": "req-observed", "operation_id": "op-observed",
        }), mock.patch.object(body_client, "response", return_value={
            "ok": True, "response": {"type": "result", "ok": True,
                                      "request_id": "req-observed", "operation_id": "op-observed",
                                      "result": {"status": "succeeded"}},
        }):
            with body_client.observation_context(
                {"id": "wcode-1", "root": r"C:\work", "device_id": "pc"}, sink=sink,
            ):
                result = body_client.call("fs.stat", {"path": r"C:\work"})

        self.assertTrue(result["ok"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0]["task_id"], "wcode-1")
        self.assertEqual(seen[0][1], "fs.stat")
        self.assertEqual(seen[0][4]["status"], "succeeded")

    def test_direct_body_call_is_attributed_to_bound_durable_run(self):
        current = run_context.RunContext.create(
            kind="chat_turn", goal="look around", principal_id="1", scope="owner",
        ).with_status("running")
        with mock.patch("computer_memory.record") as record, \
                mock.patch.object(body_client, "device_id", return_value="windows-pc"), \
                run_context.bind_run(current):
            result = body_client._observed(
                "desktop.window.list", {"visible_only": True}, "interactive",
                {"ok": True, "windows": []},
            )

        self.assertTrue(result["ok"])
        context = record.call_args.args[0]
        self.assertEqual(context["task_id"], current.run_id)
        self.assertEqual(context["run_id"], current.run_id)
        self.assertEqual(context["goal"], "look around")

    def test_workspace_write_omits_empty_hash_precondition(self):
        with mock.patch.object(body_client, "call", return_value={"ok": True}) as called:
            body_client.workspace_edit(r"C:\work", "write", path="new.txt", content="new",
                                       expected_sha256="")
        capability, payload = called.call_args.args
        self.assertEqual(capability, "fs.write_atomic")
        self.assertNotIn("expected_sha256", payload)

        with mock.patch.object(body_client, "call", return_value={"ok": True}) as called:
            body_client.workspace_edit(r"C:\work", "write", path="old.txt", content="new",
                                       expected_sha256="a" * 64)
        self.assertEqual(called.call_args.args[1]["expected_sha256"], "a" * 64)

    def test_workspace_inspect_failure_keeps_remote_diagnostics(self):
        with mock.patch.object(body_client, "workspace_inspect_result", return_value={
            "ok": False, "text": "fatal: not a git repository",
        }):
            out = body_client.workspace_inspect(r"C:\plain", "diff")
        self.assertEqual(out, "[windows-body] fatal: not a git repository")

    def test_run_argv_can_request_a_large_result_tail(self):
        with mock.patch.object(body_client, "process_start", return_value={
            "ok": True, "operation_id": "op-inventory",
        }), mock.patch.object(body_client, "process_status", return_value={
            "ok": True, "status": "succeeded", "stdout_tail": "{}",
        }) as status:
            result = body_client.run_argv("powershell.exe", ["-Command", "inventory"],
                                          tail=1_000_000)
        self.assertTrue(result["ok"])
        status.assert_called_once_with("op-inventory", tail=1_000_000,
                                       execution="interactive")

    def test_upload_artifact_resumes_on_server_grid_and_streams_raw_chunks(self):
        chunk_size = 64 * 1024
        content = b"a" * chunk_size + b"b" * chunk_size + b"tail-bytes"
        sha256 = hashlib.sha256(content).hexdigest()
        status_calls = 0
        puts = []
        offered = None

        def opened(request, timeout=0):
            nonlocal status_calls, offered
            headers = {key.lower(): value for key, value in request.header_items()}
            self.assertEqual(headers["authorization"], "Bearer controller-secret")
            url = request.full_url
            if url.endswith(f"/v1/artifacts/{sha256}/offer"):
                offered = json.loads(request.data)
                return _Response({"ok": True, "complete": False})
            if url.endswith(f"/v1/artifacts/{sha256}"):
                status_calls += 1
                return _Response({
                    "artifact": offered,
                    "complete": status_calls > 1,
                    "received_offsets": [0] if status_calls == 1 else [],
                    "chunk_size": chunk_size,
                })
            if f"/v1/artifacts/{sha256}/chunks/" in url:
                offset = int(url.rsplit("/", 1)[1])
                puts.append((offset, request.data, headers))
                self.assertEqual(
                    headers["x-praxis-chunk-sha256"],
                    hashlib.sha256(request.data).hexdigest(),
                )
                self.assertEqual(headers["x-praxis-total-size"], str(len(content)))
                self.assertEqual(bytes.fromhex(headers["x-praxis-name-hex"]), b"payload.bin")
                return _Response({
                    "ok": True, "sha256": sha256, "offset": offset,
                    "size": len(request.data),
                })
            if url.endswith(f"/v1/artifacts/{sha256}/complete"):
                return _Response({"ok": True, "artifact": offered})
            raise AssertionError(f"unexpected request: {request.get_method()} {url}")

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "payload.bin"
            source.write_bytes(content)
            with mock.patch("urllib.request.urlopen", side_effect=opened):
                result = body_client.upload_artifact(source)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["artifact"]["sha256"], sha256)
        self.assertEqual(result["artifact"]["source_device"], "praxis-server")
        self.assertEqual(result["resumed_offsets"], [0])
        self.assertEqual(result["uploaded_offsets"], [chunk_size, chunk_size * 2])
        self.assertEqual(
            [(offset, data) for offset, data, _headers in puts],
            [
                (chunk_size, content[chunk_size:chunk_size * 2]),
                (chunk_size * 2, content[chunk_size * 2:]),
            ],
        )

    def test_upload_artifact_completes_zero_byte_without_put(self):
        content = b""
        sha256 = hashlib.sha256(content).hexdigest()
        status_calls = 0
        methods = []
        artifact = {
            "sha256": sha256, "size": 0, "name": "empty.txt",
            "mime": "text/plain", "source_device": "praxis-server",
        }

        def opened(request, timeout=0):
            nonlocal status_calls
            methods.append((request.get_method(), request.full_url))
            if request.full_url.endswith("/offer"):
                self.assertEqual(json.loads(request.data), artifact)
                return _Response({"ok": True, "complete": False})
            if request.full_url.endswith("/complete"):
                return _Response({"ok": True, "artifact": artifact})
            if request.full_url.endswith(sha256):
                status_calls += 1
                return _Response({
                    "artifact": artifact, "complete": status_calls > 1,
                    "received_offsets": [], "chunk_size": 64 * 1024,
                })
            raise AssertionError(request.full_url)

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "empty.txt"
            source.write_bytes(content)
            with mock.patch("urllib.request.urlopen", side_effect=opened):
                result = body_client.upload_artifact(source)

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["reused"])
        self.assertFalse(any(method == "PUT" for method, _url in methods))
        self.assertTrue(any(url.endswith("/complete") for _method, url in methods))

    def test_upload_artifact_rejects_invalid_resume_grid_before_chunks(self):
        content = b"grid"
        sha256 = hashlib.sha256(content).hexdigest()
        seen = []

        def opened(request, timeout=0):
            seen.append(request.get_method())
            if request.full_url.endswith("/offer"):
                return _Response({"ok": True})
            if request.full_url.endswith(sha256):
                artifact = json.loads(seen_artifact[0])
                return _Response({
                    "artifact": artifact, "complete": False,
                    "received_offsets": [1], "chunk_size": 64 * 1024,
                })
            raise AssertionError(request.full_url)

        seen_artifact = []

        def capture(request, timeout=0):
            if request.full_url.endswith("/offer"):
                seen_artifact.append(request.data.decode())
            return opened(request, timeout)

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "grid.bin"
            source.write_bytes(content)
            with mock.patch("urllib.request.urlopen", side_effect=capture):
                result = body_client.upload_artifact(source)

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "artifact_protocol")
        self.assertEqual(result["stage"], "status")
        self.assertNotIn("PUT", seen)

    def test_upload_artifact_accumulates_short_file_reads(self):
        content = b"short-read-source" * 20
        sha256 = hashlib.sha256(content).hexdigest()
        artifact = {
            "sha256": sha256, "size": len(content), "name": "short.bin",
            "mime": "application/octet-stream", "source_device": "praxis-server",
        }
        status_calls = 0
        put_bodies = []

        class ShortReader(io.BytesIO):
            def read(self, size=-1):
                if size < 0:
                    size = 7
                return super().read(min(size, 7))

        def opened(request, timeout=0):
            nonlocal status_calls
            if request.full_url.endswith("/offer"):
                return _Response({"ok": True})
            if request.full_url.endswith(sha256):
                status_calls += 1
                return _Response({
                    "artifact": artifact, "complete": status_calls > 1,
                    "received_offsets": [], "chunk_size": 64 * 1024,
                })
            if "/chunks/" in request.full_url:
                put_bodies.append(request.data)
                return _Response({
                    "ok": True, "sha256": sha256, "offset": 0,
                    "size": len(request.data),
                })
            if request.full_url.endswith("/complete"):
                return _Response({"ok": True, "artifact": artifact})
            raise AssertionError(request.full_url)

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "short.bin"
            source.write_bytes(content)
            with mock.patch.object(body_client.Path, "open", return_value=ShortReader(content)), \
                    mock.patch("urllib.request.urlopen", side_effect=opened):
                result = body_client.upload_artifact(source)

        self.assertTrue(result["ok"], result)
        self.assertEqual(put_bodies, [content])

    def test_upload_artifact_surfaces_conflict_without_secret_or_content(self):
        content = b"do-not-echo-this-content"

        def opened(request, timeout=0):
            raise urllib.error.HTTPError(
                request.full_url, 409, "Conflict", {},
                io.BytesIO(json.dumps({"ok": False, "error": "artifact offer conflict"}).encode()),
            )

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "conflict.bin"
            source.write_bytes(content)
            with mock.patch("urllib.request.urlopen", side_effect=opened):
                result = body_client.upload_artifact(source)

        rendered = json.dumps(result)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "http_409")
        self.assertEqual(result["stage"], "offer")
        self.assertNotIn("controller-secret", rendered)
        self.assertNotIn(content.decode(), rendered)

    def test_upload_artifact_rejects_wrong_completion_artifact(self):
        content = b"completion-mismatch"
        sha256 = hashlib.sha256(content).hexdigest()
        artifact = {
            "sha256": sha256, "size": len(content), "name": "value.bin",
            "mime": "application/octet-stream", "source_device": "praxis-server",
        }

        def opened(request, timeout=0):
            if request.full_url.endswith("/offer"):
                return _Response({"ok": True})
            if request.full_url.endswith(sha256):
                return _Response({
                    "artifact": artifact, "complete": False,
                    "received_offsets": [], "chunk_size": 64 * 1024,
                })
            if "/chunks/" in request.full_url:
                return _Response({
                    "ok": True, "sha256": sha256, "offset": 0,
                    "size": len(content),
                })
            if request.full_url.endswith("/complete"):
                return _Response({"ok": True, "artifact": {**artifact, "sha256": "f" * 64}})
            raise AssertionError(request.full_url)

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "value.bin"
            source.write_bytes(content)
            with mock.patch("urllib.request.urlopen", side_effect=opened):
                result = body_client.upload_artifact(source)

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "artifact_mismatch")
        self.assertEqual(result["stage"], "complete")

    def test_import_artifact_calls_fs_import_with_exact_ref_and_evidence(self):
        artifact = {
            "sha256": "a" * 64, "size": 17, "name": "report.txt",
            "mime": "text/plain", "source_device": "praxis-server",
        }
        upload = {
            "ok": True, "artifact": artifact, "complete": True, "reused": False,
            "chunk_size": 64 * 1024, "resumed_offsets": [0],
            "uploaded_offsets": [64 * 1024],
        }
        imported = {
            "ok": True, "artifact": artifact, "path": r"C:\Reports\report.txt",
            "resumed_from": 0,
        }
        with mock.patch.object(body_client, "upload_artifact", return_value=upload) as uploaded, \
                mock.patch.object(body_client, "call", return_value=imported) as called:
            result = body_client.import_artifact_to_windows(
                "/srv/report.txt", r"C:\Reports\report.txt", "system", name="report.txt",
            )

        uploaded.assert_called_once_with("/srv/report.txt", name="report.txt")
        called.assert_called_once_with(
            "fs.import",
            {"artifact": artifact, "path": r"C:\Reports\report.txt"},
            execution="system", timeout=600.0,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["artifact"], artifact)
        self.assertEqual(result["upload"]["resumed_offsets"], [0])


class NewBodyVerbsCase(unittest.TestCase):
    """Проводка глаголов тела v2: чтение окна, склейка, ожидание, четыре файловых."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "PRAXIS_BODY_CONTROLLER_TOKEN": "controller-secret",
            "PRAXIS_BODY_DEVICE": "windows-pc",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_every_new_verb_is_called_by_its_exact_name_and_arguments(self):
        with mock.patch.object(body_client, "call",
                               side_effect=lambda *a, **k: {"ok": True}) as called:
            body_client.desktop_window_read(hwnd="0x1F4", shape="flat",
                                            text_contains="save", visible_only=True,
                                            max_nodes=50, timeout_ms=3000)
            body_client.desktop_input_perform_and_read(
                [{"type": "click", "button": "left"}], settle_ms=200,
                read={"text_contains": "сохранить"},
            )
            body_client.desktop_window_wait("text", text_contains="готово",
                                            timeout_ms=9000, hwnd=500)
            body_client.fs_delete(r"C:\tmp\junk", recursive=True)
            body_client.fs_move(r"C:\a.txt", r"C:\b.txt", overwrite=True)
            body_client.fs_copy(r"C:\src", r"C:\dst", recursive=True)
            body_client.fs_mkdir(r"C:\deep\tree", parents=False)

        self.assertEqual(
            [(item.args[0], item.args[1]) for item in called.call_args_list],
            [
                ("desktop.window.read", {"hwnd": "0x1F4", "shape": "flat",
                                         "text_contains": "save", "visible_only": True,
                                         "max_nodes": 50, "timeout_ms": 3000}),
                ("desktop.input.perform_and_read", {
                    "input": {"events": [{"type": "click", "button": "left"}],
                              "inter_event_delay_ms": 0},
                    "settle_ms": 200,
                    "read": {"text_contains": "сохранить"},
                }),
                ("desktop.window.wait", {"for": "text", "text_contains": "готово",
                                         "hwnd": 500, "timeout_ms": 9000}),
                ("fs.delete", {"path": r"C:\tmp\junk", "recursive": True}),
                ("fs.move", {"from": r"C:\a.txt", "to": r"C:\b.txt", "overwrite": True}),
                ("fs.copy", {"from": r"C:\src", "to": r"C:\dst",
                             "recursive": True, "overwrite": False}),
                ("fs.mkdir", {"path": r"C:\deep\tree", "parents": False}),
            ],
        )
        self.assertTrue(all(item.kwargs["execution"] == "interactive"
                            for item in called.call_args_list))

    def test_unasked_reading_sends_no_arguments_at_all(self):
        # У читалки deny_unknown_fields и все поля необязательны: подставив «дефолты»
        # сервера, мы бы стёрли разницу между «она не просила» и «она попросила ровно
        # столько» — и её просьба вне границ перестала бы объявляться телом в notes.
        with mock.patch.object(body_client, "call", return_value={"ok": True}) as called:
            body_client.desktop_window_read()
        self.assertEqual(called.call_args.args[1], {})

    def test_server_wait_outlives_the_body_budget_and_clamps_like_the_body(self):
        waits = {}

        def record(capability, _payload, **kwargs):
            waits[capability] = kwargs["timeout"]
            return {"ok": True}

        with mock.patch.object(body_client, "call", side_effect=record):
            body_client.desktop_window_read()
            self.assertAlmostEqual(waits["desktop.window.read"], 14.55)

            body_client.desktop_window_read(timeout_ms=20_000)
            self.assertAlmostEqual(waits["desktop.window.read"], 32.75)

            # Просьба выше потолка не должна уводить сервер ждать десять минут: тело
            # подтянет её к 20 с, и сервер обязан считать ожидание по тому же числу.
            body_client.desktop_window_read(timeout_ms=600_000)
            self.assertAlmostEqual(waits["desktop.window.read"], 32.75)

            body_client.desktop_window_wait("window", title_contains="Блокнот")
            self.assertAlmostEqual(waits["desktop.window.wait"], 18.2)

            # Последняя проба вправе перебрать общий бюджет ровно на свой срок —
            # 45 с ожидания плюс 20 с пробы, и сервер обязан пережить их сумму.
            body_client.desktop_window_wait("text", text_contains="x",
                                            timeout_ms=45_000, probe_timeout_ms=20_000)
            self.assertAlmostEqual(waits["desktop.window.wait"], 77.0)

            body_client.fs_delete(r"C:\tree", recursive=True)
            self.assertGreater(waits["fs.delete"], body_client.FS_TREE_BODY_SECONDS)

    def test_partial_tree_work_never_reads_as_finished(self):
        # `call()` перетирает собственный ok тела рамкой транспорта, поэтому оборванный
        # на 45 секундах обход приезжает как ok:true.  Расписка обязана назвать поле правды.
        with mock.patch.object(body_client, "call", return_value={
            "ok": True, "complete": False, "failed_count": 3, "failed_truncated": True,
            "stopped_because": "time limit reached: 45s of 45s allowed per call",
            "removed_files": 812,
        }):
            result = body_client.fs_delete(r"C:\tree", recursive=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["server_side"]["truth_field"], "complete")
        self.assertIs(result["server_side"]["truth"], False)
        self.assertIn("complete", result["server_side"]["note"])
        self.assertEqual(result["server_side"]["wait_s"], body_client.FS_TREE_WAIT_SECONDS)

    def test_timed_out_wait_is_not_an_error_and_says_so(self):
        with mock.patch.object(body_client, "call", return_value={
            "ok": True, "met": False, "timed_out": True, "waited_ms": 5012,
            "reason": "did not observe text within timeout_ms=5000",
        }):
            result = body_client.desktop_window_wait("text", text_contains="готово")

        self.assertTrue(result["ok"])
        self.assertEqual(result["server_side"]["truth_field"], "met")
        self.assertIs(result["server_side"]["truth"], False)
        self.assertIn("не увидела в срок", result["server_side"]["note"])

    def test_unread_window_is_named_as_unread_not_as_empty(self):
        with mock.patch.object(body_client, "call", return_value={
            "ok": True, "observed": False,
            "observation": {"kind": "unavailable", "error": "no foreground window"},
        }):
            result = body_client.desktop_input_perform_and_read([{"type": "key", "key": "f5"}])
        self.assertEqual(result["server_side"]["truth_field"], "observed")
        self.assertIs(result["server_side"]["truth"], False)

    def test_composed_input_is_assembled_by_the_same_code_as_the_plain_verb(self):
        events = [{"type": "mouse_down", "button": "left", "x": 10, "y": 20},
                  {"type": "drag", "x": 10, "y": 20, "to_x": 300, "to_y": 400}]
        with mock.patch.object(body_client, "call", return_value={"ok": True}) as called:
            body_client.desktop_input_perform(events, expected_foreground="0x2a",
                                              expected_pid=42, inter_event_delay_ms=5)
            plain = called.call_args.args[1]
            body_client.desktop_input_perform_and_read(events, expected_foreground="0x2a",
                                                       expected_pid=42,
                                                       inter_event_delay_ms=5)
            composed = called.call_args.args[1]["input"]
        self.assertEqual(plain, composed)
        self.assertEqual(plain["events"][1]["to_x"], 300)

    def test_window_read_text_keeps_every_limit_and_never_swallows_a_new_field(self):
        receipt = {
            "ok": True, "capability": "desktop.window.read", "backend": "win32",
            "backend_detail": "classic child windows only",
            "fallback_reason": "UIA worker did not answer within worker_grace_ms",
            "window": {"hwnd": "0x1234", "title": "Блокнот", "class": "Notepad",
                       "pid": 42, "foreground": True, "resolved_from": "foreground",
                       "rect": {"left": 0, "top": 0, "width": 800, "height": 600}},
            "shape": "tree",
            "root": {"id": 0, "depth": 0, "role": "window", "name": "Блокнот",
                     "children_unread": "max_depth",
                     "rect": {"left": 0, "top": 0, "width": 800, "height": 600,
                              "center": {"x": 400, "y": 300}},
                     "state": {"enabled": True},
                     "children": [{"id": 1, "parent": 0, "depth": 1, "role": "button",
                                   "name": "Сохранить", "text_truncated": True,
                                   "rect": {"center": {"x": 120, "y": 540}}}]},
            "nodes_read": 2, "nodes_returned": 2, "discovered_unread": 7,
            "subtrees_unread": 1, "skipped_offscreen": 0, "depth_reached": 1,
            "truncated": True, "truncated_by": ["max_depth"], "total_known": False,
            "limits": {"max_nodes": 400, "max_nodes_ceiling": 5000, "timeout_ms": 1800,
                       "worker_grace_ms": 750, "win32_text_timeout_ms": 150},
            "elapsed_ms": 615, "walk_ms": 605,
            "notes": ["win32 fallback reads classic child windows only"],
            "uia_workers_live": 1,
        }
        text = body_client.format_window_read(receipt)

        self.assertIn("backend=win32", text)
        self.assertIn("UIA worker did not answer", text)
        for limit in ("max_nodes=400", "max_nodes_ceiling=5000", "worker_grace_ms=750",
                      "win32_text_timeout_ms=150"):
            self.assertIn(limit, text)
        self.assertIn("truncated_by", text)
        self.assertIn("это НЕ всё окно", text)
        self.assertIn("children_unread=max_depth", text)
        self.assertIn("text_truncated", text)
        self.assertIn("@120,540", text)
        self.assertIn("classic child windows only", text)
        # Поле, которого свёртка не знает, обязано доехать в «прочем», а не исчезнуть:
        # ровно так тихо умер reflexes['mine'] в capabilities.describe().
        self.assertIn("uia_workers_live=1", text)
        self.assertIn("state:enabled=true", text)

    def test_window_read_text_says_not_found_instead_of_empty(self):
        text = body_client.format_window_read({
            "ok": True, "shape": "tree", "root": None, "nodes_read": 40,
            "filter": {"text_contains": "нет такого", "matched_or_ancestor": 0},
        })
        self.assertIn("root=null", text)
        self.assertIn("фильтр не нашёл ничего", text)

        failed = body_client.format_window_read({"ok": False, "error": "no such window"})
        self.assertIn("не ответил", failed)
        self.assertIn("no such window", failed)

    def test_unknown_impact_is_not_reported_as_nothing_changed(self):
        # Раньше сюда уезжал валидный JSON `{"changed": []}` под флагом ok:false, и
        # workspace_inspect заворачивал его в «[windows-body] …», ломая разбор у читателя.
        with mock.patch.object(body_client, "run_argv", return_value={
            "ok": False, "status": "failed", "stderr_tail": "fatal: not a git repository",
        }):
            structured = body_client.workspace_inspect_result(r"C:\plain", "impact")
            text = body_client.workspace_inspect(r"C:\plain", "impact")

        self.assertFalse(structured["ok"])
        self.assertNotIn("text", structured)
        self.assertIn("not a git repository", structured["error"])
        self.assertNotIn("changed", text)

        with mock.patch.object(body_client, "run_argv", return_value={
            "ok": True, "status": "succeeded", "stdout_tail": "src/a.rs\nsrc/b.rs\n",
        }):
            good = body_client.workspace_inspect(r"C:\repo", "impact")
        self.assertEqual(json.loads(good), {"changed": ["src/a.rs", "src/b.rs"]})


if __name__ == "__main__":
    unittest.main()
