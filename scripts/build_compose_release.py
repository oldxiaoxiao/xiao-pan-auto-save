"""生成无需源码的 Compose 发行附件，保留 ${...} 配置表达式。"""

import sys
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    from backend import __version__

    config = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    config["services"]["xiao-pan"].pop("build", None)
    release = ROOT / "release"
    release.mkdir(exist_ok=True)
    with zipfile.ZipFile(release / f"xiao-pan-auto-save-{__version__}-compose.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("docker-compose.yml", yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
        archive.writestr("README.md", f"# 小盘自动转存 {__version__} · Compose\n\n复制 .env.example 为 .env，按需编辑，执行 docker compose pull 和 docker compose up -d。\n\n默认访问 http://127.0.0.1:8432。局域网访问需设置密码与 WEBUI_BIND。\n\n[部署指南](docs/deployment.md) · [配置参考](docs/configuration.md) · [功能手册](docs/features.md) · [升级日志](docs/releases.md)\n")
        for source, target in [(".env.example", ".env.example"), ("LICENSE", "LICENSE")]:
            archive.write(ROOT / source, target)
        for path in (ROOT / "docs").glob("*.md"):
            archive.write(path, path.relative_to(ROOT))


if __name__ == "__main__":
    main()
