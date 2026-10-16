#!/usr/bin/env python3
"""Independent DingTalk notifier. Preview by default; no LAZY imports."""
import argparse
import base64
import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_MESSAGE = "钉钉提醒测试：消息发送链路测试。"
TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
BOT_URL = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
WORK_BASE = "https://oapi.dingtalk.com/topapi/message/corpconversation/"
REQUIRED = {"bot": ("client_id", "client_secret", "robot_code", "user_id"),
            "work_notice": ("client_id", "client_secret", "agent_id", "user_id"),
            "webhook": ("webhook",)}
PRIVATE = ("client_secret", "user_id", "webhook", "secret", "access_token", "token")


class SafeError(Exception):
    """Messages are constructed locally; never include remote response bodies."""
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def redact(text, config):
    values = [str(config[k]) for k in PRIVATE if config.get(k)]
    if config.get("webhook"):
        values += [v for _, v in urllib.parse.parse_qsl(
            urllib.parse.urlsplit(str(config["webhook"])).query)]
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "[已隐藏]")
    return text


def validate(config):
    if not isinstance(config, dict):
        raise SafeError("配置必须是 JSON 对象。")
    mode = config.get("mode")
    if mode not in REQUIRED:
        raise SafeError("mode 必须是 bot、work_notice 或 webhook。")
    missing = [k for k in REQUIRED[mode] if not str(config.get(k, "")).strip()]
    if missing:
        raise SafeError("当前模式缺少字段：" + ", ".join(missing))
    for key in REQUIRED[mode]:
        if key != "agent_id" and not isinstance(config[key], str):
            raise SafeError("当前模式字段须为字符串：" + key)
    if mode == "work_notice":
        try:
            agent_id = int(config["agent_id"])
        except (TypeError, ValueError):
            raise SafeError("agent_id 必须是正整数。") from None
        if (isinstance(config["agent_id"], bool) or agent_id <= 0
                or str(config["agent_id"]) != str(agent_id)):
            raise SafeError("agent_id 必须是正整数。")
    if mode in ("bot", "work_notice") and "," in config["user_id"]:
        raise SafeError("本工具仅接受一个接收 user_id。")
    if mode == "webhook":
        parsed = urllib.parse.urlsplit(config["webhook"])
        if (parsed.scheme != "https" or parsed.netloc != "oapi.dingtalk.com"
                or parsed.path != "/robot/send" or parsed.fragment):
            raise SafeError("webhook 必须是官方 HTTPS oapi.dingtalk.com/robot/send 地址。")
        if not dict(urllib.parse.parse_qsl(parsed.query)).get("access_token"):
            raise SafeError("webhook 缺少 access_token 参数。")
        if "secret" in config and not isinstance(config["secret"], str):
            raise SafeError("secret 必须是字符串。")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_json(url, body, headers=None, timeout=10):
    request = urllib.request.Request(url, method="POST",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SafeError("HTTP 错误（状态 %d），未自动重试。" % exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SafeError("网络请求未获得确认，未自动重试。", uncertain=True) from None
    except (ValueError, UnicodeError):
        raise SafeError("响应不是有效 JSON，结果未确认。", uncertain=True) from None
    if not isinstance(result, dict):
        raise SafeError("响应结构无效，结果未确认。", uncertain=True)
    return result


def check_business(result, config, require_errcode=False):
    code = result.get("errcode", result.get("code"))
    if code is not None and str(code) not in ("0", ""):
        safe_code = redact(str(code), config)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", safe_code):
            safe_code = "[已隐藏]"
        raise SafeError("钉钉业务错误（代码 %s）。" % safe_code)
    if require_errcode and str(result.get("errcode")) != "0":
        raise SafeError("响应缺少成功状态，结果未确认。", uncertain=True)


def signed_webhook(url, secret, timestamp):
    if not secret:
        return url
    parsed = urllib.parse.urlsplit(url)
    params = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query)
              if k not in ("timestamp", "sign")]
    digest = hmac.new(secret.encode("utf-8"),
                      (str(timestamp) + "\n" + secret).encode("utf-8"), hashlib.sha256).digest()
    params.extend((("timestamp", str(timestamp)),
                   ("sign", base64.b64encode(digest).decode("ascii"))))
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(params)))


def ids(value):
    if isinstance(value, str):
        return {part.strip() for part in value.split(",") if part.strip()}
    if isinstance(value, list):
        return {str(part) for part in value if isinstance(part, (str, int))}
    return set()


def work_status(result, target):
    if not isinstance(result, dict):
        raise SafeError("结果查询结构无效。", uncertain=True)
    failed = set().union(*(ids(result.get(key)) for key in (
        "invalid_user_id_list", "forbidden_user_id_list", "failed_user_id_list")))
    forbidden_list = result.get("forbidden_list", [])
    if isinstance(forbidden_list, list):
        failed.update(str(item["userid"]) for item in forbidden_list
                      if isinstance(item, dict) and isinstance(item.get("userid"), (str, int)))
    read, unread = ids(result.get("read_user_id_list")), ids(result.get("unread_user_id_list"))
    successful = (read | unread) - failed
    if failed or ids(result.get("invalid_dept_id_list")):
        return {"status": "partial_failure" if successful else "failed", "sent": True,
                "delivery_confirmed": False, "failed_count": len(failed),
                "platform_sent_count": len(successful),
                "note": "平台报告部分或全部接收者发送失败，未自动重试。"}
    if target in successful:
        return {"status": "platform_confirmed", "sent": True,
                "delivery_confirmed": False, "read": target in read,
                "note": "平台结果确认发送；请在接收客户端核对提醒。"}
    return None


def send(config, message, post=post_json, clock=time.monotonic, sleep=time.sleep):
    validate(config)
    mode = config["mode"]
    attempted = False
    try:
        if mode == "webhook":
            url = signed_webhook(config["webhook"], config.get("secret", ""), int(time.time() * 1000))
            attempted = True
            result = post(url, {"msgtype": "text", "text": {"content": message}})
            check_business(result, config, require_errcode=True)
        else:
            auth = post(TOKEN_URL, {"appKey": config["client_id"], "appSecret": config["client_secret"]})
            token = auth.get("accessToken")
            safe_config = {**config, "access_token": token or ""}
            check_business(auth, safe_config)
            if not isinstance(token, str) or not token:
                raise SafeError("获取 accessToken 失败；检查应用凭证和权限。")
            if mode == "bot":
                attempted = True
                result = post(BOT_URL, {"robotCode": config["robot_code"],
                    "userIds": [config["user_id"]], "msgKey": "sampleText",
                    "msgParam": json.dumps({"content": message}, ensure_ascii=False)},
                    headers={"x-acs-dingtalk-access-token": token})
                check_business(result, safe_config)
                failures = ids(result.get("invalidStaffIdList")) | ids(result.get("flowControlledStaffIdList"))
                if failures:
                    return {"status": "failed", "sent": True, "delivery_confirmed": False,
                            "failed_count": len(failures), "note": "接收者无效或被限流，未自动重试。"}
                if not result.get("processQueryKey"):
                    raise SafeError("机器人响应缺少受理标识，结果未确认。", uncertain=True)
            else:
                query = "?" + urllib.parse.urlencode({"access_token": token})
                attempted = True
                result = post(WORK_BASE + "asyncsend_v2" + query, {
                    "agent_id": int(config["agent_id"]), "userid_list": config["user_id"],
                    "to_all_user": False, "msg": {"msgtype": "text", "text": {"content": message}}})
                check_business(result, safe_config, require_errcode=True)
                task_id = result.get("task_id")
                if not task_id:
                    raise SafeError("工作通知响应缺少 task_id，结果未确认。", uncertain=True)
                deadline = clock() + 15
                while clock() < deadline:
                    try:
                        response = post(WORK_BASE + "getsendresult" + query,
                            {"agent_id": int(config["agent_id"]), "task_id": task_id},
                            timeout=min(5, max(0.1, deadline - clock())))
                        check_business(response, safe_config, require_errcode=True)
                        outcome = work_status(response.get("send_result", {}), config["user_id"])
                    except SafeError as exc:
                        return {"status": "pending", "sent": True, "delivery_confirmed": False,
                                "note": "工作通知已受理，结果查询未确认。", "error": str(exc)}
                    if outcome:
                        return outcome
                    sleep(min(2, max(0, deadline - clock())))
                return {"status": "pending", "sent": True, "delivery_confirmed": False,
                        "note": "工作通知已受理，约 15 秒内未确认发送结果；请核对客户端，勿盲目重发。"}
        return {"status": "accepted", "sent": True, "delivery_confirmed": False,
                "note": "接口已受理；请在接收客户端确认收到，勿把受理当送达。"}
    except SafeError as exc:
        uncertain = attempted and exc.uncertain
        return {"status": "unknown" if uncertain else "failed", "sent": None if uncertain else False,
                "delivery_confirmed": False, "error": str(exc),
                "note": "未自动重试；发送请求发出后出现错误时，请先核对客户端。"}


def preview(config, message):
    return {"status": "preview", "sent": False, "mode": config["mode"],
            "recipient": "[已隐藏]", "message": redact(message, config), "note": "仅预览，未发送。"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="钉钉单次普通消息；默认仅预览。")
    parser.add_argument("--config")
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--smoke", action="store_true")
    actions.add_argument("--send", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.smoke:
            result = preview({"mode": "bot"}, args.message)
        else:
            if not args.config:
                raise SafeError("请提供 --config 配置文件，或用 --smoke 检查入口。")
            try:
                with open(args.config, encoding="utf-8") as source:
                    config = json.load(source)
            except (OSError, ValueError, UnicodeError):
                raise SafeError("无法读取配置文件，或配置不是有效 JSON。") from None
            validate(config)
            if not isinstance(args.message, str) or not args.message.strip():
                raise SafeError("消息不能为空。")
            result = send(config, args.message) if args.send else preview(config, args.message)
    except SafeError as exc:
        result = {"status": "failed", "sent": False, "error": str(exc)}
    except Exception:
        result = {"status": "unknown", "sent": None, "error": "程序未获得可确认结果；请先核对客户端，未自动重试。"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in ("preview", "platform_confirmed", "accepted") else 3 if result["status"] == "pending" else 1


if __name__ == "__main__":
    raise SystemExit(main())
