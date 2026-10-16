"""Read-only confirmation of ordinary homework submission state."""
import time

from fastapi import APIRouter, Depends, Path, Request
from lxml import etree

from ..monitor import UpstreamError, request_upstream, resolve_api_config
from ..state import UserSession
from .data import _get_user

router = APIRouter(prefix="/api/assignments", tags=["assignments"])


async def _student_id(state, user):
    if user.laz_studentid:
        return user.laz_studentid
    document = await request_upstream(state, user, "https://courses.zju.edu.cn/user/index", as_json=False)
    try:
        html = etree.HTML(document)
        values = html.xpath('//span[@id="userId"]/@value') if html is not None else []
    except (ValueError, etree.Error):
        values = []
    if len(values) != 1 or not str(values[0]).isdigit():
        raise UpstreamError("student_id_unavailable")
    user.laz_studentid = str(values[0])
    return user.laz_studentid


@router.get("/submission/{activity_id}")
async def get_submission(request: Request, activity_id: int = Path(..., gt=0),
                         user: UserSession = Depends(_get_user)):
    state = request.app.state.server_state
    result = {"status": "unknown", "activity_id": activity_id, "submitted": None,
              "title": None, "end_time": None, "type": None,
              "fetched_at": None, "last_error": None}
    try:
        config = resolve_api_config("assignment.activity")
        url = config.get("url", "").replace("<placeholder>", str(activity_id))
        if not url:
            raise UpstreamError("configuration_error")
        activity = await request_upstream(state, user, url, config.get("params", {}))
        if not isinstance(activity, dict) or str(activity.get("id")) != str(activity_id):
            raise UpstreamError("invalid_activity")
        result.update({key: activity.get(key) for key in ("title", "end_time", "type")})
        if activity.get("type") != "homework":
            raise UpstreamError("unsupported_type")
        count = activity.get("user_submit_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise UpstreamError("submission_state_unknown")
        student_id = await _student_id(state, user)
        config = resolve_api_config("assignment.submission_list")
        url = config.get("url", "").replace("<placeholder1>", str(activity_id)).replace("<placeholder2>", student_id)
        if not url:
            raise UpstreamError("configuration_error")
        submission = await request_upstream(state, user, url, config.get("params", {}))
        records = submission.get("list") if isinstance(submission, dict) else None
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise UpstreamError("invalid_submission_list")
        if bool(count) != bool(records):
            raise UpstreamError("submission_state_conflict")
        result.update(status="ok", submitted=count > 0, fetched_at=time.time())
    except Exception as error:
        result["last_error"] = str(error) if isinstance(error, UpstreamError) else "request_error"
    return result
