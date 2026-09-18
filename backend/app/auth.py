"""鉴权：用户账号、口令哈希、无状态 HMAC Token。

- 用户角色：operator（营业员/热线受理员）/ department_admin（部门管理员）/
  system_admin（系统管理员）。
- Token 格式：`<user_id>.<hexdigest>`，HMAC-SHA256 签名，服务端无状态校验。
- 部门管理员：admin 账号可绑定 dept_id，未绑定则管理全部部门。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import Settings
from app.domain.wuhu import DEPARTMENTS
from app.storage.store import DataStore
from app.utils.logging import get_logger

logger = get_logger(__name__)

# 种子账号（演示用；生产应由管理员创建并妥善保管）
DEFAULT_USERS = [
    {"username": "operator", "password": "operator123", "name": "12345热线营业员", "role": "operator", "dept_id": ""},
    {"username": "admin", "password": "admin123", "name": "系统管理员", "role": "system_admin", "dept_id": ""},
] + [
    {
        "username": f"{str(dept['_id']).removeprefix('dept_')}_admin",
        "password": "admin123",
        "name": f"{dept['name']}部门管理员",
        "role": "department_admin",
        "dept_id": dept["_id"],
    }
    for dept in DEPARTMENTS
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuthService:
    """账号管理与 Token 签发/校验。"""

    def __init__(self, store: DataStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    # ---------- 口令 ----------
    @staticmethod
    def hash_password(password: str, salt: Optional[str] = None) -> str:
        salt = salt or uuid.uuid4().hex[:16]
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
        return f"{salt}${digest}"

    @staticmethod
    def verify_password(password: str, stored: str) -> bool:
        try:
            salt, digest = stored.split("$", 1)
        except ValueError:
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
        return hmac.compare_digest(calc, digest)

    # ---------- 用户 ----------
    async def seed_users(self) -> None:
        """按配置创建本地演示账号，并可创建一次性生产引导管理员。

        - `SEED_DEMO_USERS=false` 时只跳过固定口令演示账号。
        - 演示账号使用固定口令（admin123），仅用于本地演示，切勿用于生产。
        - `BOOTSTRAP_ADMIN_*` 可在无演示账号时创建首个系统管理员，口令至少 12 位。
        """
        if self.settings.seed_demo_users:
            for u in DEFAULT_USERS:
                if await self.store.get("users", u["username"]) is None:
                    await self.store.upsert_user(self._to_user(u))
                    logger.info("已创建种子账号: %s (%s)", u["username"], u["role"])
            # 同步将每个部门管理员写回部门表，保证“账号—部门—工单队列”三者可追溯。
            for u in DEFAULT_USERS:
                if u["role"] != "department_admin":
                    continue
                dept = await self.store.get("departments", u["dept_id"])
                if dept is None:
                    continue
                admins = list(dept.get("admin_users") or [])
                if u["username"] not in admins:
                    admins.append(u["username"])
                    dept["admin_users"] = admins
                    await self.store.upsert_department(dept)
            logger.warning("已创建比赛演示账号（operator/operator123、各部门 *_admin/admin123、admin/admin123）。生产环境请关闭演示账号。")
        else:
            logger.info("SEED_DEMO_USERS=false：未创建固定口令演示账号")

        username = self.settings.bootstrap_admin_username.strip().lower()
        password = self.settings.bootstrap_admin_password
        if not username and not password:
            return
        if not username or len(password) < 12:
            raise RuntimeError("生产引导管理员配置不完整：用户名不能为空且密码至少12位")
        if await self.store.get("users", username) is None:
            await self.store.upsert_user(self._to_user({
                "username": username,
                "password": password,
                "name": self.settings.bootstrap_admin_name.strip() or "系统管理员",
                "role": "system_admin",
                "dept_id": "",
            }))
            logger.info("已创建生产引导系统管理员: %s", username)

    @staticmethod
    def _to_user(u: dict[str, Any]) -> dict[str, Any]:
        return {
            "_id": u["username"],
            "username": u["username"],
            "name": u.get("name", u["username"]),
            "role": u.get("role", "operator"),
            "dept_id": u.get("dept_id", ""),
            "password_hash": AuthService.hash_password(u["password"]),
            "created_at": _now(),
        }

    async def authenticate(self, username: str, password: str) -> Optional[dict[str, Any]]:
        user = await self.store.get("users", username.strip().lower())
        if user is None or not self.verify_password(password, user.get("password_hash", "")):
            return None
        return self._public(user)

    async def register_operator(self, username: str, password: str, name: str) -> Optional[dict[str, Any]]:
        """注册营业员账号；公开入口不允许创建任何管理员角色。"""
        normalized = username.strip().lower()
        if await self.store.get("users", normalized) is not None:
            return None
        user = self._to_user({
            "username": normalized,
            "password": password,
            "name": name.strip(),
            "role": "operator",
            "dept_id": "",
        })
        await self.store.upsert_user(user)
        return self._public(user)

    async def get_user(self, user_id: str) -> Optional[dict[str, Any]]:
        user = await self.store.get("users", user_id)
        return self._public(user) if user else None

    async def list_users(self) -> list[dict[str, Any]]:
        return [self._public(u) for u in await self.store.find("users")]

    @staticmethod
    def _public(user: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": user.get("_id", ""),
            "username": user.get("username", ""),
            "name": user.get("name", ""),
            "role": user.get("role", "operator"),
            "dept_id": user.get("dept_id", ""),
        }

    # ---------- Token ----------
    @staticmethod
    def _b64encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    @staticmethod
    def _b64decode(data: str) -> bytes:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))

    def issue_token(self, user_id: str) -> str:
        """签发带 iat/exp 的紧凑 HMAC Token。

        不引入额外 JWT 依赖，但使用与 JWT 相同的 base64url payload +
        HMAC-SHA256 完整性保护。旧的永久 Token 会被拒绝。
        """
        now = int(time.time())
        payload = {
            "sub": user_id,
            "iat": now,
            "exp": now + int(self.settings.auth_token_ttl_hours * 3600),
        }
        encoded = self._b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        sig = hmac.new(self.settings.auth_secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
        return f"v1.{encoded}.{self._b64encode(sig)}"

    def verify_token(self, token: str) -> Optional[str]:
        try:
            version, encoded, signature = token.split(".", 2)
            if version != "v1":
                return None
            supplied = self._b64decode(signature)
            expected = hmac.new(
                self.settings.auth_secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
            ).digest()
            if not hmac.compare_digest(supplied, expected):
                return None
            payload = json.loads(self._b64decode(encoded))
            now = int(time.time())
            issued_at = int(payload["iat"])
            expires_at = int(payload["exp"])
            user_id = str(payload["sub"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
        # 容忍最多 60 秒时钟偏差，同时拒绝异常的倒置时间窗。
        if issued_at > now + 60 or expires_at <= now or expires_at <= issued_at:
            return None
        return user_id
