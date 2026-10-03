"""115网盘驱动骨架（官方开放平台方案）。

# TODO(driver): 115 接入要点
# 1. 推荐走官方开放平台（open.115.com，OAuth2 授权码 + code_id 拿 access/refresh_token），
#    Web Cookie（UID/CID/K）可用但风控强、易失效，仅作为降级路径。
# 2. 分享链接：115.com/s/<share_code>，分享列表需先 POST webapi.115.com/…/share 系列接口
#    （getuserinfo→datashare→mixed_file_share 拿 item_id），转存走 copy（to_pid + fid 列表）。
#    开放平台不提供分享接口 ⇒ 分享转存必须走 Web 逆向，这是主要难点。
# 3. 自己盘：开放平台 /cpu/rest/openapi/…（files.find、files.add_fold、files.rename、
#    files.move、files.delete→recyclebin），capacity/account 查容量，签到无官方接口（Web 端）。
# 4. 限速：开放平台官方限频严格（默认约 200 次/天/token 分级），任务批量轮询要精打细算。
# 5. 难度评估：★★★（分享逆向 + 开放平台双轨；签到类玩法官方明确禁止自动化，谨慎评估合规性）。
"""

from .base import UnsupportedDrive


class Pan115Driver(UnsupportedDrive):
    key = "pan115"
    name = "115网盘"
    share_domains = ["115.com", "115cdn.com", "lixian.vip"]
    supported = False
    notes = "官方开放平台 OAuth2（限频严）+ Web 分享逆向（无官方分享 API），双轨实现。"
