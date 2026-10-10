# 给部署本项目的 AI

用户已在使用能执行本地命令、支持 stdio MCP 的 AI 工具。本文件是部署入口；不要求用户理解 Python、Git 或手写配置。不假定某个客户端的配置位置或格式，也不把 Workbody / Deepseek Harness 等名称当作已经完成兼容验收的证据。

1. 读取 [AI 部署流程](docs/ai-setup.md)，在这个文件所在目录执行。下载 ZIP 后无需初始化 Git。
2. 用 `python bootstrap.py check` 检查，再用 `python bootstrap.py install` 安装锁定依赖。命令失败先解决输出中的缺项。
3. 用 `python bootstrap.py verify` 验证全部九个 stdio 服务和后台 Chrome。此检查不访问学校、不读取邮件内容或学校业务数据、不发邮件。
4. 用 `python bootstrap.py config --service mail --service teach` 生成首批客户端配置。合并到用户当前 AI 工具的 MCP 设置，保留其他服务；只注册用户需要的模块。加载项目的 `.agents/skills/school-onboarding/SKILL.md`；业务操作另读 `school-services`。
5. 调用实际加载的 `school_mail_setup_guide`。用户选择绑定并创建一个客户端密码后，运行它返回的浏览器绑定命令，让用户在窗口中登录。密码不经过聊天、命令参数或 AI 的 stdin。
6. 用 `school_mail_bind_status` / `school_mail_check_connection` 核对。不要将配置生成、进程启动或历史状态当成接入成功。只有明确发送测试邮件的请求才发信。

更新已部署项目时，按 [更新核验流程](docs/runtime-updates.md) 执行。`verify` 只验证新建进程，之后须在实际客户端调用对应 `school_<站点>_runtime_status` 并传入自检目标指纹。服务重载保留私人目录与登录状态，不批量结束其他 Python 进程。

默认浏览器为 Playwright + 本机 Chrome，无头；首次邮箱绑定的 `--headed` 是用户参与登录的专用入口。已有可用邮箱不应为了演示而再生成密码。学校统一身份凭据与邮箱专用密码不同，按需要在本机录入，邮件验证码回退按用户授权单独启用。

本轮使用的私人目录必须贯穿安装、客户端配置、绑定与检查；默认项目 `.local/`，支持 `SCHOOL_MCP_LOCAL_DIR`。不上传或复制他人的 `.local/`；不能从聊天历史提取账户填进新用户配置。发布只包含源码。不要删除绑定记录来绕过“结果未知”或重复生成客户端密码。
