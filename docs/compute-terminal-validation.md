# 107 终端与第七章验收

2026-10-23，接续已确认的85秒六章版。新增20秒官方Shell实录，成片105秒，脚本见 [终端镜头表](compute-terminal-film-script.md)。

## 功能

`school_compute_terminal_inspect(job_id=None)` 已接入桌面对话和MCP目录。固定执行节点、身份及Slurm查询；省略编号查询本人队列，指定编号查询最近31天已在本人网页记录中核实的作业。返回实际命令、输出、退出码、终端链接和 `job_found`。工具不接收任意命令字符串；用户可在应用内官方Shell手动操作。

命令组使用15秒GNU timeout和2秒强制退出宽限；读取结束标记后才给出退出状态。终端账号、官方主机、集群路径和作业归属均有检查。超时或没有结束标记保留未知状态，不自动重发。工具和录制均在finally退出本次Shell并关闭浏览器。

## 实际结果

- 原生Tauri/WebView2从“登录集群”打开Shell，新标签继承本人登录态；界面错误0项。
- 录制逐键输入 `hostname && id -un`，再执行 `sacct -X -S 2026-10-01 -j 86961 -o JobID,State,Elapsed,AllocCPUS,AllocTRES%40`。
- 作业86961：`COMPLETED`、`00:05:24`、2核CPU、1块GPU；这是本人10月7日已有记录。
- 作业110511存在于网页，但本次终端记账没有返回。功能将其区分为命令退出成功、记录未找到，并提示使用网页历史查询。
- 发布包中通过实际对话调用 `school_compute_terminal_inspect`，返回86961原始结果和退出码0；完整终端链接已单独核对。

## 工程与视频检查

固定命令参数、官方地址边界、命令回显与结束标记区分、连接前拒绝无效编号、已有作业接口、目录与对话资源等14项检查通过。独立agent/compute MCP自检与Chrome检查通过。全局MCP禁用设置保留，当前Codex连接未重新加载。

发布包 `dist/USTC-School-Agent-Native-Windows.zip` 已核对ZIP、源码指纹、二进制和资源一致性，排除私人目录。SHA-256：`3d4e61ac70adf3f5006ae4c7c0909cabb58e25836f77b38a68015a433c7e09b0`。隔离MCP目标指纹：`76a757b184b5d5976e7801a42988a9fa8558264eee57b1b3d43bdae286ef439e`。

20秒单段1200帧、105秒完整版6300帧，均为1920×1080／60fps、H.264 Main／yuv420p、AAC 48kHz、MP4 faststart；全流解码与Windows媒体解码通过。检查了镜头交界、点击目标、连续命令输入和完整返回。第六章结尾仅处理进入第七章的退镜与遮罩，前六章内容保留。

私有素材与证据在 `.local/video-shot7-terminal/`。交付目录为桌面 `SchoolAgent_107终端展示`；所有本轮OwnedProcess任务已关闭，登记中的活动子进程为0。
