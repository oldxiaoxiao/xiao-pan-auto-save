"""测试隔离：把 DATA_DIR 指向临时目录，避免 pytest 读写真实的 data/xiao_pan.db。

必须在导入任何 backend 模块之前设置。pytest 先于测试模块加载本 conftest.py，且本目录
无 __init__.py（不会连带导入 backend 包），故此处顶层赋值早于 backend.config 首次导入，
engine 会绑定到临时库。测试夹具里的 delete(Task)/delete(Account) 因此只作用于临时库。
"""

import atexit
import os
import shutil
import tempfile

_tmp = tempfile.mkdtemp(prefix="xiao-pan-tests-")
os.environ["DATA_DIR"] = _tmp
atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
