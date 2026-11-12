import secrets

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import Response


CSRF_COOKIE = "ddns_csrf"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
    ),
}


class SecurityMiddleware:
    def __init__(self, app, *, is_https, cookie_secure):
        self.app = app
        self.is_https = is_https
        self.cookie_secure = cookie_secure

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        token = request.cookies.get(CSRF_COOKIE)
        # 在模板渲染前准备 token，但只在 GET HTML 真正返回时设置 cookie，
        # 避免静态资源和失败 POST 意外建立新的表单凭据。
        request.state.csrf = token or secrets.token_urlsafe(32)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.update(SECURITY_HEADERS)
                if self.is_https(request):
                    headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
                is_html = headers.get("content-type", "").split(";", 1)[0].strip().lower() == "text/html"
                is_static = request.url.path.startswith("/static/")
                # 动态端点也可能返回认证重定向，统一禁止缓存以免保留会话状态。
                if not is_static:
                    headers["Cache-Control"] = "no-store"
                if request.method == "GET" and is_html and not is_static and not token:
                    cookie = Response()
                    cookie.set_cookie(CSRF_COOKIE, request.state.csrf, httponly=True,
                                      samesite="lax", secure=self.cookie_secure(), path="/")
                    headers.append("set-cookie", cookie.headers["set-cookie"])
            await send(message)

        await self.app(scope, receive, secure_send)
