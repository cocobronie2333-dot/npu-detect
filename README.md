# NPU Detect

一个用于批量查看 SSH 机器 NPU 状态的全栈页面。前端采用 HTML、CSS 和 JavaScript，后端采用 Python HTTP 服务与 Paramiko，通过 SSH 执行 `npu-smi info`。

## 功能

- 一键导入 JSON 格式的 SSH 机器配置，也可手动添加或删除节点。
- 一键并发检测多台机器，展示 NPU 型号、健康状态、利用率及 HBM 使用情况。
- 汇总空闲卡情况，并分别展示连接或检测失败的节点。
- 最多配置 64 台机器，最多同时建立 8 个 SSH 检测任务。

## 启动

需要 Python 3.10 或以上版本；远端机器需支持 `npu-smi info`。

```sh
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# Linux / macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
python npu_card_monitor.py
```

浏览器打开 <http://127.0.0.1:8765/>，导入机器配置后点击检测。

Windows 也可使用 `start_npu_monitor.bat`。该脚本目前使用本机固定的 Python 和 Paramiko 路径，换机器时需调整其中的 `PYTHON_EXE` 与 `PARAMIKO_DIR`；上述命令行启动方式不依赖这些路径。

## SSH 配置

参考 `npu-hosts.example.json` 填写配置，在页面点击“导入 JSON”。也可在项目目录创建 `npu-hosts.json`，启动后由后端加载。

实际配置包含 SSH 密码，请仅保存在本机。`npu-hosts.json`、日志、环境文件和常见私钥文件已加入 `.gitignore`，示例文件仅包含占位内容。

服务绑定本机地址 `127.0.0.1`，当前支持 IPv4 地址、用户名和密码认证。空闲卡判定条件为健康状态正常、利用率不高于 5%，且剩余 HBM 比例不低于 20%。

## 项目结构

| 文件 | 用途 |
| --- | --- |
| `npu_host_config.html` | 配置导入、节点管理和检测结果页面 |
| `npu_card_monitor.py` | HTTP 接口、SSH 并发检测和 NPU 输出解析 |
| `start_npu_monitor.bat` | Windows 启动脚本 |
| `requirements.txt` | Python 依赖 |
| `npu-hosts.example.json` | 不含真实凭据的配置示例 |

接口：`GET /api/config` 加载本机配置，`POST /api/scan` 执行检测，`GET /api/results` 获取最近一次检测结果。
