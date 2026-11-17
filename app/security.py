"""基于 API Key 的简单角色鉴权（演示环境）。"""
from __future__ import annotations

from fastapi import Header, HTTPException

from .config import ADMIN_KEY, CONSUMER_KEY, OPERATOR_KEY

ROLES = {
    ADMIN_KEY: "admin",
    OPERATOR_KEY: "operator",
    CONSUMER_KEY: "consumer",
}


def require_role(*allowed: str):
    def dep(x_api_key: str = Header(default="")) -> str:
        role = ROLES.get(x_api_key)
        if role is None:
            raise HTTPException(status_code=401, detail="invalid API key")
        if role not in allowed:
            raise HTTPException(status_code=403, detail=f"role {role} not allowed")
        return role

    return dep
