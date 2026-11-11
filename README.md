# fnos-resmon 飞牛资源监控 · fnOS 原生 fpk 重打包

把原 **docker 版**的飞牛资源监控（fnos-resmon）重打包成 **fnOS 原生应用（fpk）**：
以应用身份直接运行 Python 进程，不依赖 Docker，离线安装，并修复了桌面内嵌打开时的
**502 Bad Gateway**。

> **本仓库不是原作者发布的官方版本。**
> 应用代码版权归原作者 **jinghui** 所有，原始仓库：
> `https://gitea.88531.cn/jinghui/fnos-resmon`（tag `v1.3.22`）。
> 本仓库仅做「本地化重打包 + 桌面网关适配」，未改动业务逻辑。

---

## 1. 功能

- CPU / 内存 / 磁盘 SMART / 网络吞吐 / 温度 / 进程占用的实时监控
- 历史趋势、性能模式开关、登录密码防护
- 原生 fpk 形态：应用中心安装、桌面图标启动、随系统后台自启

## 2. 相对官方 docker 版的改动

| 项 | 原 docker 版 | 本 fpk 重打包版 |
| --- | --- | --- |
| 运行方式 | Docker 容器 | fnOS 应用进程（`run-as: root`） |
| 依赖获取 | 容器镜像内 | 随包内置（Flask 全家桶 + psutil abi3 wheel），安装全程离线 |
| 宿主指标 | `-v /proc:/host/proc` 等挂载 | 应用身份直接读宿主 `/proc`、`/sys` |
| 数据目录 | 容器卷 | 应用数据目录 `$TRIM_PKGVAR`（升级不丢） |
| 桌面内嵌 | 端口直连 | 补 `app.sock` Unix socket 中继 + `gatewayPrefix` 适配，修复 502 |

Bad Gateway 的根因与修复细节见 [docs/bad-gateway-fix.md](docs/bad-gateway-fix.md)。

## 3. 目录结构

```
src/                     打包进 app.tgz 的内容（应用源码根）
  app.py                 主应用（Flask，端口 7031）
  gateway_bridge.py      桌面网关所需的 Unix socket 中继
  gateway.py             原项目遗留网关脚本（未启用，保留原样）
  public/index.html      前端入口
  ui/                    桌面图标与 ui/config（gatewaySocket / gatewayPrefix）
  py/wheels/             psutil manylinux abi3 wheel，安装时离线解压
packaging/               fpk 骨架：cmd 生命周期脚本、config、manifest 模板、LICENSE
assets/                  ICON.PNG / ICON_256.PNG
build/build_fpk.py       一键构建：组装 -> app.tgz -> fpk -> 自校验
requirements.txt         纯 Python 依赖（构建期拉取到 build/vendor）
```

`build/app/`、`build/vendor/`、`build/fpkroot/`、`dist/` 均为构建产物，已被 `.gitignore` 排除。

## 4. 构建

前置：Python 3.9+（构建机可为 Windows / macOS / Linux），能联网安装依赖。

```bash
# 1) 拉取目标平台依赖（manylinux2014_x86_64 / cp311）并打出 fpk
python build/build_fpk.py --fetch

# 2) 后续增量构建（复用 build/vendor）
python build/build_fpk.py

# 仅打源码包（不含 Flask 依赖，需 NAS 侧自备 Python 依赖）
python build/build_fpk.py --no-vendor
```

产物：`dist/fnos-resmon_1.3.22_x86_64.fpk`

脚本结尾会自校验：`manifest` 里的 `checksum` 与 `app.tgz` 实际 md5 是否一致、
`app.tgz` 内 `app.py` / `gateway_bridge.py` / `public/index.html` / `ui/config` /
vendor / psutil wheel 是否齐全。校验不过会以退出码 2 结束。

> 构建机若不是 NAS 本机，**必须**用 `--fetch`（脚本会显式指定 `--platform`
> 拉 linux wheel），否则会把本机平台的二进制依赖打进包，NAS 上加载失败。

## 5. 安装

**直接下载**：[Releases → v1.3.22 → `fnos-resmon_1.3.22_x86_64.fpk`](https://github.com/liangpangzhi/fnos-resmon/releases/download/v1.3.22/fnos-resmon_1.3.22_x86_64.fpk)

1. fnOS 桌面 → 应用中心 → 手动安装 → 选择 `fnos-resmon_1.3.22_x86_64.fpk`
2. 安装完成后桌面出现「飞牛资源监控」图标
3. 已在旧版使用过的机器：`/root/.fnos_resmon_hub_key`、`/root/.fnos_resmon_password`、
   `/root/.fnos_resmon_perf.json` 会被自动复用，原密码与授权直接生效

运行日志：应用数据目录下 `info.log`（`$TRIM_PKGVAR/info.log`）。

## 6. 生命周期脚本

| 脚本 | 作用 |
| --- | --- |
| `cmd/install_init` | 检查 python3 与 `ip` / `ss` / `lscpu` / `smartctl` 等可选工具 |
| `cmd/install_callback` | 链接桌面图标目录 `/var/apps/<app>/ui` |
| `cmd/main` | start / stop / restart / status，解压 psutil wheel，30 次轮询等待端口就绪 |
| `cmd/upgrade_callback` | 保留运行数据，仅清理旧解压依赖 |
| `cmd/uninstall_init` | 停进程 |

## 7. 免责声明

- 本项目为个人本地化重打包，与飞牛官方及原作者无隶属关系，不提供任何担保。
- 应用以 `run-as: root` 运行并读取宿主 `/proc`、`/sys`（与原 docker 版 `--privileged`
  + host 挂载等价），请自行评估风险后再安装。

第三方依赖许可：Flask / Werkzeug / Jinja2 / MarkupSafe / itsdangerous / click /
blinker（BSD-3-Clause），psutil（BSD-3-Clause）。
