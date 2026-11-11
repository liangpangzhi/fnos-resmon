from flask import Flask, jsonify, request, send_from_directory, session, g
import psutil, os, time, signal, threading, subprocess, platform, glob, json, struct
import hashlib, hmac, random, urllib.request, urllib.parse
from datetime import timedelta
try:
    psutil.PROCFS_PATH = os.environ.get("PROCFS_PATH", "/proc")
except Exception:
    pass

PROCFS = os.environ.get("PROCFS_PATH", "/host/proc")
if os.path.isdir(PROCFS):
    psutil.PROCFS_PATH = PROCFS

app = Flask(__name__, static_folder="public", static_url_path="")
PORT = int(os.environ.get("PORT", "7031"))


# 飞牛桌面网关会带 gatewayPrefix（/app/<应用名>）转发；若上游未剥离该前缀，
# 在 WSGI 层剥离后再交给 Flask 路由，保证同一条链路两种转发方式都能命中。
class _GatewayPrefixStripper(object):
    def __init__(self, wsgi_app, prefix):
        self._app = wsgi_app
        self._prefix = (prefix or "").rstrip("/")

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO") or "/"
        p = self._prefix
        if p and (path == p or path.startswith(p + "/")):
            environ["PATH_INFO"] = path[len(p):] or "/"
            environ["SCRIPT_NAME"] = (environ.get("SCRIPT_NAME") or "") + p
        return self._app(environ, start_response)


app.wsgi_app = _GatewayPrefixStripper(
    app.wsgi_app,
    os.environ.get("FNNAS_GATEWAY_PREFIX") or ("/app/" + os.environ.get("TRIM_APPNAME", "fnos-resmon"))
)

# ---------- CardAuth hub (免费形态: 安装统计/在线/公告/版本推送,失败开放) ----------
HUB_URL = os.environ.get("HUB_URL", "https://auth.88531.cn/api").rstrip("/")
HUB_PROJECT_ID = os.environ.get("HUB_PROJECT_ID", "12")
HUB_VERSION = os.environ.get("SITE_VERSION", "1.3.22")
HUB = {"ok": False, "install_id": None, "latest": "", "update_available": False,
       "update_required": False, "download_url": "", "changelog": "",
       "announcements": [], "popup": [], "ts": 0}

def _hub_key():
    try:
        kf = os.environ.get("HUB_KEY_FILE", "")
        if kf and os.path.exists(kf):
            with open(kf) as f:
                k = f.read().strip()
                if k:
                    return k
    except Exception:
        pass
    return os.environ.get("HUB_API_KEY", "").strip()

def _machine_id():
    raw = ""
    for p in ("/host/etc/machine-id", "/etc/machine-id"):
        try:
            with open(p) as f:
                raw = f.read().strip()
            if raw:
                break
        except Exception:
            continue
    if not raw:
        try:
            raw = open("/proc/sys/kernel/random/uuid").read().strip()
        except Exception:
            raw = str(time.time())
    h = hashlib.sha256(("fnos-resmon-12-" + raw).encode()).hexdigest()
    return str(int(h[:15], 16) % 10**16).zfill(16)[:16]

def _hub_post(path, body):
    key = _hub_key()
    if not key:
        return None
    try:
        req = urllib.request.Request(
            HUB_URL + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Api-Key": key,
                     "X-Project-Id": HUB_PROJECT_ID}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            j = json.loads(r.read().decode())
            return j if j.get("code") == 200 else None
    except Exception:
        return None

def _hub_get_ann():
    try:
        u = (HUB_URL + "/public/announcements?target=client&project_id="
             + urllib.parse.quote(HUB_PROJECT_ID) + "&limit=20")
        with urllib.request.urlopen(urllib.request.Request(u), timeout=8) as r:
            j = json.loads(r.read().decode())
            if j.get("code") != 200:
                return None
            d = j.get("data") or {}
            items = d.get("list", []) or []
            out = [a for a in items
                   if a.get("project_id") is None or str(a.get("project_id")) == str(HUB_PROJECT_ID)]
            return {"all": out, "popup": d.get("popup", []) or []}
    except Exception:
        return None

def _hub_loop():
    mid = _machine_id()
    last_hb, last_cu = 0, 0
    while True:
        try:
            now = time.time()
            if now - last_cu >= 6 * 3600:
                r = _hub_post("/public/installations/check-update",
                              {"machine_id": mid, "client_version": HUB_VERSION,
                               "device_info": "fnos-resmon/docker"})
                if r:
                    d = r.get("data") or {}
                    HUB.update(ok=True, install_id=d.get("install_id"),
                               latest=d.get("latest_version") or "",
                               update_available=bool(d.get("update_available")),
                               update_required=bool(d.get("update_required")),
                               download_url=d.get("download_url") or "",
                               changelog=d.get("changelog") or "")
                    last_cu = now
            if now - last_hb >= 30 * 60:
                r = _hub_post("/public/installations/heartbeat", {"machine_id": mid})
                if r:
                    HUB["ok"] = True
                    iid = (r.get("data") or {}).get("install_id")
                    if iid:
                        HUB["install_id"] = iid
                    last_hb = now
            a = _hub_get_ann()
            if a is not None:
                HUB["announcements"] = a["all"]
                HUB["popup"] = a["popup"]
                HUB["ok"] = True
                HUB["ts"] = now
        except Exception:
            pass
        time.sleep(60)

threading.Thread(target=_hub_loop, daemon=True).start()

# ---------- host netns helpers (桥接模式用) ----------
# 容器切桥接后,直读 /proc/net 只能看到容器自身 netns。
# 网络类数据统一经 nsenter 进入宿主(PID 1) netns 读取,保证与 host 模式一致。
NSENTER = ["nsenter", "--target", "1", "--mount", "--net"]

def _nsenter_run(args, timeout=8):
    try:
        return subprocess.check_output(NSENTER + list(args), text=True,
                                       stderr=subprocess.DEVNULL, timeout=timeout)
    except Exception:
        return ""

from collections import namedtuple as _namedtuple
_NC = _namedtuple("NC", ["bytes_sent", "bytes_recv"])

def _parse_proc_net_dev(txt):
    res = {}
    try:
        for line in (txt or "").splitlines():
            if ":" not in line:
                continue
            nic, rest = line.split(":", 1)
            nic = nic.strip()
            if not nic or nic == "Inter":
                continue
            f = rest.split()
            if len(f) < 16:
                continue
            try:
                recv, sent = int(f[0]), int(f[8])
            except Exception:
                continue
            res[nic] = _NC(sent, recv)
    except Exception:
        pass
    return res

def _host_nic_counters():
    """宿主网卡流量;失败回退 psutil(容器视角)"""
    out = _parse_proc_net_dev(_nsenter_run(["cat", "/proc/net/dev"]))
    if out:
        return out
    try:
        return dict(psutil.net_io_counters(pernic=True) or {})
    except Exception:
        return {}

def _parse_ip_addr(txt):
    out = {}
    try:
        for line in (txt or "").splitlines():
            parts = line.split()
            if len(parts) < 4 or not parts[0].endswith(":"):
                continue
            nic, fam = parts[1], parts[2]
            e = out.setdefault(nic, {"v4": [], "v6": [], "mac": ""})
            if fam == "inet":
                addr = parts[3].split("/")[0]
                if addr and len(addr.split(".")) == 4 and addr not in e["v4"]:
                    e["v4"].append(addr)
            elif fam == "inet6":
                addr = parts[3].split("/")[0].split("%")[0]
                if not addr:
                    continue
                if addr.startswith("fe80"):
                    addr = addr + "(链路)"
                if addr not in e["v6"]:
                    e["v6"].append(addr)
            elif fam.startswith("link") and parts[2] == "link/ether":
                mac = parts[3]
                if mac and mac != "00:00:00:00:00:00":
                    e["mac"] = e["mac"] or mac
    except Exception:
        pass
    return out

def _host_ss():
    """宿主 tcp/udp 连接+监听,经 nsenter 在宿主 netns 执行 ss;失败返回 None"""
    out = _nsenter_run(["ss", "-tunHn", "state", "all"])
    if not out or not out.strip():
        return None
    import re as _re
    tcp_list, listen_list = [], []

    def _split(addr):
        if not addr or addr == "*:*":
            return "", 0
        if addr.startswith("["):
            host, _, port = addr.rpartition(":")
            try:
                return host.strip("[]"), int(port)
            except Exception:
                return host.strip("[]"), 0
        host, _, port = addr.rpartition(":")
        try:
            port = int(port)
        except Exception:
            port = 0
        return host, port

    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        proto = parts[0].lower()
        state = parts[1]
        lh, lp = _split(parts[4])
        rh, rp = _split(parts[5])
        m = _re.search(r'\("([^"]+)",pid=(\d+)', line)
        name = m.group(1) if m else ""
        try:
            pid = int(m.group(2)) if m else None
        except Exception:
            pid = None
        family = "ipv6" if (":" in lh and "." not in lh) else "ipv4"
        if state == "LISTEN":
            listen_list.append({"name": name, "laddr": lh, "port": lp,
                                "family": family, "pid": pid, "proto": proto})
        else:
            tcp_list.append({"name": name,
                             "laddr": f"{lh}:{lp}" if lh else "",
                             "raddr": f"{rh}:{rp}" if rh else "",
                             "family": family, "pid": pid, "status": state})
    return tcp_list, listen_list

# ---------- background sampler ----------
cache = {"procs": [], "cpu": 0.0, "load": [0, 0, 0], "ts": 0}
net_prev = {}
net_rate = {}

def fmt_status(s):
    m = {
        psutil.STATUS_RUNNING: ("活动", 1),
        psutil.STATUS_SLEEPING: ("睡眠", 0),
        psutil.STATUS_DISK_SLEEP: ("睡眠", 0),
        psutil.STATUS_STOPPED: ("停止", 0),
        psutil.STATUS_ZOMBIE: ("僵尸", 0),
    }
    return m.get(s, ("睡眠", 0))

_uid_cache = {"map": {}, "ts": 0}

def uid_name(uid_str):
    # 容器内 /etc/passwd 没有宿主用户,uid 会显示成数字,用宿主 passwd 还原
    if not uid_str or not uid_str.isdigit():
        return uid_str
    now = time.time()
    if now - _uid_cache["ts"] > 300:
        m = {}
        try:
            with open("/host/etc/passwd") as f:
                for line in f:
                    p = line.split(":")
                    if len(p) > 3:
                        try:
                            m[p[2]] = p[0]
                        except Exception:
                            pass
        except Exception:
            pass
        _uid_cache["map"] = m
        _uid_cache["ts"] = now
    return _uid_cache["map"].get(uid_str, uid_str)

def _scan_conns():
    """一次遍历 fd 表统计每个进程的 inet 连接数（约 70ms）。

    替代逐进程 p.net_connections()：后者每调用一次都要重读 /proc/net/*，
    1083 个进程合计 5.3 秒，占单轮采样开销的 93%。
    """
    counts = {}
    # 与 psutil 用同一套 procfs,保证 pid 键与 process_iter 完全对齐
    base = PROCFS if os.path.isdir(PROCFS) else "/proc"
    inos = set()
    for f in ("tcp", "tcp6", "udp", "udp6", "raw", "raw6"):
        try:
            with open(os.path.join(base, "net", f)) as fh:
                next(fh, None)
                for line in fh:
                    parts = line.split()
                    if len(parts) > 9 and parts[9].isdigit():
                        inos.add(parts[9])
        except Exception:
            continue
    try:
        pids = os.listdir(base)
    except Exception:
        return counts
    for pid in pids:
        if not pid.isdigit():
            continue
        n = 0
        try:
            fds = os.listdir(os.path.join(base, pid, "fd"))
        except Exception:
            continue
        for fd in fds:
            try:
                t = os.readlink(os.path.join(base, pid, "fd", fd))
            except Exception:
                continue
            if t.startswith("socket:[") and t[8:-1] in inos:
                n += 1
        if n:
            counts[int(pid)] = n
    return counts


def sample_procs():
    # init cpu percent calculation
    for p in psutil.process_iter():
        try:
            p.cpu_percent(None)
        except Exception:
            pass
    time.sleep(1.0)
    procs = []
    try:
        cpu_total = psutil.cpu_percent(interval=None)
    except Exception:
        cpu_total = 0
    try:
        load = list(os.getloadavg())
    except Exception:
        load = [0, 0, 0]
    # 一次遍历 fd 表拿全量连接数（约 70ms），替代逐进程 p.net_connections()（5.3s）
    conns_map = _scan_conns()
    for p in psutil.process_iter(["pid", "name", "username", "status", "num_threads", "cmdline"]):
        try:
            with p.oneshot():
                cpu = p.cpu_percent(None)
                mem = p.memory_percent()
                try:
                    rss = p.memory_info().rss
                except Exception:
                    rss = 0
                try:
                    io = p.io_counters()
                    rd, wr = io.read_bytes, io.write_bytes
                except Exception:
                    rd, wr = 0, 0
                # 连接数来自一次性的 fd 表扫描,不再逐进程重读 /proc/net/*
                conns = conns_map.get(p.pid, 0)
                info = p.info
            name = info.get("name") or ""
            cmd = " ".join(info.get("cmdline") or [])[:120]
            desc = cmd if cmd else name
            st, act = fmt_status(info.get("status"))
            if (not act) and (cpu or 0) > 1.0:
                st, act = "活动", 1
            procs.append({
                "name": name[:40], "pid": info["pid"],
                "cpu": round(cpu or 0, 2), "mem": round(mem or 0, 2),
                "rss": rss, "uss": None, "swap": None, "threads": info.get("num_threads") or 0,
                "user": uid_name(str(info.get("username") or ""))[:20],
                "status": st, "active": act,
                "desc": desc[:80], "io_r": rd, "io_w": wr, "conns": conns,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    # 只对 RSS 最高的 Top 20 读 smaps 补 USS/SWAP(全量 385ms,Top20 仅约 73ms)
    # 其余进程保持 None,前端 fmtB(null) 显示 "-"
    for row in sorted(procs, key=lambda x: x["rss"], reverse=True)[:20]:
        try:
            mfi = psutil.Process(row["pid"]).memory_full_info()
            row["uss"] = getattr(mfi, "uss", 0) or 0
            row["swap"] = getattr(mfi, "swap", 0) or 0
        except Exception:
            pass
    procs.sort(key=lambda x: x["cpu"], reverse=True)
    cache["procs"] = procs
    cache["cpu"] = round(cpu_total, 1)
    cache["load"] = [round(x, 2) for x in load]
    cache["ts"] = time.time()

# ===== 登录密码防护 (密码存配置文件, 有效期 48 小时) =====
# 密码来源优先级: LOGIN_PASSWORD_FILE 指向的挂载文件 > LOGIN_PASSWORD 环境变量
# 两者都没有 = 不启用防护(向后兼容, 未配置的部署行为不变)
LOGIN_PASSWORD_FILE = os.environ.get("LOGIN_PASSWORD_FILE", "/run/secrets/login_password")
LOGIN_TTL = 48 * 3600          # 登录有效期 48 小时
LOGIN_MAX_FAILS = 10           # 窗口内最多失败次数
LOGIN_FAIL_WINDOW = 600.0      # 失败计数窗口 10 分钟
LOGIN_LOCK_SEC = 60.0          # 触发后锁定 60 秒
_login_fail = {"n": 0, "ts": 0.0, "lock_until": 0.0}


def _load_password():
    """读取密码: 先看挂载的配置文件, 再看环境变量; 都没有返回空串(=关闭防护)"""
    try:
        if os.path.exists(LOGIN_PASSWORD_FILE):
            with open(LOGIN_PASSWORD_FILE) as f:
                p = f.read().strip()
                if p:
                    return p
    except Exception:
        pass
    return os.environ.get("LOGIN_PASSWORD", "").strip()


_LOGIN_PASSWORD = _load_password()

if _LOGIN_PASSWORD:
    # secret_key 由密码+机器ID派生: 稳定(重启不失效), 换密码即让所有旧登录失效
    app.secret_key = hashlib.sha256(
        ("fnos-resmon-login:" + _LOGIN_PASSWORD + _machine_id()).encode()).hexdigest()
    app.permanent_session_lifetime = timedelta(seconds=LOGIN_TTL)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


def _authed():
    """当前请求是否已登录(未启用防护时恒为 True)"""
    if not _LOGIN_PASSWORD:
        return True
    exp = session.get("exp", 0)
    return bool(exp) and time.time() < exp


# 放行路径: 登录接口本身、状态查询、容器健康检查
_AUTH_OPEN = ("/api/login", "/api/auth/status", "/api/health")


@app.before_request
def _require_login():
    """未登录一律拒绝数据接口; 页面/静态资源放行(前端据此渲染登录框)
    必须注册在 _track_page_view 之前: 未登录请求直接短路, 不会唤醒采样线程"""
    if not _LOGIN_PASSWORD:
        return None
    g.authed = _authed()
    if request.path.startswith("/api/") and request.path not in _AUTH_OPEN:
        if not g.authed:
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
    return None


@app.route("/api/auth/status")
def auth_status():
    """前端启动时查询: 是否需要登录、是否已登录、剩余有效秒数"""
    if not _LOGIN_PASSWORD:
        return jsonify({"required": False, "authed": True, "remain": 0})
    exp = session.get("exp", 0)
    remain = max(0, int(exp - time.time())) if exp else 0
    return jsonify({"required": True, "authed": _authed(), "remain": remain})


@app.route("/api/login", methods=["POST"])
def api_login():
    if not _LOGIN_PASSWORD:
        return jsonify({"ok": True, "required": False})
    now = time.time()
    if now < _login_fail["lock_until"]:
        return jsonify({"ok": False, "error": "失败次数过多，请 %d 秒后再试"
                        % int(_login_fail["lock_until"] - now)}), 429
    d = request.get_json(force=True, silent=True) or {}
    given = str(d.get("password", "") or "")
    # 恒定时间比较, 防时序侧信道
    ok = bool(given) and hmac.compare_digest(given.encode(), _LOGIN_PASSWORD.encode())
    if not ok:
        if now - _login_fail["ts"] > LOGIN_FAIL_WINDOW:
            _login_fail["n"] = 0
        _login_fail["n"] += 1
        _login_fail["ts"] = now
        if _login_fail["n"] >= LOGIN_MAX_FAILS:
            _login_fail["lock_until"] = now + LOGIN_LOCK_SEC
            _login_fail["n"] = 0
        time.sleep(0.3)   # 拖慢暴力猜测
        return jsonify({"ok": False, "error": "密码错误"}), 401
    _login_fail["n"] = 0
    session.clear()
    session["exp"] = int(now + LOGIN_TTL)
    session.permanent = True
    return jsonify({"ok": True, "remain": LOGIN_TTL})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


# ===== 按需采样: 只有页面被打开时才采集数据 =====
# 页面关闭后不再有浏览器请求, 采样线程转入空闲(不采集任何数据, 开销趋近 0);
# 页面重新打开时由 before_request 立即同步补采, 首屏即可拿到新鲜数据。
SAMPLE_IDLE_SEC = 90.0   # 无页面请求超过该秒数即停止采样(必须 > 前端最大刷新间隔 60s)
STALE_SEC = 10.0         # 缓存超过该秒数未更新, 页面请求到来时同步补采
SAMPLE_LOCK = threading.Lock()
_last_page_view = 0.0    # 最后一次来自页面(浏览器)的请求时间


def _is_browser_view(req):
    """区分"打开了页面的浏览器"与后台脚本/监控轮询 —— 脚本不带 Referer/Sec-Fetch"""
    if req.headers.get("Referer") or req.headers.get("Sec-Fetch-Mode"):
        return True
    if req.path == "/":
        # 地址栏直接打开时浏览器不带 Referer, 只能靠 UA; curl/python 脚本不会带 Mozilla/
        ua = req.headers.get("User-Agent") or ""
        return ua.startswith("Mozilla/") or "HeadlessChrome" in ua
    return False


def _sample_if_stale(max_age):
    """缓存过期则加锁同步采样一次(双检, 避免并发请求重复采样)"""
    if time.time() - cache.get("ts", 0) <= max_age:
        return
    with SAMPLE_LOCK:
        if time.time() - cache.get("ts", 0) <= max_age:
            return
        try:
            sample_procs()
        except Exception:
            pass


@app.before_request
def _track_page_view():
    """只让"页面访问"触发/维持采样; 脚本轮询不会唤醒采样线程"""
    global _last_page_view
    try:
        # 未登录的页面访问不唤醒采样(登录框页面不算"打开监控页面")
        if _LOGIN_PASSWORD and not getattr(g, "authed", False):
            return None
        if _is_browser_view(request):
            _last_page_view = time.time()
            if request.path.startswith("/api/"):
                _sample_if_stale(STALE_SEC)
    except Exception:
        pass
    return None


def sampler_loop():
    while True:
        try:
            if time.time() - _last_page_view <= SAMPLE_IDLE_SEC:
                # 有人在看页面: 每 5 秒采集一次
                with SAMPLE_LOCK:
                    sample_procs()
            # 无人访问: 不采集, 仅保留极轻量的定时检查
        except Exception:
            pass
        time.sleep(5)

threading.Thread(target=sampler_loop, daemon=True).start()
# 采样只在页面被打开期间进行; 无人访问时 CPU 开销趋近于 0

@app.route("/")
def index():
    return send_from_directory("public", "index.html")

@app.route("/api/cpu")
def api_cpu():
    active = sum(1 for p in cache["procs"] if p["active"])
    return jsonify({"cpu": cache["cpu"], "load": cache["load"],
                    "active": active, "total": len(cache["procs"]),
                    "info": get_cpu_info(), "percpu": get_percpu(),
                    "mhz": get_percpu_mhz(),
                    "items": cache["procs"][:100]})

_cpu_static = {}

def _parse_host_cpuinfo():
    info = {}
    try:
        with open("/host/proc/cpuinfo") as f:
            txt = f.read()
    except Exception:
        try:
            with open("/proc/cpuinfo") as f:
                txt = f.read()
        except Exception:
            return info
    # 取第一个 processor 段的 model name / cache size / cpu MHz
    for line in txt.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if k == "model name" and "model" not in info:
            info["model"] = v
        elif k == "cache size" and "cache" not in info:
            info["cache"] = v
    return info

def _parse_lscpu():
    out = {}
    try:
        txt = subprocess.run(["lscpu"], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return out
    for line in txt.splitlines():
        if ":" not in line and "：" not in line:
            continue
        line = line.replace("：", ":")
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        out[k] = v
    return out

def get_cpu_info():
    global _cpu_static
    if not _cpu_static:
        ci = _parse_host_cpuinfo()
        lc = _parse_lscpu()
        def pick(*keys):
            for k in keys:
                if k in lc and lc[k]:
                    return lc[k]
            return ""
        # L1d/L1i/L2/L3 键名中英文兼容
        l1d = pick("L1d 缓存", "L1d cache")
        l1i = pick("L1i 缓存", "L1i cache")
        l2 = pick("L2 缓存", "L2 cache")
        l3 = pick("L3 缓存", "L3 cache")
        _cpu_static = {
            "model": ci.get("model") or pick("型号名称", "Model name"),
            "arch": platform.machine(),
            "sockets": pick("座", "Socket(s)"),
            "cores_per_socket": pick("每个座的核数", "Core(s) per socket"),
            "threads_per_core": pick("每个核的线程数", "Thread(s) per core"),
            "l1d": l1d, "l1i": l1i, "l2": l2, "l3": l3,
            "cache": ci.get("cache", ""),
        }
    try:
        f = psutil.cpu_freq()
        cur = round(f.current, 0) if f else 0
        fmin = round(f.min, 0) if f and f.min else 0
        fmax = round(f.max, 0) if f and f.max else 0
    except Exception:
        cur = fmin = fmax = 0
    d = dict(_cpu_static)
    d.update({
        "logical": psutil.cpu_count() or 0,
        "physical": psutil.cpu_count(logical=False) or 0,
        "freq_cur": cur, "freq_min": fmin, "freq_max": fmax,
    })
    return d

def get_percpu():
    try:
        vals = psutil.cpu_percent(interval=None, percpu=True)
        return [round(v, 1) for v in (vals or [])]
    except Exception:
        return []

def get_percpu_mhz():
    """每核实时频率(MHz): 优先读 cpufreq scaling_cur_freq,读不到回退 cpuinfo"""
    mhz = []
    try:
        paths = sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"))
        if not paths:
            for base in ("/host/sys/devices/system/cpu", "/sys/devices/system/cpu"):
                try:
                    names = sorted([d for d in os.listdir(base) if d.startswith("cpu") and d[3:].isdigit()])
                except Exception:
                    continue
                for n in names:
                    paths.append(os.path.join(base, n, "cpufreq/scaling_cur_freq"))
                if paths:
                    break
        for p in paths:
            try:
                with open(p) as f:
                    v = float(f.read().strip())
                mhz.append(int(round(v / 1000.0)))
            except Exception:
                continue
    except Exception:
        pass
    if not mhz:
        try:
            for cp in ("/host/proc/cpuinfo", "/proc/cpuinfo"):
                try:
                    vals = []
                    with open(cp) as f:
                        for line in f:
                            if line.startswith("cpu MHz"):
                                vals.append(int(round(float(line.split(":")[1].strip()))))
                    if vals:
                        mhz = vals
                        break
                except Exception:
                    continue
        except Exception:
            pass
    return mhz

# ---------- 各页测速: CPU/内存/硬盘/网络(非破坏,失败返回error) ----------
import urllib.request as _urlreq

def _bench_cpu(dur=10):
    try:
        dur = max(1, min(200, int(dur)))
    except Exception:
        dur = 10
    N_total = 0
    x = 0
    t0 = time.perf_counter()
    batch = 200000
    while True:
        for i in range(batch):
            x += (i * i) % 10007
        N_total += batch
        if time.perf_counter() - t0 >= dur:
            break
    dt = max(time.perf_counter() - t0, 1e-6)
    return {"ms": int(round(dt * 1000)), "kops": int(round(N_total / dt / 1000)),
            "score": int(round(N_total / dt / 1000)), "dur": round(dt, 1),
            "detail": "整数运算持续约%s秒" % dur}

def _bench_mem(dur=10):
    try:
        dur = max(1, min(200, int(dur)))
    except Exception:
        dur = 10
    SZ = 32 * 1024 * 1024
    b = bytearray(SZ)
    t0 = time.perf_counter()
    w_bytes = 0
    while True:
        b[:] = b"\x5a" * SZ
        w_bytes += SZ
        if time.perf_counter() - t0 >= dur / 2.0:
            break
    dt_w = max(time.perf_counter() - t0, 1e-6)
    t0 = time.perf_counter()
    r_bytes = 0
    s = 0
    while True:
        s += sum(b[::4096])
        r_bytes += SZ
        if time.perf_counter() - t0 >= dur / 2.0:
            break
    dt_r = max(time.perf_counter() - t0, 1e-6)
    del b
    return {"write_mbs": round(w_bytes / dt_w / 1048576, 1),
            "read_mbs": round(r_bytes / dt_r / 1048576, 1),
            "size_mb": 32, "dur": dur, "detail": "内存写/读持续约%s秒" % dur, "checksum": s % 997}

def _bench_disk(dur=10):
    try:
        dur = max(1, min(200, int(dur)))
    except Exception:
        dur = 10
    CHUNK = 4 * 1024 * 1024
    data = os.urandom(CHUNK)
    path = ""
    for cand in ("/host/vol1/.fnos_resmon_bench.tmp",
                 "/host/.fnos_resmon_bench.tmp", "/tmp/.fnos_resmon_bench.tmp"):
        try:
            d = os.path.dirname(cand)
            if os.path.isdir(d) and os.access(d, os.W_OK):
                path = cand
                break
        except Exception:
            continue
    if not path:
        return {"ok": False, "error": "无可写目录"}
    try:
        w_bytes = 0
        t0 = time.perf_counter()
        with open(path, "wb") as f:
            while True:
                f.write(data)
                w_bytes += CHUNK
                if time.perf_counter() - t0 >= dur / 2.0:
                    break
            f.flush()
            os.fsync(f.fileno())
        dt_w = max(time.perf_counter() - t0, 1e-6)
        t0 = time.perf_counter()
        r_bytes = 0
        with open(path, "rb") as f:
            while True:
                ch = f.read(CHUNK)
                if not ch:
                    f.seek(0)
                    continue
                r_bytes += len(ch)
                if time.perf_counter() - t0 >= dur / 2.0:
                    break
        dt_r = max(time.perf_counter() - t0, 1e-6)
        return {"write_mbs": round(w_bytes / dt_w / 1048576, 1),
                "read_mbs": round(r_bytes / dt_r / 1048576, 1),
                "size_mb": round(w_bytes / 1048576, 0), "dur": dur,
                "file": path.replace("/host", "") or "/",
                "detail": "文件写(含落盘)/读持续约%s秒" % dur}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass

def _bench_net():
    out = {}
    try:
        url = "https://speed.cloudflare.com/__down?bytes=5000000"
        t0 = time.perf_counter()
        req = _urlreq.Request(url, headers={"User-Agent": "fnos-resmon"})
        with _urlreq.urlopen(req, timeout=25) as r:
            n = 0
            while True:
                ch = r.read(256 * 1024)
                if not ch:
                    break
                n += len(ch)
        dt = max(time.perf_counter() - t0, 1e-6)
        out["down_mbps"] = round(n * 8 / dt / 1000000, 1)
        out["down_mb"] = round(n / 1048576, 1)
        out["down_s"] = round(dt, 1)
    except Exception as e:
        return {"ok": False, "error": "下载测速失败: %s" % e}
    try:
        payload = os.urandom(2 * 1024 * 1024)
        t0 = time.perf_counter()
        req = _urlreq.Request("https://speed.cloudflare.com/__up",
                              data=payload, method="POST",
                              headers={"Content-Type": "application/octet-stream",
                                       "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with _urlreq.urlopen(req, timeout=25) as r:
            r.read()
        dt = max(time.perf_counter() - t0, 1e-6)
        out["up_mbps"] = round(len(payload) * 8 / dt / 1000000, 1)
        out["up_mb"] = round(len(payload) / 1048576, 1)
        out["up_s"] = round(dt, 1)
    except Exception as e:
        out["up_mbps"] = 0
        out["up_error"] = "上传测速失败: %s" % e
    out.update({"ok": True, "detail": "Cloudflare 测速点: 下行5MB/上行2MB"})
    return out

@app.route("/api/bench/<kind>", methods=["POST"])
def api_bench(kind):
    try:
        dur = 10
        try:
            j0 = request.get_json(silent=True) or {}
            if isinstance(j0, dict) and "dur" in j0:
                dur = int(j0["dur"])
            elif request.values.get("dur"):
                dur = int(request.values.get("dur"))
        except Exception:
            dur = 10
        dur = max(1, min(200, dur))
        if kind == "cpu":
            r = _bench_cpu(dur)
            p = _read_perf()
            r.update({"ok": True, "mode": p.get("mode", ""),
                      "governor": p.get("governor", ""), "epp": p.get("epp", ""),
                      "cap_max_mhz": p.get("cap_max_mhz", 0),
                      "hw_max_mhz": p.get("hw_max_mhz", 0),
                      "capped": p.get("capped", False),
                      "mhz_avg": (sum(get_percpu_mhz()) // len(get_percpu_mhz()) if get_percpu_mhz() else 0)})
        elif kind == "mem":
            r = _bench_mem(dur)
            r["ok"] = True
        elif kind == "disk":
            r = _bench_disk(dur)
            if "ok" not in r:
                r["ok"] = True
        elif kind == "net":
            r = _bench_net()
        else:
            return jsonify({"ok": False, "error": "未知测速类型"}), 400
        r["at"] = time.strftime("%m-%d %H:%M:%S", time.localtime())
        return jsonify(r)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ---------- CPU 性能模式: 省电/平衡/高性能(写宿主 cpufreq,失败开放) ----------
PERF_FILE = os.environ.get("PERF_FILE", "/run/secrets/perf_mode.json")
PERF_FILE_FALLBACKS = [PERF_FILE, "/app/perf_mode.json"]
PERF_MODES = {
    "powersave": {"governor": "powersave", "epp": "power",
                  "name": "省电模式"},
    "balanced": {"governor": "powersave", "epp": "balance_performance",
                 "name": "平衡模式"},
    "performance": {"governor": "performance", "epp": "performance",
                    "name": "高性能模式"},
}

def _saved_perf():
    for p in PERF_FILE_FALLBACKS:
        try:
            with open(p) as f:
                d = json.load(f) or {}
                m = d.get("mode", "")
                if m in PERF_MODES:
                    return {"mode": m, "cap": int(d.get("cap") or 0)}
        except Exception:
            continue
    return {"mode": "", "cap": 0}

def _saved_perf_mode():
    return _saved_perf().get("mode", "")

def _read_cpu_freq_limits():
    cur_max, hw_max, hw_min = 0, 0, 0
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq") as f:
            cur_max = int(f.read().strip()) // 1000
    except Exception:
        pass
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq") as f:
            hw_max = int(f.read().strip()) // 1000
    except Exception:
        pass
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_min_freq") as f:
            hw_min = int(f.read().strip()) // 1000
    except Exception:
        pass
    return cur_max, hw_max, hw_min

def _read_perf():
    gov, epp = "", ""
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") as f:
            gov = f.read().strip()
    except Exception:
        pass
    try:
        with open("/sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference") as f:
            epp = f.read().strip().split()[0]
    except Exception:
        pass
    mode = ""
    for k, v in PERF_MODES.items():
        if v["governor"] == gov and (not epp or v["epp"] == epp):
            mode = k
            break
    cur_max, hw_max, hw_min = _read_cpu_freq_limits()
    capped = bool(hw_max and cur_max and cur_max < hw_max)
    return {"governor": gov, "epp": epp, "mode": mode,
            "saved": _saved_perf_mode(), "cap_max_mhz": cur_max,
            "hw_max_mhz": hw_max, "hw_min_mhz": hw_min, "capped": capped}

def _apply_perf(mode, cap_mhz=0):
    if mode not in PERF_MODES:
        return {"ok": False, "error": "未知模式"}
    want = PERF_MODES[mode]
    ok_g = fail_g = ok_e = fail_e = 0
    for p in sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor")):
        try:
            with open(p, "w") as f:
                f.write(want["governor"])
            ok_g += 1
        except Exception:
            fail_g += 1
    for p in sorted(glob.glob("/sys/devices/system/cpu/cpufreq/policy*/energy_performance_preference")):
        try:
            with open(p, "w") as f:
                f.write(want["epp"])
            ok_e += 1
        except Exception:
            fail_e += 1
    ok_c = fail_c = 0
    cap_applied = 0
    target = 0
    if mode == "powersave" and cap_mhz:
        target = cap_mhz * 1000
    elif mode != "powersave":
        try:
            with open("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq") as f:
                target = int(f.read().strip())
        except Exception:
            target = 0
    if target:
        for p in sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_max_freq")) + \
                sorted(glob.glob("/sys/devices/system/cpu/cpufreq/policy*/scaling_max_freq")):
            try:
                with open(p, "w") as f:
                    f.write(str(target))
                ok_c += 1
            except Exception:
                fail_c += 1
        cap_applied = target // 1000
    if ok_g == 0 and fail_g > 0:
        return {"ok": False, "error": "调频接口不可写( governor 写入失败 )"}
    try:
        with open(PERF_FILE, "w") as f:
            json.dump({"mode": mode, "cap": (cap_mhz if mode == "powersave" else 0)}, f)
    except Exception:
        try:
            with open("/app/perf_mode.json", "w") as f:
                json.dump({"mode": mode, "cap": (cap_mhz if mode == "powersave" else 0)}, f)
        except Exception:
            pass
    st = _read_perf()
    st.update({"ok": True, "mode": mode, "gov_ok": ok_g, "gov_fail": fail_g,
               "epp_ok": ok_e, "epp_fail": fail_e,
               "cap_ok": ok_c, "cap_fail": fail_c, "cap_applied_mhz": cap_applied,
               "msg": "已切换为%s,立即生效,不重启电脑" % want["name"]})
    return st

@app.route("/api/cpu/perf")
def api_cpu_perf():
    return jsonify(_read_perf())

@app.route("/api/cpu/perf", methods=["POST"])
def api_cpu_perf_set():
    try:
        body = request.get_json(force=True, silent=True) or {}
        mode = body.get("mode", "")
        try:
            cap_mhz = int(body.get("cap_mhz") or 0)
        except Exception:
            cap_mhz = 0
        if cap_mhz < 0:
            cap_mhz = 0
    except Exception:
        mode, cap_mhz = "", 0
    r = _apply_perf(mode, cap_mhz)
    return jsonify(r), (200 if r.get("ok") else 400)

try:
    _sv = _saved_perf()
    if _sv.get("mode"):
        _apply_perf(_sv["mode"], _sv.get("cap") or 0)
except Exception:
    pass

@app.route("/api/mem")
def api_mem():
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    # USS/SWAP 只对 RSS 最高的 Top 20 读 smaps(见 sample_procs),
    # 这里只返回已采样的进程,保证不会出现 "-" 空行;按 mem 排序展示
    items = [p for p in sorted(cache["procs"], key=lambda x: x["mem"], reverse=True)
             if p.get("uss") is not None][:20]
    mi = _read_meminfo_host()
    dm = get_mem_hw()
    return jsonify({
        "total": vm.total, "used": vm.used, "available": vm.available,
        "percent": vm.percent, "cached": getattr(vm, "cached", 0),
        "swap_total": sm.total, "swap_used": sm.used, "swap_percent": sm.percent,
        "detail": mi, "hw": dm,
        "items": items})

_mem_hw_static = {}

def _read_meminfo_host():
    mi = {}
    for p in ("/host/proc/meminfo", "/proc/meminfo"):
        try:
            with open(p) as f:
                for line in f:
                    if ":" not in line:
                        continue
                    k, v = line.split(":", 1)
                    parts = v.strip().split()
                    if not parts:
                        continue
                    try:
                        num = int(parts[0])
                    except ValueError:
                        continue
                    # 统一转 bytes
                    if len(parts) > 1 and parts[1] == "kB":
                        num *= 1024
                    mi[k.strip()] = num
            if mi:
                break
        except Exception:
            continue
    return mi

def _walk_dmi(path):
    """yield (type, raw_bytes, strings)"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return
    off, n = 0, 0
    L = len(data)
    while off + 4 <= L and n < 200:
        typ, ln = data[off], data[off + 1]
        if ln < 4 or off + ln > L:
            break
        if typ == 127:  # end of table
            break
        s = off + ln
        strs = []
        ok = True
        while s < L:
            try:
                e = data.index(b"\x00", s)
            except ValueError:
                ok = False
                break
            if e == s:
                s += 1
                break
            strs.append(data[s:e].decode("utf-8", "replace").strip())
            s = e + 1
        if not ok:
            break
        yield typ, data[off:off + ln], strs
        off = s
        n += 1

_MEM_TYPE = {0x02: "Unknown", 0x03: "DRAM", 0x12: "DDR", 0x13: "DDR2",
             0x14: "DDR2 FB-DIMM", 0x18: "DDR3", 0x19: "FBD2", 0x1A: "DDR4",
             0x1B: "LPDDR", 0x1C: "LPDDR2", 0x1D: "LPDDR3", 0x1E: "LPDDR4",
             0x1F: "Logical NV", 0x20: "HBM", 0x21: "HBM2", 0x22: "DDR5",
             0x23: "LPDDR5", 0x24: "HBM3"}

def _parse_dmi_mem():
    dimms, max_cap_kb = [], 0
    for dmi_path in ("/host/sys/firmware/dmi/tables/DMI",
                     "/sys/firmware/dmi/tables/DMI"):
        found = False
        for typ, raw, strs in _walk_dmi(dmi_path):
            found = True
            def S(i):
                return strs[i - 1] if 1 <= i <= len(strs) else ""
            if typ == 16 and len(raw) >= 11:
                import struct
                (cap,) = struct.unpack_from("<I", raw, 7)
                if cap not in (0, 0x80000000):
                    max_cap_kb = cap  # SMBIOS 单位 kB
            elif typ == 17 and len(raw) >= 28:
                import struct
                (size,) = struct.unpack_from("<H", raw, 12)
                if size == 0 or size == 0xFFFF:
                    continue  # 空槽
                if size & 0x8000:
                    mb = size & 0x7FFF  # bit15=1 时单位 KB→转MB(向上)
                    size_mb = max(mb // 1024, 1) if mb >= 1024 else 1
                    size_b = (size & 0x7FFF) * 1024
                else:
                    size_mb = size & 0x7FFF
                    size_b = size_mb * 1024 * 1024
                if size_mb == 0x7FFF:
                    continue
                (speed,) = struct.unpack_from("<H", raw, 20)
                cspeed = 0
                if len(raw) >= 30:
                    (cspeed,) = struct.unpack_from("<H", raw, 28)
                mtype = raw[18] if len(raw) > 18 else 0
                dimms.append({
                    "locator": S(raw[16]) if len(raw) > 16 else "",
                    "bank": S(raw[17]) if len(raw) > 17 else "",
                    "size_b": size_b, "size_mb": size_mb,
                    "type": _MEM_TYPE.get(mtype, "DDR4" if mtype == 0 else hex(mtype)),
                    "speed": speed or 0, "cspeed": cspeed or 0,
                    "maker": S(raw[22]) if len(raw) > 22 else "",
                    "serial": S(raw[23]) if len(raw) > 23 else "",
                    "part": S(raw[25]) if len(raw) > 25 else "",
                })
        if found:
            break
    return dimms, max_cap_kb

def _get_vram():
    total = 0
    for base in ("/host/sys/class/drm", "/sys/class/drm"):
        try:
            for p in glob.glob(base + "/card*/device/mem_info_vram_total"):
                try:
                    with open(p) as f:
                        total += int(f.read().strip())
                except Exception:
                    pass
        except Exception:
            pass
        if total:
            break
    return total

def get_mem_hw():
    global _mem_hw_static
    if not _mem_hw_static:
        dimms, max_cap_kb = [], 0
        # 优先读构建时从宿主 dmidecode 导出的静态槽位表
        for jp in ("hw_mem.json",
                   os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "hw_mem.json")):
            try:
                with open(jp) as f:
                    hj = json.load(f)
                dimms = hj.get("dimms", []) or []
                max_cap_kb = int((hj.get("max_cap_b", 0) or 0) // 1024)
                if dimms:
                    break
            except Exception:
                continue
        if not dimms:
            dimms, max_cap_kb = _parse_dmi_mem()
        installed = sum(d["size_b"] for d in dimms)
        _mem_hw_static = {"dimms": dimms, "installed": installed,
                          "max_cap_kb": max_cap_kb}
    d = {"dimms": _mem_hw_static["dimms"],
         "installed": _mem_hw_static["installed"],
         "max_cap_kb": _mem_hw_static["max_cap_kb"]}
    try:
        mem_total = psutil.virtual_memory().total
    except Exception:
        mem_total = 0
    d["vram"] = _get_vram()
    d["system_total"] = mem_total
    d["reserved"] = (d["installed"] - mem_total) if d["installed"] and mem_total else 0
    return d

HOST_MNTS = [("/", "/host"), ("/vol1", "/host/vol1"),
              ("/vol2", "/host/vol2"), ("/vol3", "/host/vol3")]

def _df_host():
    """容器内 df 直读宿主路径(经 bind 进来的宿主目录),返回 {mnt: {...}}"""
    try:
        out = subprocess.run(
            ["df", "-B1", "--output=source,target,fstype,size,used,avail",
             "/host", "/host/vol1", "/host/vol2", "/host/vol3"],
            capture_output=True, text=True, timeout=5).stdout.splitlines()
        res = {}
        for line in out[1:]:
            p = line.split()
            if len(p) < 6:
                continue
            dev, tgt, fst, size, used, avail = p[0], p[1], p[2], int(p[3]), int(p[4]), int(p[5])
            mnt = tgt.replace("/host", "", 1) or "/"
            res[mnt] = {"device": dev, "mount": mnt, "fstype": fst,
                        "total": size, "used": used, "free": avail,
                        "percent": round(used / size * 100, 1) if size else 0}
        return res
    except Exception:
        return {}

@app.route("/api/cpu/advise")
def api_cpu_advise():
    """负载诊断:档位结论 + 能否顶住 + 优化建议"""
    try:
        load = list(os.getloadavg())
    except Exception:
        load = [0, 0, 0]
    try:
        cpu_pct = psutil.cpu_percent(interval=None)
    except Exception:
        cpu_pct = 0
    try:
        vm = psutil.virtual_memory()
    except Exception:
        vm = None
    logical = psutil.cpu_count() or 1
    l1 = load[0]
    per_core = l1 / logical
    if per_core < 0.5 and (cpu_pct or 0) < 50:
        level, verdict = "轻松", "✅ 当前配置完全顶得住，负载很轻，还有大量余量。"
    elif per_core < 0.8 and (cpu_pct or 0) < 75:
        level, verdict = "正常", "✅ 当前配置顶得住，负载正常，日常使用流畅。"
    elif per_core < 1.0 and (cpu_pct or 0) < 90:
        level, verdict = "偏高", "⚠️ 当前配置勉强顶住，高峰期可能卡顿，建议按下面方法优化。"
    else:
        level, verdict = "过载", "🔴 当前配置顶不住了！负载已超过 CPU 承受能力，请立即优化或升级。"
    tips = []
    tops = sorted(cache.get("procs", []), key=lambda x: x.get("cpu", 0), reverse=True)[:3]
    if tops and (tops[0].get("cpu") or 0) > 50:
        tips.append("🎯 头号吃CPU进程是 %s（占用 %s%%，PID %s），如非必需请结束它，效果最明显。" % (
            tops[0].get("name"), tops[0].get("cpu"), tops[0].get("pid")))
    if vm is not None and vm.percent > 85:
        tips.append("💾 内存已用 %s%%，内存不足会拖慢 CPU（频繁 swap）。先点内存页「一键回收」，或加内存条。" % vm.percent)
    tips.append("🧹 点下方「一键优化系统」：自动数据落盘 + 回收内存缓存，约20秒见效。")
    tips.append("📦 精简开机自启与常驻 Docker 容器，不用的先停掉（docker stop），负载立降。")
    tips.append("🌡️ 检查散热：CPU 过热会自动降频，清理灰尘、保证通风，频率上去了自然流畅。")
    return jsonify({"load": [round(x, 2) for x in load], "cpu": round(cpu_pct or 0, 1),
                    "logical": logical, "per_core": round(per_core, 2),
                    "level": level, "verdict": verdict, "tips": tips,
                    "top_cpu": [{"name": t.get("name"), "cpu": t.get("cpu"), "pid": t.get("pid")} for t in tops]})

_OPT_JOB = {"running": False, "done": False, "cancelled": False, "cancel": False,
            "steps": [], "freed": 0, "available": 0, "started": 0}

def _optimize_worker():
    job = _OPT_JOB
    steps = []
    try:
        before_mem = psutil.virtual_memory().available
    except Exception:
        before_mem = 0
    try:
        subprocess.run(["sync"], check=False, timeout=20)
        steps.append("数据落盘(sync)完成")
    except Exception as e:
        steps.append("数据落盘跳过: %s" % e)
    if job.get("cancel"):
        job["steps"] = steps + ["已手动停止,后续步骤取消"]
        job["cancelled"] = True
        job["done"] = False
        job["running"] = False
        job["cancel"] = False
        try:
            job["available"] = psutil.virtual_memory().available
        except Exception:
            pass
        return
    try:
        p = os.environ.get("PROCFS_PATH", "/proc")
        with open(os.path.join(p, "sys", "vm", "drop_caches"), "w") as f:
            f.write("3")
        steps.append("内存缓存已回收(drop caches)")
    except Exception as e:
        steps.append("缓存回收跳过: %s" % e)
    try:
        after_mem = psutil.virtual_memory().available
    except Exception:
        after_mem = 0
    job["steps"] = steps
    job["freed"] = max(0, after_mem - before_mem)
    job["available"] = after_mem
    job["done"] = True
    job["cancelled"] = False
    job["running"] = False

@app.route("/api/sys/optimize", methods=["POST"])
def api_sys_optimize():
    """一键优化系统: 后台线程做 sync + drop caches,不阻塞监控轮询.
    已去掉 compact_memory(大内存下会卡住数分钟,正是之前掉线的原因)"""
    if _OPT_JOB.get("running"):
        return jsonify({"ok": False, "running": True,
                        "msg": "优化正在进行中,请稍候"}), 409
    _OPT_JOB.update({"running": True, "done": False, "cancelled": False, "cancel": False,
                     "steps": [], "freed": 0, "available": 0, "started": time.time()})
    threading.Thread(target=_optimize_worker, daemon=True).start()
    return jsonify({"ok": True, "started": True, "eta": 20})

@app.route("/api/sys/optimize/cancel", methods=["POST"])
def api_sys_optimize_cancel():
    """停止优化: 置取消标记,worker 在步骤间隙退出,不阻塞"""
    if not _OPT_JOB.get("running"):
        return jsonify({"ok": False, "msg": "当前没有正在进行的优化"})
    _OPT_JOB["cancel"] = True
    return jsonify({"ok": True, "msg": "已发送停止信号"})

@app.route("/api/sys/optimize/status")
def api_sys_optimize_status():
    job = _OPT_JOB
    try:
        elapsed = round(time.time() - job.get("started", 0), 1) if job.get("started") else 0
    except Exception:
        elapsed = 0
    return jsonify({"running": job.get("running", False),
                    "done": job.get("done", False),
                    "cancelled": job.get("cancelled", False),
                    "steps": job.get("steps", []),
                    "freed": job.get("freed", 0),
                    "available": job.get("available", 0),
                    "elapsed": elapsed})

@app.route("/api/sensors")
def api_sensors():
    """CPU温度/风扇转速: 优先读宿主 /host/sys hwmon,读不到返回空+提示"""
    temps, fans = [], []
    HWMON_LABEL = {"k10temp": "CPU", "zenpower": "CPU", "coretemp": "CPU",
                   "amdgpu": "核显", "radeon": "核显", "nct6775": "主板",
                   "nct6683": "主板", "nct6686": "主板", "nct6687": "主板",
                   "drivetemp": "硬盘", "nvme": "硬盘", "iwlwifi": "无线",
                   "mt7921_phy0": "无线", "r8169_0_2a00:00": "网卡", "r8169": "网卡"}
    try:
        for base in ("/host/sys/class/hwmon", "/sys/class/hwmon"):
            if not os.path.isdir(base):
                continue
            for h in sorted(os.listdir(base)):
                d = os.path.join(base, h)
                if not os.path.isdir(d):
                    continue
                try:
                    with open(os.path.join(d, "name")) as f:
                        chip = f.read().strip()
                except Exception:
                    chip = h
                label = HWMON_LABEL.get(chip, chip)
                for i in (1, 2, 3, 4, 5):
                    tf = os.path.join(d, "temp%d_input" % i)
                    if not os.path.isfile(tf):
                        continue
                    try:
                        with open(tf) as f:
                            v = float(f.read().strip()) / 1000.0
                        if -40 < v < 130:
                            lf = os.path.join(d, "temp%d_label" % i)
                            nm = label
                            try:
                                if os.path.isfile(lf):
                                    with open(lf) as f:
                                        nm = "%s(%s)" % (label, f.read().strip())
                            except Exception:
                                pass
                            temps.append({"name": nm, "value": round(v, 1)})
                    except Exception:
                        pass
                for i in (1, 2, 3, 4, 5, 6):
                    ff = os.path.join(d, "fan%d_input" % i)
                    if not os.path.isfile(ff):
                        continue
                    try:
                        with open(ff) as f:
                            v = int(float(f.read().strip()))
                        if v > 0:
                            fans.append({"name": "%s 风扇%d" % (label, i), "value": v})
                    except Exception:
                        pass
            if temps or fans:
                break
    except Exception:
        pass
    note = "" if (temps or fans) else "宿主未暴露温度/转速传感器(容器读不到属正常,见 README 说明)"
    return jsonify({"temps": temps, "fans": fans, "note": note})

@app.route("/api/mem/drop", methods=["POST"])
def api_mem_drop():
    import subprocess
    try:
        subprocess.run(["sync"], check=False, timeout=10)
        p = os.environ.get("PROCFS_PATH", "/proc")
        with open(os.path.join(p, "sys", "vm", "drop_caches"), "w") as f:
            f.write("3")
        vm = psutil.virtual_memory()
        return jsonify({"ok": True, "available": vm.available, "percent": vm.percent})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
@app.route("/api/disk")
def api_disk():
    parts = []
    dfm = _df_host()
    for mp, hp in HOST_MNTS:
        if mp in dfm:
            parts.append(dfm[mp])
            continue
        try:
            st = os.statvfs(hp)
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            used = total - free
            parts.append({"device": "?", "mount": mp, "fstype": "?",
                          "total": total, "used": used, "free": free,
                          "percent": round(used / total * 100, 1) if total else 0})
        except Exception:
            continue
    try:
        dio = psutil.disk_io_counters()
        dio_d = {"read": dio.read_bytes, "write": dio.write_bytes} if dio else {}
    except Exception:
        dio_d = {}
    # top io processes
    items = sorted(cache["procs"], key=lambda x: x["io_r"] + x["io_w"], reverse=True)[:60]
    return jsonify({"parts": parts, "io": dio_d, "hw": get_disk_hw(),
                    "items": items})

_disk_hw_static = {}

def _read(p, default=""):
    try:
        with open(p) as f:
            return f.read().strip()
    except Exception:
        return default

def _sys_block_base():
    for base in ("/host/sys/block", "/sys/block"):
        try:
            if os.path.isdir(base):
                return base
        except Exception:
            continue
    return "/host/sys/block"

def _live_sysfs(name):
    """实时 sysfs: 容量/转速/型号(覆盖静态快照中的漂移项)"""
    base = _sys_block_base()
    d = os.path.join(base, name)
    out = {}
    try:
        sectors = _read(os.path.join(d, "size"))
        if sectors.isdigit():
            out["cap_b"] = int(sectors) * 512
    except Exception:
        pass
    rot = _read(os.path.join(d, "queue/rotational"))
    if rot in ("0", "1"):
        out["rotational"] = (rot == "1")
    model = _read(os.path.join(d, "device/model"))
    if model:
        out["sys_model"] = model
    vendor = _read(os.path.join(d, "device/vendor"))
    if vendor:
        out["sys_vendor"] = vendor
    return out

def _live_mdstat():
    """实时 mdstat: 每组阵列状态/同步进度,键为 md 名"""
    st = {}
    for p in ("/host/proc/mdstat", "/proc/mdstat"):
        try:
            with open(p) as f:
                txt = f.read()
            break
        except Exception:
            txt = ""
    else:
        return st
    import re
    cur = None
    for line in txt.splitlines():
        m = re.match(r"^(md\d+)\s*:\s*(\w+)\s+(\w+)(.*)$", line)
        if m:
            cur = m.group(1)
            st[cur] = {"status": m.group(2), "level": m.group(3),
                       "devs": m.group(4).strip(), "sync": ""}
            continue
        if cur and ("recovery" in line or "resync" in line or "blocks" in line):
            s = line.strip()
            if "recovery" in line or "resync" in line:
                st[cur]["sync"] = s
            elif "blocks" in line and not st[cur].get("blocks"):
                st[cur]["blocks"] = s
    return st

def _live_perdisk_io():
    try:
        d = psutil.disk_io_counters(perdisk=True) or {}
    except Exception:
        return {}
    out = {}
    for k, v in d.items():
        try:
            out[k] = {"read": v.read_bytes, "write": v.write_bytes}
        except Exception:
            continue
    return out

def get_disk_hw():
    global _disk_hw_static
    if not _disk_hw_static:
        hj = {}
        for jp in ("hw_disk.json",
                   os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "hw_disk.json")):
            try:
                with open(jp) as f:
                    hj = json.load(f)
                if hj.get("disks"):
                    break
            except Exception:
                continue
        _disk_hw_static = hj or {"disks": [], "arrays": []}
    disks = [dict(x) for x in _disk_hw_static.get("disks", [])]
    arrays = [dict(x) for x in _disk_hw_static.get("arrays", [])]
    # 合并实时数据
    pio = _live_perdisk_io()
    for dk in disks:
        nm = dk.get("name", "")
        dk.update(_live_sysfs(nm))
        if nm in pio:
            dk["io_read"] = pio[nm]["read"]
            dk["io_write"] = pio[nm]["write"]
        # 转速文字
        if dk.get("rotational") is True and not dk.get("rpm"):
            dk["rpm"] = 5400
    md_live = _live_mdstat()
    for a in arrays:
        md = a.get("md", "")
        lv = md_live.get(md, {})
        if lv:
            a["live_status"] = lv.get("status", "")
            a["live_devs"] = lv.get("devs", "")
            a["live_sync"] = lv.get("sync", "")
        if md in pio:
            a["io_read"] = pio[md]["read"]
            a["io_write"] = pio[md]["write"]
    return {"disks": disks, "arrays": arrays,
            "extra": _disk_hw_static.get("extra", {}),
            "snapshot": _disk_hw_static.get("snapshot", ""),
            "note": _disk_hw_static.get("note", "")}

@app.route("/api/net")
def api_net():
    global net_prev, net_rate
    now = time.time()
    try:
        counters = _host_nic_counters()
    except Exception:
        counters = {}
    rates = []
    for nic, c in counters.items():
        if nic.startswith(("lo", "docker", "br-", "veth")):
            continue
        prev = net_prev.get(nic)
        if prev:
            dt = max(now - prev["t"], 0.5)
            up = (c.bytes_sent - prev["sent"]) / dt
            down = (c.bytes_recv - prev["recv"]) / dt
        else:
            up = down = 0
        net_prev[nic] = {"sent": c.bytes_sent, "recv": c.bytes_recv, "t": now}
        rates.append({"nic": nic, "up": max(up, 0), "down": max(down, 0),
                      "sent": c.bytes_sent, "recv": c.bytes_recv})
    rates.sort(key=lambda x: x["up"] + x["down"], reverse=True)
    try:
        _hss = _host_ss()
    except Exception:
        _hss = None
    if _hss is not None:
        _h_tcp, _h_listen = _hss
        conns = len(_h_tcp)
    else:
        _h_tcp, _h_listen = None, None
        try:
            conns = len(psutil.net_connections())
        except Exception:
            conns = 0
    total_recv = 0
    total_sent = 0
    for nic, c in counters.items():
        if nic.startswith(("lo", "docker", "br-", "veth")):
            continue
        total_recv += getattr(c, "bytes_recv", 0) or 0
        total_sent += getattr(c, "bytes_sent", 0) or 0
    items = sorted(cache["procs"], key=lambda x: x["conns"], reverse=True)[:60]
    tcp_list = []
    listen_list = []
    if _h_tcp is not None:
        name_by_pid = {p["pid"]: p["name"] for p in cache.get("procs", [])}
        for c in _h_tcp:
            if not c.get("name"):
                c["name"] = name_by_pid.get(c.get("pid"), "")
        tcp_list, listen_list = _h_tcp[:80], (_h_listen or [])[:80]
        for c in listen_list:
            if not c.get("name"):
                c["name"] = name_by_pid.get(c.get("pid"), "")
    else:
        try:
            name_by_pid = {p["pid"]: p["name"] for p in cache.get("procs", [])}
            for c in psutil.net_connections(kind="tcp"):
                laddr = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else ""
                raddr = f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else ""
                family = "ipv6" if ":" in (c.laddr.ip if c.laddr else "") and "." not in (c.laddr.ip if c.laddr else "") else "ipv4"
                # better family detection
                import socket
                family = "ipv6" if (c.family == socket.AF_INET6) else "ipv4"
                if c.status == "LISTEN":
                    listen_list.append({"name": name_by_pid.get(c.pid, ""), "laddr": c.laddr.ip if c.laddr else "", "port": c.laddr.port if c.laddr else 0, "family": family, "pid": c.pid, "proto": "tcp"})
                else:
                    tcp_list.append({"name": name_by_pid.get(c.pid, ""), "laddr": laddr, "raddr": raddr, "family": family, "pid": c.pid, "status": c.status})
        except Exception:
            pass
    tcp_list = tcp_list[:80]
    listen_list = listen_list[:80]
    return jsonify({"nics": rates, "conns": conns, "items": items,
                    "hw": get_net_hw(),
                    "total_recv": total_recv, "total_sent": total_sent,
                    "tcp": tcp_list, "listen": listen_list})

_net_hw_static = {}

def _net_sysfs(name):
    """实时 sysfs: 状态/MTU/速率/MAC,键名与 psutil 速率互补"""
    out = {}
    for base in ("/host/sys/class/net", "/sys/class/net"):
        d = os.path.join(base, name)
        try:
            if not os.path.isdir(d):
                continue
            for k in ("address", "operstate", "mtu", "speed", "duplex",
                      "carrier"):
                v = _read(os.path.join(d, k))
                if v:
                    out[k] = v
            # 驱动名
            try:
                drv = os.path.basename(os.readlink(os.path.join(d, "device/driver")))
                if drv:
                    out["live_driver"] = drv
            except Exception:
                pass
            if out:
                break
        except Exception:
            continue
    return out

def _net_addrs():
    try:
        ad = psutil.net_if_addrs() or {}
    except Exception:
        return {}
    out = {}
    for nic, lst in ad.items():
        v4, v6, mac = [], [], ""
        for a in lst:
            try:
                fam = a.family.name if hasattr(a.family, "name") else str(a.family)
            except Exception:
                fam = ""
            addr = a.address or ""
            if "PACKET" in fam:
                if addr:
                    mac = mac or addr
            elif "AF_INET6" in fam:
                if addr and not addr.startswith("fe80"):
                    v6.append(addr.split("%")[0])
                elif addr:
                    v6.append(addr.split("%")[0] + "(链路)")
            elif "AF_INET" in fam:
                if addr and len(addr.split(".")) == 4:
                    v4.append(addr)
        out[nic] = {"v4": v4, "v6": v6, "mac": mac}
    return out

def get_net_hw():
    global _net_hw_static
    if not _net_hw_static:
        hj = {}
        for jp in ("hw_net.json",
                   os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "hw_net.json")):
            try:
                with open(jp) as f:
                    hj = json.load(f)
                if hj.get("nics"):
                    break
            except Exception:
                continue
        _net_hw_static = hj or {"nics": []}
    statics = {x.get("name"): dict(x) for x in _net_hw_static.get("nics", [])}
    try:
        st = psutil.net_if_stats() or {}
    except Exception:
        st = {}
    addrs = _parse_ip_addr(_nsenter_run(["ip", "-o", "addr", "show"]))
    if not addrs:
        addrs = _net_addrs()
    # 展示的网卡: 静态档案 + 实际有流量的非虚拟口
    names = list(statics.keys())
    try:
        counters = _host_nic_counters()
    except Exception:
        counters = {}
    for nic in counters:
        if nic.startswith(("lo", "docker", "br-", "veth")):
            continue
        if nic not in statics:
            names.append(nic)
    out = []
    for nic in names:
        base = dict(statics.get(nic, {"name": nic}))
        s = st.get(nic)
        if s is not None:
            try:
                base["isup"] = bool(s.isup)
                base["live_speed"] = s.speed or 0
                base["live_mtu"] = s.mtu or 0
            except Exception:
                pass
        base.update(_net_sysfs(nic))
        if base.get("isup") is None and base.get("operstate"):
            base["isup"] = (str(base.get("operstate")).strip().lower() == "up")
        a = addrs.get(nic, {})
        if a:
            base["ipv4"] = a.get("v4", [])
            base["ipv6"] = a.get("v6", [])
            if a.get("mac") and not base.get("mac"):
                base["mac"] = a["mac"]
        c = counters.get(nic)
        if c is not None:
            try:
                base["io_sent"] = c.bytes_sent
                base["io_recv"] = c.bytes_recv
            except Exception:
                pass
        out.append(base)
    # 默认网关/DNS 实时读
    gw, dns = _net_hw_static.get("gateway", ""), _net_hw_static.get("dns", "")
    base_if = ""
    try:
        with open("/host/proc/net/route" if os.path.exists("/host/proc/net/route") else "/proc/net/route") as f:
            for line in f.read().splitlines()[1:]:
                f_ = line.split()
                if len(f_) > 2 and f_[1] == "00000000" and f_[7] == "00000000":
                    import socket, struct
                    gw = socket.inet_ntoa(struct.pack("<L", int(f_[2], 16)))
                    base_if = f_[0]
                    break
    except Exception:
        pass
    try:
        rp = "/host/etc/resolv.conf" if os.path.exists("/host/etc/resolv.conf") else "/etc/resolv.conf"
        with open(rp) as f:
            for line in f:
                if line.startswith("nameserver"):
                    dns = line.split()[1]
                    break
    except Exception:
        pass
    return {"nics": out, "gateway": gw, "dns": dns,
            "egress": base_if,
            "snapshot": _net_hw_static.get("snapshot", ""),
            "note": _net_hw_static.get("note", "")}

@app.route("/api/kill", methods=["POST"])
def api_kill():
    d = request.get_json(force=True, silent=True) or {}
    try:
        pid = int(d.get("pid", 0))
    except Exception:
        return jsonify({"ok": False, "error": "pid无效"}), 400
    if pid <= 1:
        return jsonify({"ok": False, "error": "不能结束该进程"}), 400
    try:
        os.kill(pid, signal.SIGTERM)
        return jsonify({"ok": True})
    except ProcessLookupError:
        return jsonify({"ok": False, "error": "进程已不存在"}), 404
    except PermissionError:
        return jsonify({"ok": False, "error": "无权限"}), 403
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/health")
def health():
    return jsonify({"ok": True})

@app.route("/api/license-config")
def license_config():
    return jsonify({
        "version": HUB_VERSION,
        "latest": HUB.get("latest") or "",
        "update_available": bool(HUB.get("update_available")),
        "update_required": bool(HUB.get("update_required")),
        "download_url": HUB.get("download_url") or "",
        "changelog": HUB.get("changelog") or "",
        "announcement": (HUB.get("announcements") or [None])[0],
        "announcements": HUB.get("announcements") or [],
        "popup": HUB.get("popup") or [],
        "hub_ok": bool(HUB.get("ok")),
    })

if __name__ == "__main__":
    # 飞牛桌面 iframe 网关按 ui/config 的 gatewaySocket 走 Unix socket，
    # 应用只监听 TCP 时网关会返回 502 Bad Gateway，这里补一个 socket 中继。
    try:
        import sys as _sys
        import gateway_bridge
        _sock = gateway_bridge.start(os.environ.get("TRIM_APPDEST", ""),
                                     os.environ.get("FNNAS_GATEWAY_SOCKET", ""), PORT)
        if _sock:
            _sys.stderr.write("desktop gateway socket: %s\n" % _sock)
    except Exception as _gw_err:
        import sys as _sys2
        _sys2.stderr.write("gateway bridge 未启用: %s\n" % _gw_err)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
