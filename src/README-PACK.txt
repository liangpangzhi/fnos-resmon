fnos-resmon 1.3.22 飞牛资源监控（fnOS 原生 fpk 版）

代码来源：https://gitee.88531.cn 镜像 / 原仓库 https://gitea.88531.cn/jinghui/fnos-resmon （作者 jinghui）
本 fpk 未修改业务代码，仅做本地化打包：
  1. 内置 flask / werkzeug / jinja2 / markupsafe / itsdangerous / click / blinker 与 psutil(abi3 wheel)，安装时离线解压，不联网、不依赖 docker；
  2. 以 fnOS 应用身份（run-as root）直接读取宿主 /proc、/sys，等价于原容器的 host 网络/特权模式；
  3. 运行数据全部位于应用数据目录（升级不丢）：hub_key / login_password / perf_mode.json。
