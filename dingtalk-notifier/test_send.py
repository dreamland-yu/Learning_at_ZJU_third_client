"""Offline checks. Every HTTP call is replaced with a local fake."""
import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

import send

BOT = {"mode": "bot", "client_id": "test-app", "client_secret": "private-app-secret",
       "robot_code": "test-robot", "user_id": "private-user"}
WORK = {**BOT, "mode": "work_notice", "agent_id": 123}
TOKEN = {"accessToken": "private-token", "expireIn": 7200}


class FakeHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, body, **kwargs):
        self.calls.append((url, body, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeTime:
    def __init__(self):
        self.now = 0

    def clock(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class NotifierTests(unittest.TestCase):
    def assert_private(self, result):
        text = json.dumps(result, ensure_ascii=False)
        for value in (BOT["user_id"], BOT["client_secret"], TOKEN["accessToken"], "hook-secret", "hook-token"):
            self.assertNotIn(value, text)

    def test_real_cli_smoke_and_default_preview_do_not_network(self):
        with patch("urllib.request.build_opener", side_effect=AssertionError("network forbidden")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(send.main(["--smoke"]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "preview")
            self.assertIs(result["sent"], False)
            with tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "private.json"
                config.write_text(json.dumps(BOT), encoding="utf-8")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(send.main(["--config", str(config), "--message",
                        "private-user private-app-secret"]), 0)
                self.assert_private(json.loads(output.getvalue()))

    def test_bot_contract_and_accepted_is_not_delivery(self):
        http = FakeHTTP(TOKEN, {"processQueryKey": "opaque-key"})
        result = send.send(BOT, "hello", post=http)
        self.assertEqual(http.calls[0][0], send.TOKEN_URL)
        self.assertEqual(http.calls[0][1], {"appKey": "test-app", "appSecret": "private-app-secret"})
        url, body, kwargs = http.calls[1]
        self.assertEqual(url, send.BOT_URL)
        self.assertEqual(body["userIds"], [BOT["user_id"]])
        self.assertEqual(body["msgKey"], "sampleText")
        self.assertEqual(json.loads(body["msgParam"]), {"content": "hello"})
        self.assertEqual(kwargs["headers"]["x-acs-dingtalk-access-token"], TOKEN["accessToken"])
        self.assertEqual(result["status"], "accepted")
        self.assertFalse(result["delivery_confirmed"])
        self.assertNotIn("opaque-key", json.dumps(result))
        self.assert_private(result)

    def test_cli_requires_explicit_send_to_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "private.json"
            config.write_text(json.dumps(BOT), encoding="utf-8")
            with patch.object(send, "send", return_value={"status": "accepted", "sent": True}) as dispatch:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(send.main(["--config", str(config)]), 0)
                    dispatch.assert_not_called()
                    self.assertEqual(send.main(["--config", str(config), "--send"]), 0)
                dispatch.assert_called_once_with(BOT, send.DEFAULT_MESSAGE)

    def test_bot_invalid_or_flow_controlled_recipient_is_failure(self):
        for field in ("invalidStaffIdList", "flowControlledStaffIdList"):
            with self.subTest(field=field):
                result = send.send(BOT, "hello", post=FakeHTTP(TOKEN,
                    {"processQueryKey": "key", field: [BOT["user_id"]]}))
                self.assertEqual(result["status"], "failed")
                self.assert_private(result)

    def test_work_failure_and_partial_failure_are_not_success(self):
        for data, expected in (({"failed_user_id_list": [BOT["user_id"]]}, "failed"),
                ({"read_user_id_list": [BOT["user_id"]], "forbidden_user_id_list": ["other-user"]}, "partial_failure")):
            http = FakeHTTP(TOKEN, {"errcode": 0, "task_id": 42}, {"errcode": 0, "send_result": data})
            result = send.send(WORK, "hello", post=http)
            self.assertEqual(result["status"], expected)
            self.assertFalse(result["delivery_confirmed"])
            self.assertEqual(http.calls[1][1]["userid_list"], BOT["user_id"])
            self.assertEqual(http.calls[2][1], {"agent_id": 123, "task_id": 42})
            self.assertTrue(http.calls[2][0].startswith(send.WORK_BASE + "getsendresult?"))
            self.assert_private(result)

    def test_work_read_or_unread_list_confirms_platform_result(self):
        for field in ("read_user_id_list", "unread_user_id_list"):
            result = send.send(WORK, "hello", post=FakeHTTP(TOKEN, {"errcode": 0, "task_id": 42},
                {"errcode": 0, "send_result": {field: [BOT["user_id"]]}}))
            self.assertEqual(result["status"], "platform_confirmed")
            self.assertEqual(result["read"], field == "read_user_id_list")
            self.assertFalse(result["delivery_confirmed"])

    def test_work_forbidden_list_is_failure_even_with_read_entry(self):
        result = send.send(WORK, "hello", post=FakeHTTP(TOKEN, {"errcode": 0, "task_id": 42},
            {"errcode": 0, "send_result": {"read_user_id_list": [BOT["user_id"]],
                "forbidden_list": [{"code": "143105", "count": 500, "userid": BOT["user_id"]}]}}))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_count"], 1)
        self.assertFalse(result["delivery_confirmed"])
        self.assert_private(result)

    def test_work_pending_is_bounded_and_send_is_not_retried(self):
        timer = FakeTime()
        http = FakeHTTP(TOKEN, {"errcode": 0, "task_id": 42},
                        *[{"errcode": 0, "send_result": {}} for _ in range(8)])
        result = send.send(WORK, "hello", post=http, clock=timer.clock, sleep=timer.sleep)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(timer.now, 15)
        self.assertEqual(sum("asyncsend_v2" in call[0] for call in http.calls), 1)
        self.assert_private(result)

    def test_result_query_failure_preserves_pending(self):
        result = send.send(WORK, "hello", post=FakeHTTP(TOKEN, {"errcode": 0, "task_id": 42},
            {"errcode": 40014, "errmsg": TOKEN["accessToken"] + BOT["client_secret"]}))
        self.assertEqual(result["status"], "pending")
        self.assert_private(result)

    def test_business_errors_and_tokens_are_not_exposed(self):
        for code in ("invalidParameter.userIds.empty", TOKEN["accessToken"], BOT["client_secret"], BOT["user_id"]):
            http = FakeHTTP(TOKEN, {"code": code, "message": TOKEN["accessToken"]})
            result = send.send(BOT, "hello", post=http)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(len(http.calls), 2)
            self.assert_private(result)

    def test_transport_builds_post_and_http_error_does_not_echo_url_body(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://oapi.dingtalk.com?access_token=private-token", 403,
            "private-user private-app-secret", {}, io.BytesIO(b"private-token"))
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(send.SafeError) as caught:
                send.post_json(send.BOT_URL, {"content": "hello"}, {"x-acs-dingtalk-access-token": "private-token"})
        request = opener.open.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"content": "hello"})
        self.assertEqual(request.get_header("X-acs-dingtalk-access-token"), "private-token")
        self.assertIn("403", str(caught.exception))
        self.assert_private({"error": str(caught.exception)})
        self.assertEqual(opener.open.call_count, 1)

    def test_missing_mode_credentials_and_unused_fields(self):
        for mode in ("bot", "work_notice", "webhook"):
            with self.assertRaises(send.SafeError):
                send.validate({"mode": mode})
        send.validate({**BOT, "agent_id": "", "webhook": ""})
        with self.assertRaises(send.SafeError):
            send.validate({**WORK, "agent_id": 1.5})

    def test_send_timeout_is_unknown_and_does_not_resend(self):
        http = FakeHTTP(TOKEN, send.SafeError("网络请求未获得确认。", uncertain=True))
        result = send.send(BOT, "hello", post=http)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["sent"])
        self.assertEqual(len(http.calls), 2)
        self.assert_private(result)

    def test_webhook_signing_and_no_app_credentials_required(self):
        config = {"mode": "webhook", "webhook": "https://oapi.dingtalk.com/robot/send?access_token=hook-token", "secret": "hook-secret"}
        url = send.signed_webhook(config["webhook"], config["secret"], 123456789)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertEqual(query["timestamp"], "123456789")
        self.assertEqual(query["access_token"], "hook-token")
        self.assertEqual(query["sign"], "VxQ0iHF2UV8XzS0Rv32YB+8/qZdbbLwuq6Jx6hKuZ0s=")
        http = FakeHTTP({"errcode": 0, "errmsg": "ok"})
        result = send.send(config, "hello", post=http)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0][1], {"msgtype": "text", "text": {"content": "hello"}})
        self.assertEqual(result["status"], "accepted")
        self.assert_private(result)
        self.assert_private(send.preview(config, "hook-token hook-secret " + config["webhook"]))


if __name__ == "__main__":
    unittest.main()
