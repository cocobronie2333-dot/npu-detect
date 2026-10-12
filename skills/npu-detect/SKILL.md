---
name: npu-detect
description: 获取 NPU Detect 应用，根据用户提供的 SSH 机器信息生成本机配置，启动后端并打开支持分类筛选和占用进程详情的监控页面。
---

# NPU Detect

仓库：https://github.com/cocobronie2333-dot/npu-detect

## 获取应用

在用户指定的应用目录执行 `git clone https://github.com/cocobronie2333-dot/npu-detect.git`。已有同仓库 checkout 时复用，仅在工作区干净时 `git pull --ff-only`，不覆盖用户修改。

需要 Git、Python 3.10+，远端需要 `npu-smi`。在应用目录创建 `.venv`，使用虚拟环境 Python 执行 `-m pip install -r requirements.txt`，不依赖开发者机器的路径。

## 生成机器配置

读取本次任务用户提供的 SSH 信息或用户指定的凭据文件。遵守已有的凭据来源和生成文件位置约定。缺少必要字段时询问，不猜测密码或类别。

当前应用只支持 IPv4、默认 SSH 端口 22、用户名和密码。收到私钥或自定义端口配置时说明当前限制，不静默忽略。

将配置保存为仓库外的 UTF-8 JSON；用 JSON 序列化器写入，最多 64 台机器：

```json
{"hosts": [{"ip": "192.0.2.10", "username": "example", "password": "user-provided-password", "category": "A5"}]}
```

类别可为 A3、A5 或不超过 40 字符的自定义名称，缺省归入“未分类”。不要覆盖已有凭据文件，除非用户要求更新；可选择新文件。真实配置不得写入技能、提交到 Git 或打印到日志和回复中。

## 启动和验证

设置服务进程环境变量 `NPU_CONFIG_FILE` 为上述配置绝对路径，用虚拟环境 Python 启动 `npu_card_monitor.py`，工作目录为应用目录。服务 URL 为 `http://127.0.0.1:8765/`。

启动前检查端口。已有本应用服务且配置一致时复用；加载新配置或代码时，仅重启已确认属于本应用的进程。其他服务占用端口时报告冲突，不终止它。

Windows 用 `Start-Process -WindowStyle Hidden` 启动，传入带正确引号的脚本路径，并通过 `$env:NPU_CONFIG_FILE` 让子进程继承配置路径。其他系统使用适当的后台进程机制并记录进程标识。需要日志时放在仓库外的本机临时目录。

有限次数轮询页面 HTTP 200；检查 `/api/config` 返回的机器数量和类别，不输出完整响应（含密码）。确认成功后用浏览器工具打开 URL，报告应用位置、配置路径和服务状态。

用户类别下拉框统一筛选机器列表和卡状态；检测只针对选中类别。请求查询或验证状态时可调用 `/api/scan` 执行只读 SSH 检测，仅请求启动应用时不必自动扫描。

总览上条是 HBM 空闲比例，下条是计算利用率。详情含进程、PID、用户、显存和运行时长，以及可展开的命令、容器信息。进程退出或权限不足时字段可能未知。启动命令可能含敏感参数，不对外发布原始查询结果。
