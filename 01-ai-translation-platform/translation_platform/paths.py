"""不依赖进程当前工作目录的项目路径工具。"""

from __future__ import annotations

from os import PathLike
from pathlib import Path
from typing import Union


PathPart = Union[str, PathLike[str]]

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_project_path(first: PathPart, *additional: PathPart) -> Path:
    """从项目根目录解析相对路径。

    绝对路径保持不变，允许调用方显式选择项目外路径；相对路径始终以项目根目录为
    基准，不受当前工作目录影响。
    """

    candidate = Path(first).joinpath(*additional)
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate
