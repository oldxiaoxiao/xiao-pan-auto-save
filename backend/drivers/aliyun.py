"""阿里云盘（drive.aliyundrive.com / 开放平台）驱动骨架。

# TODO(driver): 阿里云盘接入要点
# 1. 分享体系：分享页 aliyundrive.com/s/<share_id>，需 POST /v2/share_link/get_by_anonymous? 不，
#    走 api.aliyundrive.com/v2/share_link/get_share_token（body: share_id, share_password）拿
#    x-share-token，再 /v2/file/list_by_share（share_id, parent_file_id, st 分页 marker）。
# 2. 转存：POST /v2/file/batch（async 任务，body: share_id, file_ids[], to_parent_file_id,
#    to_drive_id, auto_rename）需 access_token；新链路转存需 union 开放平台 or 客户端授权。
# 3. 鉴权现状：Web 端 refreshToken 已大面积失效、风控强（需 signatureUrl 机制），
#    推荐改走官方开放平台（OAuth2 + 申请开发者，union 接口带 upload/download 权限），
#    或走 AList/OpenList 代理（已有 alist.py 骨架）。
# 4. 目录 fid：根目录 "root"，drive_id 固定拿 /v2/user/get。重命名 /v2/file/update，删除 /v2/file/batch(废弃)。
# 5. 难度评估：★★★（token 体系与风控变动频繁，建议优先用 AList 代理落地）。
"""

from .base import UnsupportedDrive


class AliyunDriver(UnsupportedDrive):
    key = "aliyun"
    name = "阿里云盘"
    share_domains = ["aliyundrive.com", "alipan.com"]
    supported = False
    notes = "share_token + batch 转存；token 风控严，建议先走 AList/OpenList 代理。"
