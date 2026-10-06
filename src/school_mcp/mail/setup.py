from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk

from .config import MailConfig, MailError, load_config
from .onboarding import connect_and_save


def run(address: str = "") -> None:
    if not address:
        try:
            address = load_config().address
        except MailError:
            pass
    root = tk.Tk()
    root.title("School MCP · 中科大邮箱接入")
    root.geometry("640x470")
    root.minsize(580, 440)
    root.configure(bg="#f6f8fc")
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="连接中科大邮箱", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
    ttk.Label(frame, text="先在学校网页邮箱中人工登录并完成验证，再输入已有客户端专用密码。\n需要新建时由本人在 设置 → 安全设置 → 客户端专用密码 操作；本程序不会创建、删除或重置授权。\n密码只在本机输入，不要发送到对话。只读连接验证成功后才加密保存。", wraplength=580).pack(anchor="w", pady=(10, 16))
    account = tk.StringVar(value=address)
    password = tk.StringVar()
    status = tk.StringVar(value="填写客户端专用密码后点击连接。")
    ttk.Label(frame, text="完整邮箱地址").pack(anchor="w")
    account_entry = ttk.Entry(frame, textvariable=account)
    account_entry.pack(fill="x", pady=(4, 12))
    ttk.Label(frame, text="客户端专用密码").pack(anchor="w")
    secret_entry = ttk.Entry(frame, textvariable=password, show="●")
    secret_entry.pack(fill="x", pady=(4, 12))
    buttons = ttk.Frame(frame)
    buttons.pack(fill="x")
    ttk.Button(buttons, text="打开学校邮箱", command=lambda: webbrowser.open("https://mail.ustc.edu.cn/")).pack(side="left")
    messages: queue.Queue[tuple[bool, str]] = queue.Queue()
    busy = False

    def submit() -> None:
        nonlocal busy
        if busy:
            return
        try:
            config = MailConfig(account.get().strip())
            secret = "".join(password.get().split())
            if not secret:
                raise MailError("请填写客户端专用密码。")
        except MailError as exc:
            status.set(str(exc))
            return
        busy = True
        password.set("")
        connect.configure(state="disabled")
        account_entry.configure(state="disabled")
        secret_entry.configure(state="disabled")
        status.set("正在验证学校邮箱连接…")

        def worker() -> None:
            try:
                result = connect_and_save(config.address, secret)
                messages.put((True, f"接入成功：收件箱 {result['messages']} 封，未读 {result['unread']} 封。可以关闭此窗口。"))
            except MailError as exc:
                messages.put((False, str(exc)))
            except Exception:
                messages.put((False, "本地配置失败，请关闭窗口后重新打开。"))
        threading.Thread(target=worker, daemon=True).start()

    connect = ttk.Button(buttons, text="连接并保存", command=submit)
    connect.pack(side="right")
    ttk.Label(frame, textvariable=status, wraplength=550).pack(anchor="w", pady=(16, 0))

    def poll() -> None:
        nonlocal busy
        try:
            success, detail = messages.get_nowait()
        except queue.Empty:
            root.after(100, poll)
            return
        busy = False
        status.set(detail)
        connect.configure(state="normal")
        account_entry.configure(state="normal")
        secret_entry.configure(state="normal")
        if success:
            password.set("")
            connect.configure(text="更新配置")
        root.after(100, poll)

    root.bind("<Return>", lambda _: submit())
    root.after(100, poll)
    secret_entry.focus_set() if address else account_entry.focus_set()
    root.mainloop()
