"""测试隔离：把 DATA_DIR 指向临时目录，避免 pytest 读写真实的 data/xiao_pan.db。

必须在导入任何 backend 模块之前设置。pytest 先于测试模块加载本 conftest.py，且本目录
无 __init__.py（不会连带导入 backend 包），故此处顶层赋值早于 backend.config 首次导入，
engine 会绑定到临时库。测试夹具里的 delete(Task)/delete(Account) 因此只作用于临时库。

XIAO_PAN_SKIP_STARTUP_PRUNE=1 同属隔离：lifespan 的启动清理会真删共享临时库里其它用例
seed 的过期终态行（执行顺序敏感的静默串扰）；每日/手动清理路径不受影响，仍可测。
"""

import atexit
import os
import shutil
import tempfile

import pytest

_tmp = tempfile.mkdtemp(prefix="xiao-pan-tests-")
os.environ["DATA_DIR"] = _tmp
os.environ["XIAO_PAN_SKIP_STARTUP_PRUNE"] = "1"
atexit.register(shutil.rmtree, _tmp, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _init_test_db():
    """建表一次：让不经过 TestClient 的测试(如 test_scheduler_log/test_task_service 单跑)也能访问临时库。"""
    from backend.database import init_db

    init_db()


@pytest.fixture(autouse=True)
def _no_quiet_hours():
    """默认关掉免打扰，让通知相关用例不受"跑测试的时刻"影响。

    FR-04 的免打扰默认 23:00–08:00，若沿用默认，夜里跑测试时所有通知都会改成入队，
    一堆既有用例会随挂钟时间翻脸。专门测免打扰的用例自己显式传 settings 覆盖。
    """
    from backend.api.deps import set_setting

    set_setting("notify_quiet", {"enabled": False, "start": "23:00", "end": "08:00"})
    yield
