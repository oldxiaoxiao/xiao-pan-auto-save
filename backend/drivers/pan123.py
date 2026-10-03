"""123云盘驱动骨架。

# TODO(driver): 123云盘接入要点
# 1. 分享体系：123pan.com/s/<share_code>-<pwd>.html 或 mail.123pan.com 深链；
#    新版 API 走 123pan.com 前端 axios 接口：POST /api/v2/share/get/share（shareCode, sharePwd）
#    → ShareDetailData，分页 POST /api/v2/share/get/file（ParentID, Page, PageSize）。
# 2. 转存：POST /api/v2/share/save/copy（ParentID + SaveIdxList: [{Savename, ShareID, FileID…}]）
#    返回 TaskID，需轮询 /api/v2/user/task/list 确认完成。
# 3. 鉴权：Authorization 头为 JWT（登录后长期有效），部分接口需 sign 头（请求体签名，
#    算法曾变更多次，需抓包核对当前版本）。
# 4. 目录：自己盘文件列表 /api/v2/file/file-info-dircet，建目录 /api/v2/folder/create，
#    重命名 /api/v2/file/rename，删除进回收站 /api/v2/file/recycle-bin… 需二次清空。
# 5. 难度评估：★☆☆（接口 REST 化程度高、社区资料多，是第二个落地的良好候选）。
"""

from .base import UnsupportedDrive


class Pan123Driver(UnsupportedDrive):
    key = "pan123"
    name = "123云盘"
    share_domains = ["123pan.com", "123684.com", "123865.com", "123123666.com"]
    supported = False
    notes = "share/save/copy 转存 + task 轮询；JWT 鉴权 + 请求体 sign 头需抓包核对。"
