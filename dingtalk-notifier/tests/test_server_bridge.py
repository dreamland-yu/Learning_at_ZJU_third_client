"""Server bridge tests: in-memory stores and httpx transports only, no real login."""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from lazy.server import monitor
from lazy.server.routers import assignments, data
from lazy.server.state import MonitorTask, UserSession


class MemoryStore:
    def __init__(self):
        self.reads = 0
        self.updated = []

    def get(self, studentid):
        self.reads += 1
        return {"password": "PRIVATE-PASSWORD"}

    def update_cookies(self, studentid, cookies):
        self.updated.append((studentid, cookies))


class ServerBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients = []
        self.store = MemoryStore()
        self.state = SimpleNamespace(credential_store=self.store, trust_env=False, sessions={})
        self.task = MonitorTask("rollcall_watch", "rollcall.rollcall", 30, id_field="rollcall_id")

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()

    def client(self, handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.clients.append(client)
        return client

    def user(self, handler):
        user = UserSession("PRIVATE-TOKEN", "SCHOOL-ACCOUNT", self.client(handler))
        user.tasks[self.task.task_id] = self.task
        self.state.sessions[user.token] = user
        return user

    async def call_api(self, path):
        app = FastAPI()
        app.state.server_state = self.state
        app.include_router(data.router)
        app.include_router(assignments.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            return await client.get(path, params={"token": "PRIVATE-TOKEN"})

    async def test_success_cache_has_fresh_timestamp_and_safe_api_metadata(self):
        user = self.user(lambda request: httpx.Response(200, json={"rollcalls": [{"rollcall_id": 7}]}))
        before = time.time()
        self.assertTrue(await monitor.poll_user_task_once(self.state, user, self.task))
        result = (await self.call_api("/api/data/rollcall_watch")).json()
        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(result["fetched_at"], before)
        self.assertFalse(result["stale"])
        self.assertIsNone(result["last_error"])
        self.assertEqual(self.store.reads, 0)

    async def test_request_failure_keeps_old_cache_but_marks_stale(self):
        response = {"status": 200}

        def handler(request):
            return httpx.Response(response["status"], json={"rollcalls": []},
                                  headers={"secret": "PRIVATE-COOKIE"})

        user = self.user(handler)
        await monitor.poll_user_task_once(self.state, user, self.task)
        old_time = user.cache_fetched_at[self.task.task_id]
        response["status"] = 500
        self.assertFalse(await monitor.poll_user_task_once(self.state, user, self.task))
        result = (await self.call_api("/api/data/rollcall_watch")).json()
        self.assertTrue(result["stale"])
        self.assertEqual(result["fetched_at"], old_time)
        self.assertEqual(result["data"], {"rollcalls": []})
        self.assertEqual(result["last_error"], "http_error")
        self.assertNotIn("PRIVATE", str(result))

    async def test_aged_or_disabled_cache_is_stale_and_pending_cache_has_no_timestamp(self):
        user = self.user(lambda request: httpx.Response(200, json=[]))
        result = (await self.call_api("/api/data/rollcall_watch")).json()
        self.assertEqual(result["status"], "pending")
        self.assertTrue(result["stale"])
        self.assertIsNone(result["fetched_at"])
        await monitor.poll_user_task_once(self.state, user, self.task)
        user.cache_fetched_at[self.task.task_id] = time.time() - 61
        self.assertTrue((await self.call_api("/api/data/rollcall_watch")).json()["stale"])
        user.cache_fetched_at[self.task.task_id] = time.time()
        self.task.enabled = False
        self.assertTrue((await self.call_api("/api/data/rollcall_watch")).json()["stale"])

    async def test_refresh_expired_session_once_then_cache_success(self):
        user = self.user(lambda request: httpx.Response(401, text="PRIVATE-PASSWORD PRIVATE-TOKEN"))
        replacement = self.client(lambda request: httpx.Response(200, json={"rollcalls": []}))
        replacement.cookies.set("session", "PRIVATE-COOKIE")
        candidate = SimpleNamespace(session=replacement, login=AsyncMock(return_value=True))
        with patch.object(monitor, "create_user_client", new=AsyncMock(return_value=candidate)) as create:
            self.assertTrue(await monitor.poll_user_task_once(self.state, user, self.task))
        self.assertEqual(create.await_count, 1)
        self.assertEqual(user.session_generation, 1)
        self.assertIs(user.zju_client, replacement)
        self.assertEqual(self.store.updated, [("SCHOOL-ACCOUNT", {"session": "PRIVATE-COOKIE"})])
        self.assertIsNone(user.cache_errors[self.task.task_id])

    async def test_failed_refresh_is_throttled_for_sixty_seconds(self):
        user = self.user(lambda request: httpx.Response(401, text="PRIVATE-PASSWORD"))
        candidate = SimpleNamespace(session=self.client(lambda request: httpx.Response(200)),
                                    login=AsyncMock(return_value=False))
        with patch.object(monitor, "create_user_client", new=AsyncMock(return_value=candidate)) as create:
            self.assertFalse(await monitor.poll_user_task_once(self.state, user, self.task))
            self.assertEqual(user.cache_errors[self.task.task_id], "auth_refresh_failed")
            self.assertFalse(await monitor.poll_user_task_once(self.state, user, self.task))
        self.assertEqual(create.await_count, 1)
        self.assertEqual(self.store.reads, 1)
        self.assertGreater(user.next_auth_refresh, time.monotonic() + 59)
        self.assertEqual(user.cache_errors[self.task.task_id], "auth_retry_backoff")
        self.assertTrue(candidate.session.is_closed)

    async def test_concurrent_monitors_only_refresh_once(self):
        user = self.user(lambda request: httpx.Response(401))
        replacement = self.client(lambda request: httpx.Response(200, json=[]))

        async def login(*args):
            await asyncio.sleep(0)
            return True

        candidate = SimpleNamespace(session=replacement, login=AsyncMock(side_effect=login))
        with patch.object(monitor, "create_user_client", new=AsyncMock(return_value=candidate)) as create:
            results = await asyncio.gather(*(monitor.poll_user_task_once(self.state, user, self.task) for _ in range(2)))
        self.assertEqual(results, [True, True])
        self.assertEqual(create.await_count, 1)
        self.assertEqual(candidate.login.await_count, 1)

    async def test_nonbusiness_response_is_not_cached(self):
        for kwargs, category in (({"text": "<html>upstream error</html>"}, "invalid_json"),
                                 ({"json": "not a business object"}, "invalid_payload"),
                                 ({"json": {"error": "PRIVATE-PASSWORD"}}, "business_error"),
                                 ({"json": {"message": "PRIVATE-TOKEN"}}, "business_error")):
            with self.subTest(category=category):
                user = self.user(lambda request: httpx.Response(200, **kwargs))
                self.assertFalse(await monitor.poll_user_task_once(self.state, user, self.task))
                self.assertNotIn(self.task.task_id, user.caches)
                self.assertEqual(user.cache_errors[self.task.task_id], category)

    def homework_handler(self, *, count=0, records=None, fail_submission=False, kind="homework"):
        self.paths = []

        def handler(request):
            self.paths.append(request.url.path)
            if request.url.path == "/api/activities/5":
                return httpx.Response(200, json={"id": 5, "title": "Homework", "type": kind,
                    "end_time": "2026-10-18T15:59:00Z", "user_submit_count": count})
            if request.url.path == "/user/index":
                return httpx.Response(200, text='<html><span value="9876" id="userId"></span></html>')
            if request.url.path == "/api/activities/5/students/9876/submission_list":
                return httpx.Response(500, text="PRIVATE-TOKEN") if fail_submission else httpx.Response(200,
                    json={"list": records if records is not None else []})
            return httpx.Response(404)
        return handler

    async def test_homework_zero_count_and_empty_records_confirm_not_submitted(self):
        self.user(self.homework_handler())
        result = (await self.call_api("/api/assignments/submission/5")).json()
        self.assertEqual(result["status"], "ok")
        self.assertIs(result["submitted"], False)
        self.assertEqual(result["title"], "Homework")
        self.assertTrue(any("/students/9876/" in path for path in self.paths))
        self.assertFalse(any("SCHOOL-ACCOUNT" in path for path in self.paths))

    async def test_homework_positive_count_and_records_confirm_submitted(self):
        self.user(self.homework_handler(count=1, records=[{"id": 77}]))
        result = (await self.call_api("/api/assignments/submission/5")).json()
        self.assertIs(result["submitted"], True)
        self.assertEqual(result["status"], "ok")

    async def test_submission_read_failure_is_unknown_not_unsubmitted(self):
        self.user(self.homework_handler(fail_submission=True))
        result = (await self.call_api("/api/assignments/submission/5")).json()
        self.assertIsNone(result["submitted"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["last_error"], "http_error")
        self.assertNotIn("PRIVATE", str(result))

    async def test_unsupported_or_contradictory_submission_is_unknown(self):
        for kwargs, category in (({"kind": "exam"}, "unsupported_type"),
                ({"count": None}, "submission_state_unknown"),
                ({"count": 1, "records": []}, "submission_state_conflict"),
                ({"count": 0, "records": [{"id": 77}]}, "submission_state_conflict")):
            with self.subTest(category=category):
                self.user(self.homework_handler(**kwargs))
                result = (await self.call_api("/api/assignments/submission/5")).json()
                self.assertIsNone(result["submitted"])
                self.assertEqual(result["last_error"], category)

    async def test_bad_internal_student_id_is_unknown_and_never_uses_school_account(self):
        base = self.homework_handler()

        def handler(request):
            return httpx.Response(200, text="<html>no current user</html>") if request.url.path == "/user/index" else base(request)

        self.user(handler)
        result = (await self.call_api("/api/assignments/submission/5")).json()
        self.assertIsNone(result["submitted"])
        self.assertEqual(result["last_error"], "student_id_unavailable")
        self.assertEqual(self.paths, ["/api/activities/5"])


if __name__ == "__main__":
    unittest.main()
