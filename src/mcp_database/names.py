"""Chuẩn hóa tên bảng/cột: bỏ nháy, bỏ schema, viết hoa."""


def norm(name: str) -> str:
    cleaned = name.replace('"', "").replace("`", "").strip()
    return cleaned.rsplit(".", 1)[-1].strip().upper()
