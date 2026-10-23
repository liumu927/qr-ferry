"""把 PyInstaller onedir 产物压成发布 zip，并校验版本号三处一致。

用法:
    python scripts/pack_windows_zip.py            # 版本取 pyproject.toml
    python scripts/pack_windows_zip.py v0.1.29    # CI 传入 tag，顺带校验

产物: dist/qr-ferry-v<ver>-windows-x64.zip，内顶层目录固定为 qr-ferry/。

版本号有三处来源，任一不一致都拒绝产出——否则会打出「文件名标 A、程序自报
B」的错标包：pyproject.toml、src/qrferry/__init__.py（exe 运行时窗口/托盘自报
的就是它）、CI 的 git tag。
"""
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "dist", "qr-ferry")


def _pyproject_version() -> str:
    import tomllib
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)["project"]["version"]


def _module_version() -> str:
    with open(os.path.join(ROOT, "src", "qrferry", "__init__.py"), encoding="utf-8") as f:
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.MULTILINE)
    if not m:
        raise SystemExit("无法从 src/qrferry/__init__.py 解析 __version__")
    return m.group(1)


def main(argv: list[str]) -> int:
    versions = {"pyproject.toml": _pyproject_version(), "qrferry/__init__.py": _module_version()}
    if len(argv) > 1:
        versions[f"tag {argv[1]}"] = argv[1].removeprefix("v")
    if len(set(versions.values())) != 1:
        print("版本号不一致，拒绝打包：", file=sys.stderr)
        for k, v in versions.items():
            print(f"  {k}: {v}", file=sys.stderr)
        return 1
    version = versions["pyproject.toml"]

    if not os.path.isdir(SRC):
        print(f"缺少构建产物: {SRC}（先跑 pyinstaller --noconfirm qr-ferry.spec）", file=sys.stderr)
        return 1

    out = os.path.join(ROOT, "dist", f"qr-ferry-v{version}-windows-x64.zip")
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for dirpath, _, files in os.walk(SRC):
            for name in sorted(files):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, SRC)
                z.write(full, os.path.join("qr-ferry", rel))
                n += 1
    print(f"OK {out}  entries={n}  size={os.path.getsize(out) / 1048576:.1f} MB  version={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
