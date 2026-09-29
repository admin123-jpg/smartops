"""统一响应结构：{code, message, data}。"""
from __future__ import annotations

from typing import Any


def ok(data: Any = None, message: str = "success") -> dict:
    return {"code": 0, "message": message, "data": data}


def fail(message: str, code: int = 1, data: Any = None) -> dict:
    return {"code": code, "message": message, "data": data}


def page(items: list, total: int, page_no: int, page_size: int) -> dict:
    return {
        "code": 0,
        "message": "success",
        "data": {
            "items": items,
            "total": total,
            "page": page_no,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size if page_size else 0,
        },
    }
