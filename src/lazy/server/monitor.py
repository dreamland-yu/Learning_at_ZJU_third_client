import asyncio
import logging
import time

import httpx

from ..core.load_config import load_config
from .session_manager import create_user_client
from .state import MonitorTask, ServerState, UserSession

logger = logging.getLogger(__name__)


def resolve_api_config(api_config_path: str) -> dict:
    category, api_name = api_config_path.split(".", 1)
    config = load_config.apiListConfig().load_config().get(category, {})
    return config.get("apis_config", {}).get(api_name, {})


class UpstreamError(Exception):
    """Only fixed categories cross the API/log boundary; no remote error text."""


def _response_data(response, as_json):
    if response.status_code in (401, 403) or response.url.host != "courses.zju.edu.cn":
        raise UpstreamError("auth_expired")
    if response.status_code >= 400:
        raise UpstreamError("http_error")
    if response.is_redirect:
        raise UpstreamError("auth_expired")
    if not as_json:
        return response.text
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        text = response.text.lower()
        if "password" in text and ("<form" in text or "<input" in text):
            raise UpstreamError("auth_expired") from None
        raise UpstreamError("invalid_json") from None
    if not isinstance(data, (dict, list)):
        raise UpstreamError("invalid_payload")
    if isinstance(data, dict):
        indicators = (data.get("code"), data.get("error_code"), data.get("status"), data.get("status_code"))
        if any(str(value).lower() in ("401", "403", "unauthorized", "unauthenticated", "login_required")
               for value in indicators):
            raise UpstreamError("auth_expired")
        message_only = bool(data.get("message") or data.get("detail")) and not (
            "id" in data or any(isinstance(value, (list, dict)) for value in data.values()))
        if (data.get("error") or data.get("errors") or data.get("success") is False or message_only
                or any(data.get(key) not in (None, 0, "0", "", 200, "200")
                       for key in ("code", "errcode", "error_code"))
                or str(data.get("status")).lower() in ("error", "failed", "failure")):
            raise UpstreamError("business_error")
    return data


async def _refresh_session(state, user, generation):
    async with user.refresh_lock:
        if user.session_generation != generation:
            return  # Another task already refreshed this expired session.
        now = time.monotonic()
        if now < user.next_auth_refresh:
            raise UpstreamError("auth_retry_backoff")
        user.next_auth_refresh = now + 60
        candidate = None
        try:
            credentials = state.credential_store.get(user.studentid)
            if not credentials or not credentials.get("password"):
                raise UpstreamError("credentials_unavailable")
            candidate = await create_user_client(cookies=None, trust_env=state.trust_env)
            if not await candidate.login(user.studentid, credentials["password"]):
                raise UpstreamError("auth_refresh_failed")
            state.credential_store.update_cookies(user.studentid, dict(candidate.session.cookies))
            old_session = user.zju_client
            user.zju_client = candidate.session
            user.session_generation += 1
            user.laz_studentid = None
            candidate = None  # The session is now owned by UserSession.
            await old_session.aclose()
        except UpstreamError:
            raise
        except Exception:
            raise UpstreamError("auth_refresh_failed") from None
        finally:
            if candidate is not None and candidate.session is not None:
                await candidate.session.aclose()


async def request_upstream(state, user, url, params=None, as_json=True):
    """Read only; retry once after an expired session is successfully replaced."""
    generation = user.session_generation
    for attempt in range(2):
        try:
            response = await user.zju_client.get(url, params=params or {}, follow_redirects=True)
            return _response_data(response, as_json)
        except UpstreamError as error:
            if str(error) != "auth_expired" or attempt:
                raise
            await _refresh_session(state, user, generation)
        except httpx.RequestError:
            raise UpstreamError("network_error") from None
        except Exception:
            raise UpstreamError("request_error") from None


async def poll_user_task_once(state, user, task):
    try:
        api_config = resolve_api_config(task.api_config_path)
        url = api_config.get("url")
        if not url:
            raise UpstreamError("configuration_error")
        raw_data = await request_upstream(state, user, url, api_config.get("params", {}))
        user.caches[task.task_id] = raw_data
        user.cache_fetched_at[task.task_id] = time.time()
        user.cache_errors[task.task_id] = None
        items = _extract_items(raw_data, task.id_field)
        ids = {item[task.id_field] for item in items
               if isinstance(item, dict) and isinstance(item.get(task.id_field), (str, int))}
        new_ids = ids - user.seen_ids.get(task.task_id, set())
        user.seen_ids[task.task_id] = ids
        if new_ids:
            logger.info("%s: 发现 %s 个新项目", task.task_id, len(new_ids))
        return True
    except Exception as error:
        category = str(error) if isinstance(error, UpstreamError) else "request_error"
        user.cache_errors[task.task_id] = category
        logger.warning("监控任务 %s 失败: %s", task.task_id, category)
        return False


async def run_user_task(state: ServerState, user: UserSession, task: MonitorTask):
    logger.info("启动监控任务 %s | 间隔 %ss", task.task_id, task.interval)
    while task.enabled:
        await poll_user_task_once(state, user, task)
        await asyncio.sleep(task.interval)


def _extract_items(data, id_field: str) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("rollcalls", "todos", "items", "data", "list", "uploads", "activities"):
            if key in data and isinstance(data[key], list):
                return data[key]
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


async def start_monitor_for_user(state: ServerState, user: UserSession):
    if user.task_coros:
        for coro in user.task_coros.values():
            coro.cancel()
        user.task_coros.clear()

    for task in user.tasks.values():
        if not task.enabled:
            continue
        coro = asyncio.create_task(run_user_task(state, user, task))
        user.task_coros[task.task_id] = coro
        await asyncio.sleep(0)


async def stop_monitor_for_user(user: UserSession):
    for coro in user.task_coros.values():
        coro.cancel()
    user.task_coros.clear()


def merge_tasks(system_tasks: list[MonitorTask], overrides: dict[str, dict]) -> dict[str, MonitorTask]:
    result: dict[str, MonitorTask] = {}
    for t in system_tasks:
        override = overrides.get(t.task_id, {})
        merged = MonitorTask(
            task_id=t.task_id,
            api_config_path=t.api_config_path,
            interval=override.get("interval", t.interval),
            action=override.get("action", t.action),
            id_field=override.get("id_field", t.id_field),
            enabled=override.get("enabled", t.enabled),
            is_system=t.is_system,
            description=override.get("description", t.description),
        )
        result[t.task_id] = merged
    return result
