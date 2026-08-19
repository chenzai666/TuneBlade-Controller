# TuneBlade Controller

Windows 托盘小工具：用 **Fn+PgUp / Fn+PgDown** 调节 TuneBlade 里指定 AirPlay 设备的音量（默认步进 5%）。

## 功能

- 托盘常驻 / 开机自启
- Master **ON** 时接管快捷键；**OFF** 时放行系统音量与翻页
- 托盘菜单 **选择设备**（不写死设备名，适配不同电脑）
- 调节时屏幕下方弹出音量条（设备名 + 进度 + 百分比）
- 静音：`Ctrl+Alt+M`；退出：`Ctrl+Alt+Q` 或托盘菜单

## 使用

1. 安装并打开 [TuneBlade](https://tuneblade.com/)
2. 运行 `TuneBladeController.exe`（见 Releases）
3. 托盘右键 → **选择设备** → 勾选你的接收器
4. 确保 Master 为 ON（托盘图标为绿色）后使用快捷键

## 配置 `config.json`（与 exe 同目录）

```json
{
  "device_name": "",
  "volume_step": 5,
  "window_title": "TuneBlade",
  "poll_interval_sec": 1.0,
  "autostart": false,
  "debug_log": false
}
```

- `device_name` 为空：自动选第一个检测到的设备，并写回配置  
- `debug_log`: `true` 时才写 `TuneBladeController.log`

## 开发 / 打包

```bat
python -m venv venv
venv\Scripts\pip install -r requirements.txt pyinstaller
venv\Scripts\pyinstaller --noconfirm build_exe.spec
```

产物：`dist\TuneBladeController.exe`

## License

MIT
