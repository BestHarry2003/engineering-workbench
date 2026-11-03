# 工作台 · Windows 桌面版

面向本地工程计算的统一入口：Abaqus、ANSYS Fluent、PyroSim、SSH 工作站和 CMD/BAT 脚本工作台。

## 使用

1. 下载 Release 中的 `Workbench.exe`，双击启动。
2. 适用于 Windows 10/11 x64。已内置 Python 和 SSH 依赖，无需安装 Python。
3. 界面需要 Microsoft Edge WebView2 Runtime；若未安装，从微软官网下载并安装：
   https://developer.microsoft.com/microsoft-edge/webview2/
4. Abaqus、Fluent、PyroSim、FDS 等专业软件及许可证需自行安装、配置；本程序不包含求解器。

首次启动是空白工作台，不包含任何人的模型索引、备注、工作站、密码、脚本入口或本机设置。
桌面版不会自动读取旧源码目录中的数据库。需要原有 Abaqus 索引时，请在旧版导出元数据，再在桌面版“设置与迁移”中导入；Fluent/PyroSim 索引、工作站和脚本需自行配置。

## 数据与运行行为

- 数据存放于 `%LOCALAPPDATA%\EngineeringWorkbench`，与 EXE 分离。覆盖升级 EXE 不会清空数据。
- 后端仅监听 `127.0.0.1` 的自动分配端口，不占用旧版的 8765，也不向局域网开放。
- 同一用户数据目录只能运行一个桌面实例。
- 关闭窗口会退出工作台本机服务。已启动脚本、专业软件和远程求解不会被自动终止；远程监控和回传需要工作台运行。
- SSH 密码使用 Windows 当前用户加密，不能直接搬到其他电脑使用。
- 日志在数据目录的 `logs/desktop.log`。分享日志或导出内容前，请检查其中的个人路径与模型备注。
- 桌面版“打包迁移”导出 EXE 和当前 Abaqus 元数据；不包含专业模型文件、其他工作台数据或 SSH 凭据。

## 发布隐私

构建使用明确文件白名单，排除所有运行数据、数据库、配置、备份、日志、导出文件、截图、Git 历史、SSH 凭据和私钥。
发布仓库仅使用经过检查的源码快照，不上传开发目录的历史提交。
工作站输入框使用通用示例，不包含真实网络地址。

## 从源码构建

在 Windows x64 / Python 3.12 的独立虚拟环境中：

```powershell
python -m pip install -r requirements-desktop.txt
python build_release.py --stage C:\Temp\workbench-clean-source --output C:\Temp\workbench-release
```

构建目录必须是新的空目录。输出 `Workbench.exe`、干净源码 ZIP、第三方许可与文件哈希。
不应直接用 PyInstaller 打包整个开发目录。`dist/` 是网页资源，不是 EXE 输出目录。

打包后可执行隔离验证：

```powershell
.\Workbench.exe --self-test C:\Temp\workbench-test.json
```

该测试使用临时空数据库，验证 API、备份、依赖和真实 WebView 窗口，不启动求解器、不连接实际工作站。

## 第三方组件

桌面窗口使用 pywebview / Microsoft WebView2，EXE 使用 PyInstaller 打包，SSH 使用 Paramiko。
随包提供 `THIRD_PARTY_NOTICES.txt`。WebView2 Runtime 由微软单独分发。
本发布版尚未进行 Windows 代码签名。
