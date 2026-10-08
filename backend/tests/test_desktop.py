"""桌面真实服务生命周期和持久路径，不需要 GUI。"""

from pathlib import Path

import httpx
import pytest


def test_desktop_data_paths_are_user_owned():
    from desktop.runtime import user_data_dir

    assert user_data_dir("darwin", {}, Path("/users/test")) == Path("/users/test/Library/Application Support/XiaoPan")
    assert user_data_dir("win32", {"LOCALAPPDATA": "C:/Users/test/AppData/Local"}, Path("C:/Users/test")) == Path("C:/Users/test/AppData/Local/XiaoPan")
    assert user_data_dir("linux", {}, Path("/users/test")) == Path("/users/test/.local/share/XiaoPan")


def test_desktop_custom_data_dir_is_absolute(tmp_path):
    from desktop.runtime import user_data_dir

    assert user_data_dir("darwin", {"XIAO_PAN_DESKTOP_DATA_DIR": str(tmp_path / "中文目录")}) == tmp_path / "中文目录"


def test_single_instance_lock_can_be_reacquired_after_exit(tmp_path):
    from desktop.runtime import InstanceLock

    with InstanceLock(tmp_path):
        with pytest.raises(RuntimeError, match="已经运行"):
            with InstanceLock(tmp_path):
                pass
    with InstanceLock(tmp_path):
        assert (tmp_path / "desktop.lock").is_file()


def test_desktop_server_starts_authenticated_and_stops(tmp_path):
    from desktop.runtime import BackendProcess

    data_dir = tmp_path / "中文数据"
    backend = BackendProcess(data_dir)
    try:
        backend.start()
        with httpx.Client(base_url=backend.url, trust_env=False) as client:
            assert client.get("/api/settings").status_code == 401
            response = client.get(backend.bootstrap_url, follow_redirects=True)
            assert response.status_code == 200
            drivers = client.get("/api/drivers").json()
            assert any(d["key"] == "quark" and d["supported"] for d in drivers)
            assert client.get("/api/auth/status").json() == {"required": False, "authenticated": True}
            assert client.get("/api/health").json()["data_dir"] == str(data_dir)
            assert client.get("/api/auth/desktop", params={"token": backend.token}).status_code == 401
        assert (data_dir / "xiao_pan.db").exists()
    finally:
        backend.stop()
    assert backend.process.poll() is not None
    with pytest.raises(httpx.ConnectError):
        httpx.get(backend.url + "/api/health", timeout=1, trust_env=False)


def test_default_download_dir_respects_data_dir():
    from backend.config import DATA_DIR
    from backend.services.download_service import DownloadSettings

    assert DownloadSettings.from_dict({}).dir == str(DATA_DIR / "downloads")


@pytest.mark.parametrize(("name", "expected"), [("CON.txt", "_CON.txt"), ("nul.mkv", "_nul.mkv"), ("AUX", "_AUX"), ("COM1.mp4", "_COM1.mp4"), ("LPT².txt", "_LPT².txt"), ("episode. ", "episode"), ("normal.mkv", "normal.mkv")])
def test_download_names_are_portable_on_windows(name, expected):
    from backend.services.download_service import safe_name

    assert safe_name(name) == expected
