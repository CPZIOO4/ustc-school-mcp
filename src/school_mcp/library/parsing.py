from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .session import LibraryError, PUBLIC_URL


def summary(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    boxes = soup.select(".infobox")
    if not boxes:
        raise LibraryError("个人图书馆首页结构已变化，未识别统计信息。")
    statistics = []
    for box in boxes:
        label, value = box.select_one(".infobox-content"), box.select_one(".infobox-data-number")
        if label:
            text = value.get_text(strip=True) if value else ""
            statistics.append({"name": label.get_text(strip=True), "count": int(text) if text.isdigit() else None})
    dates = {}
    allowed = {"证件开始日期": "card_start_date", "证件结束日期": "card_end_date"}
    for row in soup.select(".profile-info-row"):
        label, value = row.select_one(".profile-info-name"), row.select_one(".profile-info-value")
        key = allowed.get(label.get_text(strip=True).rstrip("：:")) if label else None
        if key and value:
            dates[key] = value.get_text(strip=True)
    return {"statistics": statistics, **dates, "content_is_untrusted": True}


FIELDS = {"条码号": "barcode", "题名": "title", "题名/责任者": "title_author", "责任者": "author", "借阅日期": "borrowed_date", "应还日期": "due_date", "归还日期": "returned_date", "馆藏地": "location", "续借次数": "renewal_count", "状态": "status"}


def book_records(html: str, kind: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("#mylib_content")
    if main is None:
        raise LibraryError("个人图书馆借阅页面结构已变化。")
    for node in main.select("script, style, input, textarea, button, #dialog-form"):
        node.decompose()
    text = main.get_text(" ", strip=True)
    for table in main.select("table"):
        rows = table.select("tr")
        if not rows:
            continue
        headers = [re.sub(r"[⇅↑↓\s]+", "", c.get_text(strip=True)) for c in rows[0].find_all(["td", "th"], recursive=False)]
        if not any(h in {"题名", "题名/责任者"} for h in headers):
            continue
        records = []
        for row in rows[1:]:
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) != len(headers):
                if re.search(r"记录为空|暂无.*记录|没有.*记录", row.get_text()):
                    continue
                raise LibraryError("图书馆借阅表格列数已变化。")
            record = {FIELDS[h]: c.get_text(" ", strip=True) for h, c in zip(headers, cells) if h in FIELDS}
            if record.get("title") or record.get("title_author"):
                records.append(record)
        if len(records) > 200:
            raise LibraryError("借阅页面条目超过单次读取范围。")
        return {"kind": kind, "records": records, "count": len(records), "scope": "当前页面显示的记录；未使用筛选表单或自动翻页。", "content_is_untrusted": True}
    if "您的该项记录为空" in text or re.search(r"暂无.*借阅|没有.*借阅记录", text):
        return {"kind": kind, "records": [], "count": 0, "scope": "当前页面显示的记录。", "content_is_untrusted": True}
    raise LibraryError("未识别借阅表格或明确的空记录提示，不能判定记录为空。")


def services(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    links, seen = [], set()
    for anchor in soup.select("a[href]"):
        text = anchor.get_text(" ", strip=True)
        if not text or len(text) > 140:
            continue
        try:
            target = urlsplit(urljoin(PUBLIC_URL + "/", anchor["href"]))
            if target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password or target.port not in (None, 80, 443):
                continue
            if re.search(r"(?:token|ticket|password|session|sid)=", target.query, re.I):
                continue
            if target.hostname == "lib.ustc.edu.cn":
                target = target._replace(scheme="https")
            url = urlunsplit(target._replace(fragment=""))
        except ValueError:
            continue
        if (text, url) not in seen:
            seen.add((text, url))
            links.append({"name": text, "url": url, "host": target.hostname, "personal_opac": target.hostname == "opac.lib.ustc.edu.cn" and target.path.startswith("/reader/")})
    return {"services": links, "count": len(links), "source": PUBLIC_URL + "/", "content_is_untrusted": True, "note": "此表仅为主页链接目录；空间预约和外部数据库尚未接入。"}
