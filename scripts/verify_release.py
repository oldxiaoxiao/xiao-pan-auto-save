"""校验下载到的发布包是否被改动过。

用法（任选一种校验依据）：

    python scripts/verify_release.py 下载目录/            # 自动找同目录的 SHA256SUMS
    python scripts/verify_release.py xxx.zip --sums SHA256SUMS
    python scripts/verify_release.py xxx.zip --expected <64位十六进制>

每个 Release 都带 `SHA256SUMS`（由 `sha256sum *.zip` 生成，格式为 `<摘要>  <文件名>`）。
**校验不通过就不要解压安装**——这正是本脚本存在的唯一理由。

退出码：0 全部一致；1 存在不一致或缺失条目；2 参数/环境问题（没找到文件、没给校验依据）。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

CHUNK = 1024 * 1024
EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_USAGE = 2


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_sums(text: str) -> dict[str, str]:
    """解析 sha256sum 输出：'<摘要>  <文件名>'；二进制模式带 '*' 前缀也认。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        hexed, _, name = line.partition(" ")
        name = name.strip().lstrip("*").strip()
        hexed = hexed.strip().lower()
        if len(hexed) == 64 and name:
            out[Path(name).name] = hexed
    return out


def find_sums(target: Path) -> Path | None:
    """目标旁边（同目录，或目录本身）找 SHA256SUMS，不向上递归——下载目录就那一个地方。"""
    base = target if target.is_dir() else target.parent
    for cand in (base / "SHA256SUMS", base / "sha256sums.txt"):
        if cand.is_file():
            return cand
    return None


class UsageError(Exception):
    """用法或环境问题。main 统一转成退出码 2，测试里则表现为返回值而非抛出。"""


def collect_files(targets: list[Path]) -> list[Path]:
    files: list[Path] = []
    for t in targets:
        if t.is_dir():
            files.extend(sorted(p for p in t.glob("*.zip") if p.is_file()))
        elif t.is_file():
            files.append(t)
        else:
            raise UsageError(f"找不到：{t}")
    if not files:
        raise UsageError("没有可校验的文件（目录里没有 .zip）")
    return files


def verify(files: list[Path], expected: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """返回 (通过的文件名, 不一致的描述, 缺失的文件名)。"""
    ok: list[str] = []
    bad: list[str] = []
    missing: list[str] = []
    for path in files:
        want = expected.get(path.name)
        if not want:
            missing.append(path.name)
            print(f"[缺失] {path.name}：校验和文件里没有这一项")
            continue
        got = sha256_of(path)
        if got == want:
            ok.append(path.name)
            print(f"[一致] {path.name}\n       {got}")
        else:
            bad.append(path.name)
            print(f"[不一致] {path.name}\n       应为 {want}\n       实际 {got}")
    return ok, bad, missing


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_release.py",
        description="校验发布包 SHA256 与官方 SHA256SUMS 是否一致",
    )
    parser.add_argument("paths", nargs="+", type=Path, help="zip 文件，或装有 zip 与 SHA256SUMS 的目录")
    parser.add_argument("--sums", type=Path, default=None, help="SHA256SUMS 文件路径（默认在目标旁查找）")
    parser.add_argument("--expected", default=None, help="直接给出期望摘要（仅单个文件时可用）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        files = collect_files([p.expanduser() for p in args.paths])

        if args.expected:
            if len(files) != 1:
                raise UsageError("--expected 只在校验单个文件时可用")
            want = args.expected.strip().lower()
            if len(want) != 64:
                raise UsageError("--expected 需要 64 位十六进制摘要")
            expected = {files[0].name: want}
        else:
            sums_path = args.sums or find_sums(files[0])
            if not sums_path or not sums_path.is_file():
                raise UsageError("没找到 SHA256SUMS：把它和 zip 放在同一个目录，或用 --sums 指定")
            expected = parse_sums(sums_path.read_text(encoding="utf-8", errors="replace"))
            if not expected:
                raise UsageError(f"{sums_path.name} 里没有可解析的条目")
            print(f"依据 {sums_path}")
    except UsageError as exc:
        print(f"[用法] {exc}", file=sys.stderr)
        return EXIT_USAGE

    ok, bad, missing = verify(files, expected)
    print(f"\n合计 {len(ok)} 个一致，{len(bad)} 个不一致，{len(missing)} 个缺失")
    if bad or missing:
        print("**不要安装这些包**：与官方发布的摘要不符，可能下载不完整或被篡改。")
        return EXIT_MISMATCH
    print("全部一致，可以解压安装。")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
