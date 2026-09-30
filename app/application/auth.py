import hashlib
import hmac
import re
import secrets
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta, timezone
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import jwt
from jwt import InvalidTokenError
from pwdlib import PasswordHash
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.errors import AppError
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import AuthSession, User, utcnow
from app.infrastructure.persistence.repository import Repository

USERNAME = re.compile(r"^[a-z0-9_-]{3,32}$")
PASSWORD_HASH = PasswordHash.recommended()
DUMMY_HASH = PASSWORD_HASH.hash("not-a-real-user-password")


@dataclass(frozen=True)
class AuthContext:
    user_id: UUID
    username: str
    auth_session_id: UUID


class AuthService:
    def __init__(self, settings: Settings, database: Database):
        settings.require_auth_configuration()
        self.settings = settings
        self.database = database
        self.attempts: dict[str, deque[float]] = defaultdict(deque)
        self.attempt_lock = Lock()

    @staticmethod
    def normalize_username(value: str) -> str:
        username = value.strip().lower()
        if not USERNAME.fullmatch(username):
            raise AppError("INVALID_USERNAME", "用户名须为 3-32 位小写字母、数字、下划线或连字符", 422)
        return username

    @staticmethod
    def validate_password(value: str) -> None:
        if not 6 <= len(value) <= 30:
            raise AppError("INVALID_PASSWORD", "密码长度须为 6-30 个字符", 422)

    def _limit(self, namespace: str, value: str, limit: int = 10, window: int = 900, *, record: bool = False) -> None:
        key, now = f"{namespace}:{value}", monotonic()
        with self.attempt_lock:
            bucket = self.attempts[key]
            while bucket and bucket[0] <= now - window:
                bucket.popleft()
            if len(bucket) >= limit:
                raise AppError("AUTH_RATE_LIMITED", "登录尝试过多，请稍后再试", 429)
            if record:
                bucket.append(now)

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _refresh_token(self, session_id: UUID) -> str:
        return f"{session_id}.{secrets.token_urlsafe(32)}"

    def _access_token(self, user_id: UUID, session_id: UUID) -> tuple[str, int]:
        now = utcnow()
        seconds = self.settings.access_token_minutes * 60
        payload = {
            "sub": str(user_id), "sid": str(session_id), "jti": str(uuid4()),
            "token_use": "access", "iss": self.settings.jwt_issuer, "aud": self.settings.jwt_audience,
            "iat": now, "nbf": now, "exp": now + timedelta(seconds=seconds),
        }
        return jwt.encode(payload, self.settings.jwt_secret.get_secret_value(), algorithm="HS256"), seconds

    def _issue_session(self, user: User) -> tuple[str, str, int]:
        session_id = uuid4()
        refresh = self._refresh_token(session_id)
        with self.database.sessions() as database:
            database.add(AuthSession(
                id=session_id, user_id=user.id, refresh_token_hash=self._digest(refresh),
                absolute_expires_at=utcnow() + timedelta(days=self.settings.refresh_token_days),
            ))
            database.commit()
        access, seconds = self._access_token(user.id, session_id)
        return access, refresh, seconds

    def register(self, username: str, password: str):
        if not self.settings.registration_enabled:
            raise AppError("REGISTRATION_DISABLED", "当前未开放注册", 403)
        username = self.normalize_username(username)
        self.validate_password(password)
        with self.database.sessions() as database:
            user = User(username=username, password_hash=PASSWORD_HASH.hash(password))
            database.add(user)
            try:
                database.commit()
            except IntegrityError:
                database.rollback()
                raise AppError("USERNAME_TAKEN", "用户名已被使用", 409) from None
        return user, *self._issue_session(user)

    def login(self, username: str, password: str, client_ip: str):
        username = self.normalize_username(username)
        self._limit("login:user", username)
        self._limit("login:ip", client_ip or "unknown", limit=30)
        with self.database.sessions() as database:
            user = Repository(database).user_by_username(username)
            valid = PASSWORD_HASH.verify(password, user.password_hash if user else DUMMY_HASH)
            if user is None or not valid or not user.is_active:
                self._limit("login:user", username, record=True)
                self._limit("login:ip", client_ip or "unknown", limit=30, record=True)
                raise AppError("INVALID_CREDENTIALS", "用户名或密码错误", 401)
        return user, *self._issue_session(user)

    def authenticate(self, token: str) -> AuthContext:
        try:
            payload = jwt.decode(
                token, self.settings.jwt_secret.get_secret_value(), algorithms=["HS256"],
                issuer=self.settings.jwt_issuer, audience=self.settings.jwt_audience, leeway=30,
                options={"require": ["sub", "sid", "jti", "token_use", "iss", "aud", "iat", "nbf", "exp"]},
            )
            if payload["token_use"] != "access":
                raise ValueError
            user_id, session_id = UUID(payload["sub"]), UUID(payload["sid"])
            UUID(payload["jti"])
        except (InvalidTokenError, KeyError, TypeError, ValueError):
            raise AppError("UNAUTHORIZED", "认证信息无效或已过期", 401) from None
        with self.database.sessions() as database:
            session = Repository(database).active_auth_session(user_id, session_id)
            if session is None:
                raise AppError("UNAUTHORIZED", "认证信息无效或已过期", 401)
            user = database.get(User, user_id)
            return AuthContext(user.id, user.username, session.id)

    def refresh(self, presented: str):
        try:
            session_id = UUID(presented.split(".", 1)[0])
        except (ValueError, AttributeError):
            raise AppError("UNAUTHORIZED", "刷新凭据无效", 401) from None
        with self.database.sessions() as database:
            repository = Repository(database)
            session = repository.auth_session(session_id, lock=True)
            if session is None:
                raise AppError("UNAUTHORIZED", "刷新凭据无效或已过期", 401)
            expires_at = session.absolute_expires_at
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if session.revoked_at is not None or expires_at <= utcnow():
                raise AppError("UNAUTHORIZED", "刷新凭据无效或已过期", 401)
            if not hmac.compare_digest(session.refresh_token_hash, self._digest(presented)):
                session.revoked_at = utcnow()
                database.commit()
                raise AppError("REFRESH_REPLAYED", "登录会话已失效，请重新登录", 401)
            user = database.get(User, session.user_id)
            if user is None or not user.is_active:
                raise AppError("UNAUTHORIZED", "刷新凭据无效或已过期", 401)
            new_refresh = self._refresh_token(session.id)
            session.refresh_token_hash = self._digest(new_refresh)
            session.last_refreshed_at = utcnow()
            database.commit()
            access, seconds = self._access_token(user.id, session.id)
            return user, access, new_refresh, seconds

    def logout(self, context: AuthContext) -> None:
        with self.database.sessions() as database:
            session = Repository(database).auth_session(context.auth_session_id, lock=True)
            if session is not None and session.user_id == context.user_id:
                session.revoked_at = utcnow()
                database.commit()

    def logout_all(self, context: AuthContext) -> None:
        with self.database.sessions() as database:
            Repository(database).revoke_user_sessions(context.user_id)
            database.commit()

    def verify_cookie_origin(self, origin: str | None, sec_fetch_site: str | None, request_origin: str) -> None:
        allowed = {self.settings.frontend_origin.rstrip("/"), request_origin.rstrip("/")}
        if origin not in allowed or sec_fetch_site not in {None, "same-origin", "same-site"}:
            raise AppError("CSRF_REJECTED", "请求来源无效", 403)