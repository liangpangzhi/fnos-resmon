# -*- coding: utf-8 -*-
"""
飞牛桌面网关接入模块

飞牛桌面在 iframe 模式下按 ui/config 的 gatewaySocket（相对 $TRIM_APPDEST）
与 gatewayPrefix 把请求转发到应用的 Unix socket；应用只监听 TCP 端口时，
网关连不上 socket 会统一返回 502 Bad Gateway。

本模块在该 Unix socket 上起一个透明中继：接受网关连接，转接到本机 TCP 服务
（默认 127.0.0.1:7031），双向透传字节，等价于把 socket 变成应用的 HTTP 入口。
"""
import os
import sys
import socket
import select
import threading

_STALE_SUFFIX = ".stale"


def _relocate_stale(path):
    """把残留的 socket 文件改名让位（不做物理删除，保留可追溯痕迹）。"""
    try:
        if not os.path.exists(path):
            return
    except Exception:
        return
    target = path + _STALE_SUFFIX
    for i in range(3):
        try:
            os.replace(path, target if i == 0 else "%s%d" % (target, i))
            return
        except Exception:
            continue


def _pipe(a, b):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 300)
            if not r:
                continue
            if a in r:
                d = a.recv(65536)
                if not d:
                    break
                b.sendall(d)
            if b in r:
                d = b.recv(65536)
                if not d:
                    break
                a.sendall(d)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except Exception:
                pass


def _handle(conn, target_port):
    try:
        up = socket.create_connection(("127.0.0.1", target_port), timeout=10)
        up.settimeout(None)
    except Exception as e:
        sys.stderr.write("gateway 上游连接失败: %s\n" % e)
        try:
            conn.close()
        except Exception:
            pass
        return
    _pipe(conn, up)


def serve(sock_path, target_port):
    if not sock_path:
        return
    srv = None
    for attempt in (1, 2):
        try:
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(sock_path)
            break
        except OSError as e:
            try:
                srv.close()
            except Exception:
                pass
            srv = None
            if attempt == 1:
                _relocate_stale(sock_path)
                continue
            sys.stderr.write("gateway socket 创建失败(%s): %s\n" % (sock_path, e))
            return
    if srv is None:
        return
    try:
        os.chmod(sock_path, 0o770)
    except Exception:
        pass
    try:
        srv.listen(128)
    except Exception as e:
        sys.stderr.write("gateway socket listen 失败: %s\n" % e)
        return
    sys.stderr.write("gateway socket 已监听: %s -> 127.0.0.1:%d\n" % (sock_path, target_port))
    while True:
        try:
            conn, _ = srv.accept()
        except Exception:
            break
        threading.Thread(target=_handle, args=(conn, target_port), daemon=True).start()


def start(appdest, override_sock, port):
    """按飞牛约定推断 socket 路径并在后台线程启动中继，返回 socket 路径。"""
    sock_path = override_sock or (os.path.join(appdest, "app.sock") if appdest else "")
    if not sock_path:
        return ""
    threading.Thread(target=serve, args=(sock_path, int(port)), daemon=True).start()
    return sock_path
