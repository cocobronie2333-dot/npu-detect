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

Windows 创建 `.venv` 并安装依赖后，也可使用 `start_npu_monitor.bat`。

## SSH 配置

参考 `npu-hosts.example.json` 填写配置，在页面点击“导入 JSON”。也可在项目目录创建 `npu-hosts.json`，启动后由后端加载。

每台机器可设置 `category` 字段，例如 `"A3"` 或 `"A5"`，也支持自定义类别；省略或留空时归入“未分类”。页面顶部的类别下拉框统一筛选机器列表与卡状态，检测和刷新仅针对当前类别，选择“全部类别”时检测所有机器。类别也可在机器列表中编辑。

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

可设置 `NPU_CONFIG_FILE` 环境变量为仓库外的配置文件绝对路径。附带技能：[npu-detect](skills/npu-detect/SKILL.md)，用于获取应用、生成本机配置和启动服务。
