"""百度网盘驱动骨架。

# TODO(driver): 百度网盘接入要点
# 1. 分享体系：pan.baidu.com/s/<pwd_id>，提取码走 http://pan.baidu.com/share/init?surl=…
#    先 GET /share/confirm?method=open 拿 cookies，再 /share/list?shareid=…&uk=… 列文件
#    （需要 fs_ids/fs_share_ids 数组字段），转存走 POST /share/transfer?async=2&fid=…，
#    之后轮询 /share/async?task_id=… 拿 result[].to。
# 2. 自己的盘：/rest/2.0/xpan/nas?method=uinfo 拿 uk；/rest/2.0/xpan/file?method=list 列目录、
#    listbysyncdir 不支持；创建目录 /rest/2.0/xpan/file?method=create（type=0）；
#    重命名/移动/删除走 /rest/2.0/xpan/multimedia?method=filemanager（operation=rename/move/delete，
#    参数需 fsid 串 + preventOverwrite/conflictControl）。
# 3. 关键鉴权：BDUSS cookie；所有请求需百度 UA + BDUSS 提取，且需处理 web 端限速/风控。
# 4. 难度评估：★★☆（分享转存流程相对成熟，接口版本混乱，需要维护 surl→ukw 转换与多次重定向）。
"""

from .base import UnsupportedDrive


class BaiduDriver(UnsupportedDrive):
    key = "baidu"
    name = "百度网盘"
    share_domains = ["pan.baidu.com", "eyun.baidu.com"]
    supported = False
    notes = "BDUSS 鉴权；share/transfer 异步转存 + async 轮询；filemanager 批量操作。"
