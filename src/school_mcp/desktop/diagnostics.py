"""Offline packaged-runtime check; no account, API call, or school navigation."""
import asyncio
from .chat import skill_text
from ..agent.catalog import WORKFLOWS, describe


def check():
    from playwright.sync_api import sync_playwright
    from ..browser import chrome_browser
    result = {'network_checked': False, 'school_data_read': False, 'headed': False}
    try:
        async def registry():
            return {flow: len((await describe(flow))['tools']) for flow in WORKFLOWS}
        result['workflow_tools'] = asyncio.run(registry())
        assert skill_text()
        for flow in WORKFLOWS:
            assert skill_text(flow)
        result['skills_available'] = True
        with sync_playwright() as p, chrome_browser(p, headless=True) as browser:
            page = browser.new_page()
            page.set_content('<title>school-offline-check</title><p>offline</p>')
            assert page.title() == 'school-offline-check'
        result.update(ready=True, chrome_headless=True)
    except Exception:
        result.update(ready=False, detail='本机环境检查未通过，请确认已安装 Chrome，且完整解压了应用文件夹。')
    return result
