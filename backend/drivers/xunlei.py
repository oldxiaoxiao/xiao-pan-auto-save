"""迅雷云盘驱动骨架。

# TODO(driver): 迅雷云盘接入要点
# 1. 分享体系：pan.xunlei.com/s/<pwd_id>，POST /api/x/1/share/token（pwd_id + passcode）
#    拿 stoken，GET /api/x/1/share/detail?pwd_id=&stoken=&pdir_fid= 分页列文件；
#    转存 POST /api/x/1/drive/saveas（fid_list + fid_token_list 与夸克体系相似——
#    都是阿里系风格协议），迅雷接口响应字段命名也高度接近夸克。
# 2. 鉴权：set-cookie 里的 session token + X-Replication 等自定义头；
#    需要迅雷会员才可转存大文件，账号健康检查要校验 VIP 状态。
# 3. 目录：/api/x/1/drive/files（parent_fid 分页）、files/rename、files/move、
#    folders（创建）、delete（进回收站）。
# 4. 签到：无官方签到。
# 5. 难度评估：★☆☆（协议结构最接近夸克驱动，可作为验证驱动抽象层的低成本第二个实现）。
"""

from .base import UnsupportedDrive


class XunleiDriver(UnsupportedDrive):
    key = "xunlei"
    name = "迅雷云盘"
    share_domains = ["pan.xunlei.com", "xunlei.com"]
    supported = False
    notes = "share/token + saveas（fid_token 体系同夸克）；需会员；X-Replication 头。"
