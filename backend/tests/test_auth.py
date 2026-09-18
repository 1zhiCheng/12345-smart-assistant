"""鉴权 Token 与身份边界测试。"""
from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from app.auth import AuthService
from app.config import Settings
from app.main import app
from app.storage.store import MemoryStore


def test_token_contains_expiry_and_verifies():
    settings = Settings(storage_mode="memory", auth_secret="test-secret", auth_token_ttl_hours=1)
    auth = AuthService(MemoryStore(), settings)
    token = auth.issue_token("operator")
    assert token.startswith("v1.")
    assert auth.verify_token(token) == "operator"
    assert auth.verify_token(token + "tampered") is None


def test_expired_token_is_rejected(monkeypatch):
    settings = Settings(storage_mode="memory", auth_secret="test-secret", auth_token_ttl_hours=1)
    auth = AuthService(MemoryStore(), settings)
    monkeypatch.setattr(time, "time", lambda: 1000)
    token = auth.issue_token("operator")
    monkeypatch.setattr(time, "time", lambda: 5000)
    assert auth.verify_token(token) is None


def test_register_operator_hashes_password_and_rejects_duplicate():
    async def scenario():
        store = MemoryStore()
        auth = AuthService(store, Settings(storage_mode="memory", auth_secret="test-secret"))
        user = await auth.register_operator("New_Operator", "password123", "测试营业员")
        assert user is not None
        assert user["id"] == "new_operator"
        assert user["role"] == "operator"
        stored = await store.get("users", "new_operator")
        assert stored is not None
        assert stored["password_hash"] != "password123"
        assert "password" not in stored
        assert await auth.authenticate("NEW_OPERATOR", "password123") is not None
        assert await auth.register_operator("new_operator", "another123", "重复账号") is None

    asyncio.run(scenario())


def test_register_validation_returns_actionable_chinese_error():
    """注册页不应再把输入格式问题笼统展示为 HTTP 422。"""
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/register",
            json={"username": "a!", "password": "123", "name": "张"},
        )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["message"] == "注册信息不符合要求"
    assert "账号至少需要 3 位" in detail["issues"]
    assert "密码至少需要 8 位" in detail["issues"]
    assert "姓名至少需要 2 位" in detail["issues"]


def test_bootstrap_admin_works_without_demo_accounts():
    async def scenario():
        store = MemoryStore()
        settings = Settings(
            storage_mode="memory", seed_demo_users=False,
            bootstrap_admin_username="competition_admin",
            bootstrap_admin_password="strong-password-123",
        )
        auth = AuthService(store, settings)
        await auth.seed_users()

        assert await store.get("users", "operator") is None
        user = await auth.authenticate("competition_admin", "strong-password-123")
        assert user is not None
        assert user["role"] == "system_admin"

    asyncio.run(scenario())
