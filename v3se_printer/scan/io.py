from __future__ import annotations

import errno
import os
import shutil
from typing import Any


def is_no_space_error(exc: BaseException) -> bool:
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC:
        return True
    msg = str(exc).lower()
    return "no space left on device" in msg or "enospc" in msg


def imwrite(cv2: Any, path: str, img: Any, params: list[int] | None = None) -> None:
    if params:
        ok = cv2.imwrite(path, img, params)
    else:
        ok = cv2.imwrite(path, img)
    if ok is True:
        return
    try:
        free = int(shutil.disk_usage(os.path.dirname(path) or ".").free)
        if free < (64 * 1024 * 1024):
            raise OSError(errno.ENOSPC, "No space left on device", path)
    except OSError:
        raise
    except Exception:
        pass
    raise RuntimeError(f"cv2.imwrite returned False: {os.path.basename(path)}")
