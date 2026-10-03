"""天翼云盘驱动骨架。

# TODO(driver): 天翼云盘接入要点
# 1. 分享体系：cloud.189.cn 分享链接（/f/xxxx 或提取码形式），先 GET 分享页拿
#    shareId，再 mkt.189.cn / api.cloud.189.cn 的 share 接口：
#    GetShareResourceByTime 分页列文件；转存 POST /os/api/…（BatchCopyFile，源传 shareItemId）。
# 2. 鉴权：渠道用户登录拿 userId + token（oauth2 系列），Cookie 内字段（yf_acct_info /
#    portal_token）时效短，长期运行需刷新逻辑；请求需带 Signature（HMAC，密钥在 JS 里）
#    + 加密的请求头，是接入天翼最大的坎。
# 3. 目录：ListDirectory / CreateFolder / RenameFile / Delete 均为 api.cloud.189.cn 的
#    REST 风格（?json=…），返回 XML 需兼容解析（部分接口 XML、部分 JSON）。
# 4. 签到：天翼有"金币签到"活动页（mkt.189.cn），非网盘官方能力，可选实现。
# 5. 难度评估：★★☆（社区已有可参考的开源实现，但 Signature 算法变更需跟进）。
"""

from .base import UnsupportedDrive


class CtyunDriver(UnsupportedDrive):
    key = "ctyun"
    name = "天翼云盘"
    share_domains = ["cloud.189.cn", "189.cn"]
    supported = False
    notes = "HMAC Signature 请求头 + token 刷新；BatchCopyFile 转存；XML/JSON 混合响应。"
