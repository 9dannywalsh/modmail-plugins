from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlparse

import aiohttp
import jinja2
from aiohttp import web
from aiohttp.web import Application, Request, Response, normalize_path_middleware
from aiohttp_session import get_session, setup
from aiohttp_session.cookie_storage import EncryptedCookieStorage
from cryptography import fernet
from discord.utils import MISSING
from jinja2 import Environment, FileSystemLoader

from core.models import getLogger

from .auth import authentication
from .handlers import AIOHTTPMethodHandler, aiohttp_error_handler
from .models import LogEntry, LogList

if TYPE_CHECKING:
    from jinja2 import Template  # noqa: F401

    from bot import ModmailBot

    from .types_ext import RawPayload


logger = getLogger(__name__)

# Set path for static
parent_dir = Path(__file__).parent.parent.resolve()
static_path = parent_dir / "static"

# Set path for templates
templates_path = parent_dir / "templates"
jinja_env = Environment(
    loader=FileSystemLoader(templates_path),
    enable_async=True,
)

OAUTH2_CLIENT_ID = os.getenv("OAUTH2_CLIENT_ID")
OAUTH2_CLIENT_SECRET = os.getenv("OAUTH2_CLIENT_SECRET")
OAUTH2_REDIRECT_URI = os.getenv("OAUTH2_REDIRECT_URI")
oauth_enabled = all((OAUTH2_CLIENT_ID, OAUTH2_CLIENT_SECRET, OAUTH2_REDIRECT_URI))


class Config:
    """
    Base class for storing configurations from `.env` (environment variables).
    """

    def __init__(self):
        self.log_url = os.getenv("LOG_URL", "https://example.com")
        self.log_prefix = os.getenv("LOG_URL_PREFIX", "/logs")
        self.host = os.getenv("HOST", "0.0.0.0")
        self.port = int(os.getenv("PORT", 8000))
        self.pagination = os.getenv("LOGVIEWER_PAGINATION", 25)

        self.using_oauth = oauth_enabled
        if self.using_oauth:
            logger.info(f"Enabling Logviewer Oauth: {self.using_oauth}")
            self.guild_id = os.getenv("GUILD_ID")
            self.bot_token = os.getenv("TOKEN")
            self.netloc = urlparse(OAUTH2_REDIRECT_URI).netloc
            self.bot_id = int(base64.b64decode(self.bot_token.split(".")[0] + "=="))


class LogviewerServer:
    """
    Main class to handle the log viewer server.
    """

    def __init__(self, bot: ModmailBot):
        self.bot: ModmailBot = bot
        self.config: Config = Config()

        self.app: Application = MISSING
        self.site: web.TCPSite = MISSING
        self.runner: web.AppRunner = MISSING
        self._hooked: bool = False
        self._running: bool = False

    def init_hook(self) -> None:
        """
        Initial setup to start the server.
        """
        self.app: Application = Application(
            middlewares=[
                normalize_path_middleware(remove_slash=True, append_slash=False),
            ]
        )
        self.app.router.add_static("/static", static_path)
        self.app["server"] = self

        self._add_routes()

        # middlewares
        self.app.middlewares.append(aiohttp_error_handler)

        self._hooked = True

        key = os.getenv("LOGVIEWER_SECRET", "A sophisticated key")[:24]
        key_b = key.encode("utf-8")
        key64 = base64.urlsafe_b64encode(key_b.ljust(32)[:32])
        f = fernet.Fernet(key64)

        setup(self.app, EncryptedCookieStorage(f))

    def _add_routes(self) -> None:
        prefix = self.config.log_prefix or "/logs"
        self.app.router.add_route("HEAD", "/", AIOHTTPMethodHandler)
        self.app.router.add_route("GET", "/login", AIOHTTPMethodHandler)
        self.app.router.add_route("GET", "/callback", AIOHTTPMethodHandler)
        self.app.router.add_route("GET", "/logout", AIOHTTPMethodHandler)

        if prefix == "/":
            for path in ("/", "/{key}", "/raw/{key}"):
                self.app.router.add_route("GET", path, AIOHTTPMethodHandler)
        else:
            for path in ("/", prefix, prefix + "/{key}", prefix + "/raw/{key}"):
                self.app.router.add_route("GET", path, AIOHTTPMethodHandler)

    async def start(self) -> None:
        """
        Starts the log viewer server.
        """
        if self._running:
            raise RuntimeError("Log viewer server is already running.")
        if not self._hooked:
            self.init_hook()
        logger.info("Starting log viewer server.")
        self.runner = web.AppRunner(self.app, handle_signals=True)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.config.host, self.config.port)
        await self.site.start()
        self._running = True

    async def stop(self) -> None:
        """
        Stops the log viewer server.
        """
        logger.info(" - Shutting down web server. - ")
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        self._running = False

    def is_running(self) -> bool:
        """Returns `True` if the server is currently running."""
        return self._running

    def info(self) -> str:
        """Returns modules used to run the web server."""
        main_deps = (
            f"Web application: aiohttp v{aiohttp.__version__}\n"
            f"Template renderer: jinja2 v{jinja2.__version__}\n"
        )

        return main_deps

    async def process_logs(
        self, request: Request, *, path: str, key: str, **kwargs
    ) -> Response:
        """
        Matches the request path with regex before rendering the logs template to user.
        """

        prefix = (
            "" if self.config.log_prefix == "/" else self.config.log_prefix or "/logs"
        )
        path_re = re.compile(rf"^{prefix}/(?:(?P<raw>raw)/)?(?P<key>([a-zA-Z]|[0-9])+)")
        match = path_re.match(path)
        if match is None:
            return await self.raise_error(
                "not_found", message=f"Invalid path, '{path}'."
            )
        data = match.groupdict()
        raw = data["raw"]
        if not raw:
            return await self.render_logs(request, key, **kwargs)
        else:
            return await self.render_raw_logs(request, key, **kwargs)

    @authentication
    async def render_logs(
        self,
        request: Request,
        key: str,
        **kwargs,
    ) -> Response:
        """Returns the html rendered log entry"""
        logs = self.bot.api.logs
        document: RawPayload = await logs.find_one({"key": key})
        if not document:
            return await self.raise_error(
                "not_found", message=f"Log entry '{key}' not found."
            )
        log_entry = LogEntry(document)
        return await self.render_template(
            "logbase", request, log_entry=log_entry, **kwargs
        )

    @authentication
    async def render_raw_logs(self, request, key, **kwargs) -> Any:
        """
        Returns the plain text rendered log entry.
        """
        logs = self.bot.api.logs
        document: RawPayload = await logs.find_one({"key": key})
        if not document:
            return await self.raise_error(
                "not_found", message=f"Log entry '{key}' not found."
            )

        log_entry = LogEntry(document)
        return Response(
            status=200,
            text=log_entry.plain_text(),
            content_type="text/plain",
            charset="utf-8",
        )

    @authentication
    async def render_loglist(self, request, **kwargs) -> Any:
        """
        Returns the html rendered log list
        """
        logs = self.bot.api.logs

        logs_per_page = int(self.config.pagination)

        try:
            page = int(request.query.get("page", 1))
        except ValueError:
            page = 1

        async def find_logs():
            filter_ = {"bot_id": str(self.bot.user.id)}

            count_all = await logs.count_documents(filter=filter_)

            status_open = request.query.get("open")

            if status_open == "false":
                filter_["open"] = False
            elif status_open == "true":
                filter_["open"] = True
            else:
                status_open = None

            if request.query.get("search"):
                search = request.query.get("search")
                filter_["$text"] = {"$search": search}

            projection_ = {
                "key": 1,
                "open": 1,
                "created_at": 1,
                "closed_at": 1,
                "recipient": 1,
                "creator": 1,
                "title": 1,
                "last_message": {"$arrayElemAt": ["$messages", -1]},
                "message_count": {"$size": "$messages"},
                "nsfw": 1,
            }

            cursor = logs.find(
                filter=filter_,
                projection=projection_,
                skip=(page - 1) * logs_per_page,
            ).sort("created_at", -1)

            count = await logs.count_documents(filter=filter_)

            max_page = count // logs_per_page
            if (count % logs_per_page) > 0:
                max_page += 1

            items = await cursor.to_list(length=logs_per_page)

            return items, max_page, status_open, count_all

        prefix = self.config.log_prefix

        document, max_page, status_open, count_all = await find_logs()

        log_list = LogList(document, prefix, page, max_page, status_open, count_all)

        return await self.render_template("loglist", request, data=log_list, **kwargs)

    @staticmethod
    async def raise_error(
        error_type: str, *, message: Optional[str] = None, **kwargs
    ) -> Any:
        exc_mapping = {
            "not_found": web.HTTPNotFound,
            "error": web.HTTPInternalServerError,
        }
        try:
            ret = exc_mapping[error_type]
        except KeyError:
            ret = web.HTTPInternalServerError
        if "status_code" in kwargs:
            status = kwargs.pop("status_code")
            kwargs["status"] = status

        if message is None:
            message = "No error message."
        raise ret(reason=message, **kwargs)

    async def render_template(
        self,
        name: str,
        request: Request,
        *args: Any,
        **kwargs: Any,
    ) -> Response:

        session = await get_session(request)
        kwargs["session"] = session
        kwargs["user"] = session.get("user")
        kwargs["app"] = request.app
        kwargs["config"] = self.config
        kwargs["using_oauth"] = oauth_enabled
        kwargs["logged_in"] = kwargs["user"] is not None
        kwargs["favicon"] = self.bot.user.display_avatar.replace(size=32, format="webp")

        template = jinja_env.get_template(name + ".html")
        template = await template.render_async(*args, **kwargs)
        response = Response(
            status=200,
            content_type="text/html",
            charset="utf-8",
        )
        response.text = template
        return response