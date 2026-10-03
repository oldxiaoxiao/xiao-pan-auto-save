from backend.core.router import driver_class_for_share, make_driver
from backend.drivers import DRIVERS, driver_matrix, route_driver
from backend.drivers.base import CloudDrive, DriveError
from backend.drivers.quark import QuarkDriver


def test_registry_discovers_all_drivers():
    assert set(DRIVERS) >= {
        "quark",
        "baidu",
        "aliyun",
        "pan123",
        "pan115",
        "ctyun",
        "cmcc",
        "xunlei",
        "alist",
    }


def test_only_quark_supported():
    supported = {k for k, c in DRIVERS.items() if c.supported}
    assert supported == {"quark"}


def test_route_by_domain():
    assert route_driver("https://pan.quark.cn/s/abc123") is QuarkDriver
    assert route_driver("https://pan.quark.cn/s/abc?pwd=xx#/list/share/0" * 1) is QuarkDriver


def test_route_unknown_domain():
    assert route_driver("https://invalid.example.com/s/x") is None


def test_route_skeleton_domains():
    cls = route_driver("https://pan.baidu.com/s/xxx")
    assert cls is not None and cls.key == "baidu" and not cls.supported


def test_make_driver():
    drv = make_driver("https://pan.quark.cn/s/abc", cookie="__uid=1", index=0)
    assert isinstance(drv, QuarkDriver)
    assert drv.index == 1


def test_driver_class_for_share_rejects_unknown():
    try:
        driver_class_for_share("https://nope.example/s/1")
        raise AssertionError("should raise")
    except DriveError:
        pass


def test_matrix_shape():
    matrix = driver_matrix()
    quark = next(m for m in matrix if m["key"] == "quark")
    assert quark["supported"] and "rename" in quark["capability"]
    baidu = next(m for m in matrix if m["key"] == "baidu")
    assert not baidu["supported"]


def test_skeleton_raises_not_implemented():
    from backend.drivers.base import UnsupportedDrive

    cls = DRIVERS["baidu"]
    assert issubclass(cls, UnsupportedDrive)
    drv = CloudDrive.__new__(cls)  # type: ignore
    try:
        drv.parse_share("https://pan.baidu.com/s/x")
        raise AssertionError("should raise")
    except NotImplementedError as exc:
        assert "接入要点" in str(exc)
