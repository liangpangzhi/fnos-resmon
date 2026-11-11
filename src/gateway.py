#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fnos-resmon 统一网关入口（fnOS 桌面 iframe 内打开用）

飞牛 fnOS 桌面以 iframe 方式打开第三方应用时，会把 /app/<appname>/... 的请求
经 Unix Socket（${TRIM_APPDEST}/app.sock）转发给应用进程，**并保留路径前缀**。

本脚本职责单一：
  1. 在 ${TRIM_APPDEST}/app.sock 上提供 HTTP 服务（权限 0770）；
  2. 把 /app/<appname>/xxx 剥掉前缀后代理到本机 127.0.0.1:7031/xxx；
  3. 在返回的 HTML 里注入一小段脚本，让页面内写死的绝对路径（/api/...）
     自动补上网关前缀，保证 iframe 下接口不 404。

只用 Python 标准库，兼容飞牛自带的 python3（3.6+）；不影响原有 TCP 端口访问。
"""
import http.client
import os
import signal
import socket
import socketserver
import sys
import time

APPNAME = os.environ.get("TRIM_APPNAME") or "fnos-resmon"
APP_DIR = os.environ.get("TRIM_APPDEST") or os.path.dirname(os.path.abspath(__file__))

SOCK_NAME = "app.sock"
SOCK_PATH = os.environ.get("FNNAS_GATEWAY_SOCKET") or os.path.join(APP_DIR, SOCK_NAME)
PREFIX = (os.environ.get("FNNAS_GATEWAY_PREFIX") or ("/app/" + APPNAME)).rstrip("/")
PORT = int(os.environ.get("PORT") or os.environ.get("TRIM_SERVICE_PORT") or 7031)
UPSTREAM = os.environ.get("RESMON_UPSTREAM") or ("127.0.0.1:%d" % PORT)

# 调试用：设成 127.0.0.1:7098 时改用 TCP 监听（Windows 等无 AF_UNIX 的环境自测）
DEBUG_TCP = os.environ.get("GATEWAY_TCP_DEBUG") or ""

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

# 注入脚本：给页面内写死的绝对路径补上网关前缀（fetch / XHR / EventSource）
INJECT_TMPL = (
    "<script>(function(){var P=__PREFIX__;"
    "function fix(u){if(typeof u!=='string')return u;"
    "if(u.lastIndexOf(P,0)===0)return u;"          # 已经带前缀
    "if(u.charAt(0)!=='/'||u.charAt(1)==='/')return u;"  # 相对/协议外链不动
    "return P+u.slice(1);}"                        # P 末尾已含 "/"
    "var f=window.fetch;"
    "if(typeof f==='function'){window.fetch=function(u,o){"
    "try{if(typeof u==='string'){u=fix(u);}"
    "else if(u&&u.url){u=new Request(fix(u.url),u);}}catch(e){}"
    "return f.call(this,u,o);};}"
    "var X=window.XMLHttpRequest;"
    "if(X&&X.prototype&&X.prototype.open){var op=X.prototype.open;"
    "X.prototype.open=function(){var a=[].slice.call(arguments);"
    "a[1]=fix(a[1]);return op.apply(this,a);};}"
    "var E=window.EventSource;"
    "if(typeof E==='function'){var ES=function(u,c){return new E(fix(u),c);};"
    "ES.prototype=E.prototype;window.EventSource=ES;}"
    "})();</script>"
)


def log(msg):
    sys.stdout.write("[gateway %s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stdout.flush()


def _inject_html(data):
    """把注入脚本插入 HTML 的 <head> 之后；没有 head 就放在最前面。"""
    script = INJECT_TMPL.replace("__PREFIX__", '"%s/"' % PREFIX)
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        return data
    low = text.lower()
    idx = low.find("<head")
    if idx >= 0:
        close = text.find(">", idx)
        if close > 0:
            out = text[:close + 1] + script + text[close + 1:]
        else:
            out = script + text
    else:
        out = script + text
    return out.encode("utf-8")


class Handler(socketserver.StreamRequestHandler):
    """极简反向代理：读一个请求 → 转发到上游 → 回写给客户端。"""

    protocol_version = "HTTP/1.1"
    timeout = 60

    def log_message(self, fmt, *args):  # 不刷日志
        pass

    # ---- 请求解析 ----
    def _read_request(self):
        line = self.rfile.readline(65537)
        if not line:
            return None
        try:
            text = line.decode("iso-8859-1").rstrip("\r\n")
        except Exception:
            return None
        parts = text.split()
        if len(parts) < 3:
            return None
        method, target, version = parts[0], parts[1], parts[2]
        headers = []
        while True:
            h = self.rfile.readline(65537)
            if not h or h in (b"\r\n", b"\n"):
                break
            try:
                hs = h.decode("iso-8859-1").rstrip("\r\n")
            except Exception:
                continue
            if ":" in hs:
                k, _, v = hs.partition(":")
                headers.append((k.strip(), v.strip()))
        return method, target, version, headers

    @staticmethod
    def _inner_path(target):
        if not target.startswith("/"):
            target = "/" + target
        path, sep, query = target.partition("?")
        if path == PREFIX or path == PREFIX + "/":
            path = "/"
        elif path.startswith(PREFIX + "/"):
            path = path[len(PREFIX):]
        return path + sep + query

    def handle(self):
        try:
            req = self._read_request()
            if not req:
                return
            method, target, _version, headers = req

            hmap = {}
            for k, v in headers:
                hmap[k.lower()] = v
            body = b""
            clen = hmap.get("content-length")
            if clen and clen.isdigit():
                body = self.rfile.read(int(clen))

            host, _, port = UPSTREAM.partition(":")
            up_headers = {}
            for k, v in headers:
                lk = k.lower()
                if lk in HOP_BY_HOP or lk in ("host", "content-length", "accept-encoding"):
                    continue
                up_headers[k] = v
            up_headers["Host"] = "%s:%s" % (host, port)
            up_headers["X-Forwarded-Prefix"] = PREFIX
            if body:
                up_headers["Content-Length"] = str(len(body))

            conn = http.client.HTTPConnection(host, int(port), timeout=60)
            try:
                conn.request(method, self._inner_path(target),
                             body=(body if body else None), headers=up_headers)
                resp = conn.getresponse()
                data = resp.read()
                ctype = (resp.getheader("Content-Type") or "").lower()
                if method != "HEAD" and data and "text/html" in ctype:
                    data = _inject_html(data)

                self.wfile.write(("HTTP/1.1 %d %s\r\n" % (resp.status, resp.reason)).encode("iso-8859-1"))
                for k, v in resp.getheaders():
                    lk = k.lower()
                    if lk in HOP_BY_HOP or lk == "content-length":
                        continue
                    self.wfile.write(("%s: %s\r\n" % (k, v)).encode("iso-8859-1"))
                self.wfile.write(("Content-Length: %d\r\n" % len(data)).encode("latin-1"))
                self.wfile.write(b"Connection: close\r\n")
                self.wfile.write(b"\r\n")
                if method != "HEAD":
                    self.wfile.write(data)
            finally:
                conn.close()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                msg = ("网关转发失败: %s" % exc).encode("utf-8")
                self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\n")
                self.wfile.write(b"Content-Type: text/plain; charset=utf-8\r\n")
                self.wfile.write(("Content-Length: %d\r\n\r\n" % len(msg)).encode("latin-1"))
                self.wfile.write(msg)
            except Exception:
                pass


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_unix_server(path):
    """Windows 版 Python 没有 socketserver.UnixStreamServer，故运行时动态构造。"""
    base = getattr(socketserver, "UnixStreamServer", None)
    if base is None:
        raise RuntimeError("当前 Python 不支持 AF_UNIX，无法创建网关 socket")
    cls = type("GatewayUnixServer", (socketserver.ThreadingMixIn, base),
               {"daemon_threads": True})
    return cls(path, Handler)


def main():
    # 上游就绪提示（不阻塞：上游稍后起来也能用）
    log("prefix=%s upstream=%s socket=%s" % (PREFIX, UPSTREAM, SOCK_PATH if not DEBUG_TCP else DEBUG_TCP))

    if DEBUG_TCP:
        host, _, port = DEBUG_TCP.partition(":")
        srv = ThreadingTCPServer((host or "127.0.0.1", int(port or 7098)), Handler)
        log("调试模式：TCP 监听 %s" % DEBUG_TCP)
    else:
        d = os.path.dirname(SOCK_PATH)
        if d:
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                pass
        try:
            if os.path.exists(SOCK_PATH):
                os.unlink(SOCK_PATH)
        except Exception:
            pass
        srv = make_unix_server(SOCK_PATH)
        try:
            os.chmod(SOCK_PATH, 0o770)
        except Exception:
            pass
        log("Unix socket 已就绪：%s (mode 0770)" % SOCK_PATH)

    def _bye(_sig, _frm):
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            if not DEBUG_TCP and os.path.exists(SOCK_PATH):
                os.unlink(SOCK_PATH)
        except Exception:
            pass
        sys.exit(0)

    for sig in ("SIGTERM", "SIGINT"):
        h = getattr(signal, sig, None)
        if h is not None:
            try:
                signal.signal(h, _bye)
            except Exception:
                pass

    srv.serve_forever()


if __name__ == "__main__":
    main()
