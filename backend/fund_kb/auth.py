"""Cookie authentication, CSRF and optional standards-based OIDC code/PKCE login."""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select

from . import models as m
from .services import Result, aware, fail, now, uid


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate(request, db):
    token = request.cookies.get("kb_session")
    session = db.scalars(select(m.LoginSession).where(m.LoginSession.token_hash == token_hash(token))).first() if token else None
    if not session or session.revoked_at or aware(session.expires_at) <= now():
        fail(401, "AUTH_REQUIRED", "请先登录")
    user = db.get(m.User, session.user_id)
    if not user or not user.active:
        fail(401, "AUTH_REQUIRED", "会话已失效")
    request.state.login_session = session
    return user


def check_origin(request, settings):
    origin = request.headers.get("origin")
    allowed = set(getattr(settings, "allowed_origins", []) or [])
    allowed.add(str(request.base_url).rstrip("/"))
    if origin not in allowed or request.headers.get("sec-fetch-site") == "cross-site":
        fail(403, "ORIGIN_REJECTED", "请求来源不被允许")


def check_csrf(request, settings):
    check_origin(request, settings)
    supplied = request.headers.get("x-csrf-token", "")
    if not supplied or not secrets.compare_digest(supplied, request.state.login_session.csrf_token):
        fail(403, "CSRF_REJECTED", "CSRF令牌缺失或无效")


def set_session(request, db, user, response):
    previous = request.cookies.get("kb_session")
    if previous:
        old = db.scalars(select(m.LoginSession).where(m.LoginSession.token_hash == token_hash(previous))).first()
        if old:
            old.revoked_at = now()
    raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.add(m.LoginSession(id=uid(), user_id=user.id, token_hash=token_hash(raw), csrf_token=csrf,
        expires_at=now() + timedelta(hours=8), created_at=now(), last_seen_at=now()))
    response.set_cookie("kb_session", raw, max_age=8 * 3600, httponly=True,
        secure=request.app.state.settings.cookie_secure, samesite="lax", path="/")
    response.headers["Cache-Control"] = "private, no-store"
    return csrf


def me(ctx):
    from .libraries import visible_libraries
    spaces = visible_libraries(ctx.db, ctx.user)
    return Result({"id": ctx.user.id, "display_name": ctx.user.display_name,
        "csrf_token": ctx.request.state.login_session.csrf_token, "spaces": spaces})


def logout(ctx):
    ctx.request.state.login_session.revoked_at = now()
    response = JSONResponse(None, status_code=204)
    response.body = b""
    response.delete_cookie("kb_session", path="/")
    return response


def require_demo(settings):
    if settings.app_env != "development" or settings.auth_mode != "demo":
        fail(404, "NOT_FOUND", "接口不可用")


def demo_users(request, db):
    require_demo(request.app.state.settings)
    result = []
    for user in db.scalars(select(m.User).where(m.User.active.is_(True))):
        # Only controlled demo identities; never enumerate actual users through this route.
        if not user.external_subject.startswith("demo:"):
            continue
        rr = sorted(set(db.scalars(select(m.SpaceMember.role).where(m.SpaceMember.user_id == user.id))))
        result.append({"id": user.id, "display_name": user.display_name, "roles": rr})
    return result


def demo_login(request, db, data):
    require_demo(request.app.state.settings)
    check_origin(request, request.app.state.settings)
    if not isinstance(data, dict) or set(data) != {"user_id"}:
        fail(422, "INVALID_INPUT", "需要user_id")
    user = db.get(m.User, data["user_id"])
    if not user or not user.active or not user.external_subject.startswith("demo:"):
        fail(403, "DEMO_USER_REQUIRED", "只能使用预设演示身份")
    response = JSONResponse({})
    csrf = set_session(request, db, user, response)
    response.body = JSONResponse({"id": user.id, "display_name": user.display_name, "csrf_token": csrf}).body
    response.headers["content-length"] = str(len(response.body))
    return response


def oidc_config(settings):
    if not settings.oidc_issuer or not settings.oidc_client_id:
        fail(503, "OIDC_NOT_CONFIGURED", "OIDC尚未配置；没有调用身份服务")
    issuer = settings.oidc_issuer.rstrip("/")
    if not issuer.startswith("https://"):
        fail(503, "OIDC_INVALID_CONFIG", "OIDC issuer必须使用HTTPS")
    return issuer


async def oidc_login(ctx):
    settings = ctx.settings
    issuer = oidc_config(settings)
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            res = await client.get(issuer + "/.well-known/openid-configuration")
            res.raise_for_status()
            metadata = res.json()
    except (httpx.HTTPError, ValueError):
        fail(503, "OIDC_UNAVAILABLE", "身份服务暂时不可用")
    if metadata.get("issuer", "").rstrip("/") != issuer:
        fail(503, "OIDC_INVALID_CONFIG", "OIDC issuer不匹配")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not metadata.get(key, "").startswith("https://"):
            fail(503, "OIDC_INVALID_CONFIG", "OIDC端点无效")
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    callback = getattr(settings, "oidc_redirect_uri", None) or str(ctx.request.url_for("finishLogin"))
    pending = ctx.request.app.state.oidc_pending
    for key in list(pending):
        if pending[key]["expires_at"] <= now():
            pending.pop(key, None)
    if len(pending) >= 1000:
        fail(429, "LOGIN_RATE_LIMIT", "登录请求过多")
    browser_nonce = secrets.token_urlsafe(32)
    pending[state] = {"nonce": nonce, "verifier": verifier, "callback": callback,
        "metadata": metadata, "expires_at": now() + timedelta(minutes=5), "browser_nonce": browser_nonce}
    response = RedirectResponse(metadata["authorization_endpoint"] + "?" + urlencode({
        "client_id": settings.oidc_client_id, "redirect_uri": callback, "response_type": "code",
        "scope": "openid profile", "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256"}), status_code=302)
    response.set_cookie("kb_oidc_state", browser_nonce, max_age=300, httponly=True,
        secure=settings.cookie_secure, samesite="lax", path="/api/v1/auth")
    return response


async def oidc_callback(ctx):
    issuer = oidc_config(ctx.settings)
    from authlib.jose import JsonWebToken
    from authlib.jose.errors import JoseError
    from authlib.oidc.core import CodeIDToken

    state = ctx.query["state"]
    pending = ctx.request.app.state.oidc_pending.pop(state, None)
    if not pending or pending["expires_at"] <= now() or not secrets.compare_digest(
        ctx.request.cookies.get("kb_oidc_state", ""), pending["browser_nonce"]):
        fail(400, "OIDC_STATE_INVALID", "登录状态失效，请重新登录")
    metadata = pending["metadata"]
    client_secret = ctx.settings.oidc_client_secret
    if hasattr(client_secret, "get_secret_value"):
        client_secret = client_secret.get_secret_value()
    body = {"grant_type": "authorization_code", "code": ctx.query["code"],
        "redirect_uri": pending["callback"], "client_id": ctx.settings.oidc_client_id,
        "code_verifier": pending["verifier"]}
    auth = (ctx.settings.oidc_client_id, client_secret) if client_secret else None
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            res = await client.post(metadata["token_endpoint"], data=body, auth=auth)
            res.raise_for_status()
            tokens = res.json()
            keys = await client.get(metadata["jwks_uri"])
            keys.raise_for_status()
        claims = JsonWebToken(["RS256", "ES256", "PS256"]).decode(tokens["id_token"], keys.json(),
            claims_cls=CodeIDToken,
            claims_options={"iss": {"essential": True, "value": metadata["issuer"]},
                "aud": {"essential": True, "value": ctx.settings.oidc_client_id}},
            claims_params={"nonce": pending["nonce"], "client_id": ctx.settings.oidc_client_id,
                "access_token": tokens.get("access_token")})
        claims.validate(leeway=30)
        subject = f"{issuer}|{claims['sub']}"
    except (httpx.HTTPError, JoseError, ValueError, KeyError, TypeError):
        fail(401, "OIDC_LOGIN_FAILED", "身份验证未通过")
    user = ctx.db.scalars(select(m.User).where(m.User.external_subject == subject)).first()
    if user and not user.active:
        fail(403, "USER_DISABLED", "此身份已停用")
    if not user:
        user = m.User(id=uid(), external_subject=subject, display_name=str(claims.get("name", claims["sub"]))[:200], active=True)
        ctx.db.add(user)
        ctx.db.flush()
    response = RedirectResponse("/", status_code=302)
    set_session(ctx.request, ctx.db, user, response)
    response.delete_cookie("kb_oidc_state", path="/api/v1/auth")
    return response


HANDLERS = {"getMe": me, "logout": logout, "beginLogin": oidc_login, "finishLogin": oidc_callback}
