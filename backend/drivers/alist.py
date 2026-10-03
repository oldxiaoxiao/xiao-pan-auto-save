"""AList / OpenList 代理驱动骨架。

# TODO(driver): AList/OpenList 代理接入要点
# 不逆向任何网盘，而是对接自建 AList/OpenList 实例（alist.org），把其挂载的任意存储
# 统一成一个"网盘"。一个驱动即可覆盖 115/阿里/移动/联通 等几十种后端。
# 1. 配置：base_url（如 http://127.0.0.1:5244）+ 管理员 token（写在账号 Cookie 字段，
#    格式 `token=xxx`），实例需在 AList 中挂好对应网盘存储并开启"网盘目录"。
# 2. 分享：AList 本身不代理"别人发的分享链接"，两种方案：
#    a) 仅把本系统的 AList 驱动用于"目标目录"而非"分享源"（分享源仍需原生网盘驱动）；
#    b) 若必须从分享转存，用 AList 的 offline_download 或依赖具体 storage 的
#       `/api/fs/get` + sign URL 方案，或保留原生驱动做源、AList 做目标。
# 3. 核心接口：POST /api/fs/list（path + password）列目录、POST /api/fs/add 建目录、
#    POST /api/fs/rename、POST /api/fs/move、POST /api/fs/delete；
#    均返回 {code, message, data: {content:[{name, size, modified, is_dir}]}}。
# 4. FsItem 映射：fid 用 path（AList 以路径为键），mtime 用 modified。
# 5. 难度评估：★☆☆（REST 干净、文档全；限制是不支持分享源转存，需在 docs/drivers.md
#    说明其适用场景——作为"通用目标盘"或配合 AList 的 WebDAV 侧能力）。
"""

from .base import UnsupportedDrive


class AlistDriver(UnsupportedDrive):
    key = "alist"
    name = "AList/OpenList 代理"
    share_domains = []  # 不通过分享域名路由；由任务显式选择该驱动做目标盘
    supported = False
    notes = "REST /api/fs/{list,add,rename,move,delete}；适合做通用目标盘，分享源转存受限。"
