import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..state import ServerState, UserSession

router = APIRouter(prefix="/api/data", tags=["data"])


def _get_user(request: Request, token: str = Query(...)) -> UserSession:
    state: ServerState = request.app.state.server_state
    user = state.sessions.get(token)
    if not user:
        raise HTTPException(status_code=401, detail="Token 无效")
    user.last_access = time.time()
    return user


@router.get("/{task_id}")
async def get_data(task_id: str, user: UserSession = Depends(_get_user)):  # noqa: B008
    if task_id not in user.tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    data = user.caches.get(task_id)
    fetched_at = user.cache_fetched_at.get(task_id)
    last_error = user.cache_errors.get(task_id)
    task = user.tasks[task_id]
    stale = (fetched_at is None or last_error is not None or not task.enabled
             or time.time() - fetched_at > max(2 * task.interval, 60))
    metadata = {"fetched_at": fetched_at, "stale": stale, "last_error": last_error}
    if data is None:
        return {"status": "pending", "task_id": task_id, "data": None, **metadata}
    return {"status": "ok", "task_id": task_id, "data": data, **metadata}
