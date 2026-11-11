# -*- coding: utf-8 -*-
"""fnos-resmon fpk 一键构建脚本

流程：
  1. 组装 build/app  <- src/（应用源码与内置 wheel）
  2. 准备 build/vendor <- pip install -r requirements.txt（Flask 全套依赖，仅构建期需要）
  3. 打包 dist/app.tgz（排除 __pycache__ / *.pyc）
  4. 渲染 packaging/manifest 并组装 build/fpkroot
  5. 打包 dist/fnos-resmon_<version>_x86_64.fpk
  6. 自校验：manifest.checksum 与 app.tgz 实际 md5 一致、app.tgz 内关键文件齐全

用法：
  python build/build_fpk.py            # 常规构建（沿用已有 build/vendor）
  python build/build_fpk.py --fetch    # 强制重新拉取 Python 依赖
  python build/build_fpk.py --no-vendor # 仅打源码包（不含 Flask 依赖，需 NAS 侧自备）
"""
import os, sys, shutil, hashlib, tarfile, subprocess, io

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
PKG = os.path.join(ROOT, "packaging")
ASSETS = os.path.join(ROOT, "assets")
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")
STAGE = os.path.join(BUILD, "app")
VENDOR = os.path.join(BUILD, "vendor")
FPKROOT = os.path.join(BUILD, "fpkroot")

APPNAME = "fnos-resmon"
VERSION = "1.3.22"


def log(*a):
    print("[build]", *a)


def skip(name):
    return name == "__pycache__" or name.endswith(".pyc")


def copy_tree(src, dst, collected, prefix=""):
    """复制目录并记录写入的相对路径（不打表就不进包，避免陈旧文件残留）"""
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if not skip(d))
        for f in sorted(files):
            if skip(f):
                continue
            full = os.path.join(root, f)
            rel = os.path.join(prefix, os.path.relpath(full, src)).replace(os.sep, "/")
            out = os.path.join(dst, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            shutil.copy2(full, out)
            collected.append(rel)


def fetch_vendor():
    """拉取目标平台（NAS: linux x86_64 / CPython 3.11）的依赖。

    在 Windows/macOS 上构建时必须显式指定 --platform，否则会拉到本机平台的
    二进制 wheel，拷到 NAS 上无法加载。
    """
    req = os.path.join(ROOT, "requirements.txt")
    log("拉取 Python 依赖（manylinux2014_x86_64 / cp311）->", VENDOR)
    common = [sys.executable, "-m", "pip", "install", "--no-compile", "--target", VENDOR]
    pinned = ["--only-binary=:all:", "--platform", "manylinux2014_x86_64",
              "--python-version", "3.11", "--implementation", "cp", "--abi", "cp311",
              "-r", req]
    r = subprocess.run(common + pinned)
    if r.returncode != 0:
        log("带平台参数拉取失败，退回本机平台安装（仅适用于在 NAS 本机构建）")
        r = subprocess.run(common + ["-r", req])
    if r.returncode != 0:
        log("依赖拉取失败，可手动执行：pip install --target build/vendor -r requirements.txt")
        sys.exit(1)


def main():
    argv = sys.argv[1:]
    do_fetch = "--fetch" in argv
    use_vendor = "--no-vendor" not in argv

    os.makedirs(DIST, exist_ok=True)
    if do_fetch and os.path.isdir(VENDOR):
        # 仅清空内容，避免把旧版本依赖打进包
        for name in os.listdir(VENDOR):
            shutil.rmtree(os.path.join(VENDOR, name), ignore_errors=True)
    if use_vendor and (do_fetch or not os.path.isdir(VENDOR)):
        fetch_vendor()

    # ---- 1) 组装 build/app ----
    entries = []
    copy_tree(SRC, STAGE, entries)
    if use_vendor:
        copy_tree(VENDOR, STAGE, entries, prefix="py/vendor")
    log("源码文件数:", len([e for e in entries if not e.startswith("py/vendor/")]),
        "| vendor 文件数:", len([e for e in entries if e.startswith("py/vendor/")]))

    # ---- 2) app.tgz ----
    apptgz = os.path.join(DIST, "app.tgz")
    entries.sort(key=lambda p: (p.count("/"), p))
    with tarfile.open(apptgz, "w:gz") as t:
        for rel in entries:
            full = os.path.join(STAGE, rel.replace("/", os.sep))
            ti = t.gettarinfo(full, arcname=rel)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mode = 0o755 if (rel.endswith(".py") or os.access(full, os.X_OK)) else 0o644
            with open(full, "rb") as fh:
                t.addfile(ti, fh)
    md5 = hashlib.md5(open(apptgz, "rb").read()).hexdigest()
    log("app.tgz:", round(os.path.getsize(apptgz) / 1024, 1), "KB  md5:", md5)

    # ---- 3) fpkroot ----
    os.makedirs(FPKROOT, exist_ok=True)
    for name in ("ICON.PNG", "ICON_256.PNG"):
        shutil.copy2(os.path.join(ASSETS, name), os.path.join(FPKROOT, name))
    shutil.copy2(apptgz, os.path.join(FPKROOT, "app.tgz"))
    shutil.copy2(os.path.join(PKG, "LICENSE"), os.path.join(FPKROOT, "LICENSE"))
    for d in ("cmd", "config"):
        os.makedirs(os.path.join(FPKROOT, d), exist_ok=True)
        for name in sorted(os.listdir(os.path.join(PKG, d))):
            dst = os.path.join(FPKROOT, d, name)
            shutil.copy2(os.path.join(PKG, d, name), dst)
            os.chmod(dst, 0o755 if d == "cmd" else 0o644)

    tpl = open(os.path.join(PKG, "manifest.tpl"), encoding="utf-8").read()
    manifest = tpl.replace("{{CHECKSUM}}", md5).replace("{{VERSION}}", VERSION)
    with open(os.path.join(FPKROOT, "manifest"), "w", encoding="utf-8", newline="\n") as f:
        f.write(manifest)

    # ---- 4) fpk ----
    fpk = os.path.join(DIST, "%s_%s_x86_64.fpk" % (APPNAME, VERSION))
    order = ["LICENSE", "ICON.PNG", "ICON_256.PNG", "app.tgz", "manifest"]
    with tarfile.open(fpk, "w:gz") as t:
        for name in order:
            t.add(os.path.join(FPKROOT, name), arcname=name)
        for d in ("cmd", "config"):
            for name in sorted(os.listdir(os.path.join(FPKROOT, d))):
                full = os.path.join(FPKROOT, d, name)
                ti = t.gettarinfo(full, arcname="%s/%s" % (d, name))
                ti.mode = 0o755 if d == "cmd" else 0o644
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = "root"
                with open(full, "rb") as fh:
                    t.addfile(ti, fh)
    log("FPK:", fpk)
    log("size:", round(os.path.getsize(fpk) / 1024, 1), "KB")

    # ---- 5) 自校验 ----
    with tarfile.open(fpk, "r:gz") as t:
        names = t.getnames()
        inner = t.extractfile("app.tgz").read()
        mani = t.extractfile("manifest").read().decode("utf-8")
    ok_md5 = hashlib.md5(inner).hexdigest() == md5
    ok_sum = ("checksum = " + md5) in mani
    inner_names = tarfile.open(fileobj=io.BytesIO(inner), mode="r:gz").getnames()
    need = ["app.py", "gateway_bridge.py", "public/index.html", "ui/config"]
    if use_vendor:
        need += ["py/vendor/flask/__init__.py"]
    missing = [n for n in need if n not in inner_names]
    if use_vendor and not [n for n in inner_names if n.startswith("py/wheels/psutil-")]:
        missing.append("py/wheels/psutil-*.whl")
    log("校验 md5:", ok_md5, "| manifest checksum:", ok_sum, "| 缺失:", missing or "无")
    if not (ok_md5 and ok_sum and not missing):
        sys.exit(2)
    log("完成")


if __name__ == "__main__":
    main()
