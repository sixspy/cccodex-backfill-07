import asyncio
import json
from pathlib import Path

import falcon.asgi
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import init_db
from app.services.errors import ServiceError
from app.services.resume import (
    get_resume_state,
    list_checkpoints,
    pause_window,
    resume_window,
)
from app.services.windows import (
    coverage_report,
    list_coverage,
    list_slices,
    list_windows,
    plan_slices,
    replay_slice,
)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-backfill-session")


def _json(resp, data, status=200):
    resp.status = str(status)
    resp.content_type = "application/json"
    resp.text = json.dumps(data, ensure_ascii=False)


def _error_body(exc: ServiceError) -> dict:
    body = {"ok": False, "error": exc.message}
    if exc.reason_code:
        body["reason_code"] = exc.reason_code
    if exc.details:
        body.update(exc.details)
    return body


async def _run(fn, *args, **kwargs):
    """把同步 SQLite 工作放线程池：并发请求才会真的抢写锁。"""
    return await asyncio.to_thread(fn, *args, **kwargs)


def _read_session(req) -> dict:
    raw = req.get_cookie_values("backfill_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw[0])
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(req):
    user = _read_session(req).get("user")
    if user != Config.ADMIN_USER:
        raise ServiceError("未登录", 401)
    return user


class StaticFile:
    def __init__(self, path: Path, content_type: str):
        self.path = path
        self.content_type = content_type

    async def on_get(self, req, resp):
        resp.content_type = self.content_type
        resp.data = self.path.read_bytes()


class LoginPage(StaticFile):
    pass


class SpaPage:
    async def on_get(self, req, resp):
        if _read_session(req).get("user") != Config.ADMIN_USER:
            raise falcon.HTTPFound("/login")
        resp.content_type = "text/html; charset=utf-8"
        resp.data = (STATIC / "index.html").read_bytes()


class Root:
    async def on_get(self, req, resp):
        if _read_session(req).get("user") == Config.ADMIN_USER:
            raise falcon.HTTPFound("/app/")
        raise falcon.HTTPFound("/login")


class ApiLogin:
    async def on_post(self, req, resp):
        body = await req.get_media() or {}
        user = str(body.get("username", "")).strip()
        pwd = str(body.get("password", ""))
        if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
            _json(resp, {"ok": False, "error": "账号或密码错误"}, 400)
            return
        _json(resp, {"ok": True, "user": user})
        resp.set_cookie(
            "backfill_session",
            serializer.dumps({"user": user}),
            path="/",
            http_only=True,
            secure=False,
        )


class ApiLogout:
    async def on_post(self, req, resp):
        _json(resp, {"ok": True})
        resp.unset_cookie("backfill_session", path="/")


class ApiMeta:
    async def on_get(self, req, resp):
        try:
            require_user(req)
            _json(resp, {"lab": Config.LAB_NAME, "product": "cc-backfill-lab"})
        except ServiceError as exc:
            _json(resp, {"error": exc.message}, exc.status_code)


class ApiWindows:
    async def on_get(self, req, resp):
        try:
            require_user(req)
            _json(resp, {"items": await _run(list_windows)})
        except ServiceError as exc:
            _json(resp, {"error": exc.message}, exc.status_code)


class ApiPlan:
    async def on_post(self, req, resp, window_id):
        try:
            require_user(req)
            _json(resp, await _run(plan_slices, int(window_id)))
        except ServiceError as exc:
            _json(resp, _error_body(exc), exc.status_code)


class ApiSlices:
    async def on_get(self, req, resp, window_id):
        try:
            require_user(req)
            _json(resp, {"items": await _run(list_slices, int(window_id))})
        except ServiceError as exc:
            _json(resp, _error_body(exc), exc.status_code)


class ApiReplay:
    async def on_post(self, req, resp, slice_id):
        try:
            require_user(req)
            _json(resp, {"slice": await _run(replay_slice, int(slice_id))})
        except ServiceError as exc:
            _json(resp, _error_body(exc), exc.status_code)


class ApiResume:
    async def on_get(self, req, resp, window_id):
        try:
            require_user(req)
            _json(resp, await _run(get_resume_state, int(window_id)))
        except ServiceError as exc:
            _json(resp, _error_body(exc), exc.status_code)


class ApiPause:
    async def on_post(self, req, resp, window_id):
        try:
            user = require_user(req)
            _json(resp, {"state": await _run(pause_window, int(window_id), actor=user)})
        except ServiceError as exc:
            _json(resp, _error_body(exc), exc.status_code)


class ApiResumeAction:
    async def on_post(self, req, resp, window_id):
        try:
            user = require_user(req)
            body = await req.get_media() or {}
            state = await _run(
                resume_window,
                int(window_id),
                version=_opt_int(body.get("version")),
                offset_no=_opt_int(body.get("offset_no")),
                next_slice_id=_opt_int(body.get("next_slice_id")),
                source=str(body.get("source") or "续灌"),
                actor=str(body.get("actor") or user),
            )
            _json(resp, {"state": state})
        except ServiceError as exc:
            _json(resp, _error_body(exc), exc.status_code)


class ApiCheckpoints:
    async def on_get(self, req, resp, window_id):
        try:
            require_user(req)
            _json(resp, {"items": await _run(list_checkpoints, int(window_id))})
        except ServiceError as exc:
            _json(resp, _error_body(exc), exc.status_code)


class ApiCoverage:
    async def on_get(self, req, resp):
        try:
            require_user(req)
            _json(resp, {"items": await _run(list_coverage)})
        except ServiceError as exc:
            _json(resp, {"error": exc.message}, exc.status_code)

    async def on_post(self, req, resp):
        try:
            require_user(req)
            body = await req.get_media() or {}
            wid = int(body.get("window_id"))
            _json(resp, {"report": await _run(coverage_report, wid)})
        except ServiceError as exc:
            _json(resp, _error_body(exc), exc.status_code)
        except (TypeError, ValueError):
            _json(resp, {"error": "window_id 无效"}, 400)


def _opt_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ServiceError("位点参数必须是整数", 400, reason_code="BAD_CHECKPOINT")


def create_app() -> falcon.asgi.App:
    init_db()
    app = falcon.asgi.App()
    app.add_route("/", Root())
    app.add_route("/login", LoginPage(STATIC / "login.html", "text/html; charset=utf-8"))
    app.add_route("/app/", SpaPage())
    app.add_route("/api/login", ApiLogin())
    app.add_route("/api/logout", ApiLogout())
    app.add_route("/api/meta", ApiMeta())
    app.add_route("/api/windows", ApiWindows())
    app.add_route("/api/windows/{window_id:int}/plan", ApiPlan())
    app.add_route("/api/windows/{window_id:int}/slices", ApiSlices())
    app.add_route("/api/windows/{window_id:int}/resume-state", ApiResume())
    app.add_route("/api/windows/{window_id:int}/pause", ApiPause())
    app.add_route("/api/windows/{window_id:int}/resume", ApiResumeAction())
    app.add_route("/api/windows/{window_id:int}/checkpoints", ApiCheckpoints())
    app.add_route("/api/slices/{slice_id:int}/replay", ApiReplay())
    app.add_route("/api/coverage-reports", ApiCoverage())
    app.add_static_route("/static", str(STATIC))
    return app


app = create_app()
