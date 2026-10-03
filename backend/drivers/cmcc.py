"""中国移动云盘（和彩云/mCloud）驱动骨架。

# TODO(driver): 移动云盘接入要点
# 1. 分享体系：139.cn 短链 → GET share 页面解析 linkID + passcode，
#    POST /business-api/share/getShareFileList（或新版 /nrails/…）拿目录，
#    转存 POST /business-api/pan/personal/batch/copy（源 side=share，带 shareId/passwd）。
# 2. 鉴权：Authorization 头为 Bearer JWT（由登录手机号 + 验证码/RSA 加密获取，
#    另有 a-token 变体），并普遍附带 hmacSha256 签名头（x-Signature，appSecret 藏 JS）；
#    部分接口需 userId + RCOS 头组合，文档全靠抓包。
# 3. 目录：/business-api/pan/file/list、createFolder、rename、delete（回收站 recycle）
#    为较稳定的一族接口；容量/账号 /business-api/pan/user/getPersonalInfo。
# 4. 签到：无官方网盘签到接口，可省略 sign 能力。
# 5. 难度评估：★★☆（接口齐全但签名体系复杂，RSA 登录流程建议直接抄社区成熟实现思路）。
"""

from .base import UnsupportedDrive


class CmccDriver(UnsupportedDrive):
    key = "cmcc"
    name = "移动云盘"
    share_domains = ["139.com", "yun.139.cn", "caiyun.139.com"]
    supported = False
    notes = "linkID+passcode 分享解析；batch/copy 转存；JWT + x-Signature 签名头。"
