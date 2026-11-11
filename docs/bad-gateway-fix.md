# 桌面内嵌打开 502 Bad Gateway：根因与修复

## 现象

fpk 安装成功、应用中心显示「运行中」，但点桌面图标打开面板时页面返回
**502 Bad Gateway**；直接访问 `http://<NAS>:7031` 则完全正常。

## 根因

fnOS 桌面用 iframe 内嵌应用，转发链路不走 TCP 端口，而是按应用 `ui/config` 里声明的

```json
{
  "type": "iframe",
  "url": "/app/fnos-resmon",
  "gatewaySocket": "app.sock",
  "gatewayPrefix": "/app/fnos-resmon"
}
```

走应用目录下的 **Unix socket**：网关连接 `$TRIM_APPDEST/app.sock`，把 HTTP 请求代理过去。

而原 `app.py` 只调用 `app.run(host, port)` 监听 TCP 7031，**从未创建 `app.sock`**。
网关连不上 socket，于是返回 502。

## 修复（三处，均不改业务逻辑）

### 1. 新增 `gateway_bridge.py` — Unix socket 中继

在 `$TRIM_APPDEST/app.sock` 上监听，把收到的字节流转发给 `127.0.0.1:7031`，
对两端都使用线程化 I/O，不引入额外依赖（仅标准库 `socket` + `threading`）。

`app.py` 启动时按需拉起：

```python
import gateway_bridge
_sock = gateway_bridge.start(os.environ.get("TRIM_APPDEST", ""),
                             int(os.environ.get("PORT", "7031")))
```

socket 文件不存在、权限不足等异常均被吞掉并写日志，**不影响 TCP 端口直连**。

### 2. `app.py` 增加 WSGI 前缀剥离

桌面网关转发时会带上前缀 `/app/fnos-resmon`，若上游未剥离，Flask 路由全部 404。
新增 `_GatewayPrefixStripper` 中间件：命中前缀时改写 `PATH_INFO` / `SCRIPT_NAME`，
使「带前缀」与「不带前缀」两种转发方式都能路由到同一处理函数。

前缀来源：`FNNAS_GATEWAY_PREFIX` 环境变量，缺省 `/app/<TRIM_APPNAME>`。

### 3. `public/index.html` 注入 fetch 包装

前端所有请求都是相对路径，在带前缀的内嵌场景下会拼错 base。注入一段包装：
请求 URL 若不以 `/app/<app>` 开头，自动补上当前页面所在前缀，保证 API 命中。

## 验证

1. 应用中心安装 fpk → 状态「运行中」
2. `ls -l $TRIM_APPDEST/app.sock` 存在
3. 点桌面图标 → 面板正常渲染，CPU / 内存 / 磁盘曲线有数据
4. 浏览器直连 `http://<NAS>:7031` → 同样正常（两条链路并存）

## 相关文件

- `src/gateway_bridge.py`
- `src/app.py`（类 `_GatewayPrefixStripper`、`gateway_bridge` 启动段）
- `src/public/index.html`（fetch 前缀包装）
- `src/ui/config`（`gatewaySocket` / `gatewayPrefix`）
