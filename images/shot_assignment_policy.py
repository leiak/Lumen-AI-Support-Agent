"""Screenshot the auto-assignment policy card on /admin/tenant-settings.

Logs in as admin@example.com, navigates to the tenant settings page,
waits for the policy card to render, then captures a viewport screenshot.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

OUT_DIR = Path(__file__).resolve().parent
LOGIN_URL = "http://localhost:8080/login"
SETTINGS_URL = "http://localhost:8080/admin/tenant-settings"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "Demo123!"


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        page = await ctx.new_page()

        # 1. Log in via the SPA.
        await page.goto(LOGIN_URL, wait_until="networkidle")
        await page.fill('input[type="email"]', ADMIN_EMAIL)
        await page.fill('input[type="password"]', ADMIN_PASSWORD)
        # Wait for the tenant lookup debounce + resolution.
        await page.wait_for_timeout(800)
        submit = page.get_by_role("button", name="登录")
        await submit.click()
        # Give the SPA a moment to navigate, then snapshot where we are.
        try:
            await page.wait_for_url("**/inbox", timeout=15_000)
        except Exception as e:
            dbg = OUT_DIR / "_debug_after_submit.png"
            await page.screenshot(path=str(dbg), full_page=True)
            print(f"Login nav failed: {e}\n  url={page.url}\n  shot={dbg}")
            raise

        # 2. Navigate to the tenant settings page.
        await page.goto(SETTINGS_URL, wait_until="networkidle")
        debug = OUT_DIR / "_debug_tenant_settings.png"
        await page.screenshot(path=str(debug), full_page=True)
        print(f"Debug screenshot: {debug}  url={page.url}")
        # 3. Wait for the auto-assignment policy card to render.
        card = page.get_by_test_id("assignment-policy-card")
        await card.wait_for(state="visible", timeout=10_000)
        # Also wait for the strategy select to have a real value (so the
        # screenshot doesn't catch a loading skeleton).
        select = page.get_by_test_id("assignment-policy-strategy")
        await select.wait_for(state="attached", timeout=5_000)
        await page.wait_for_timeout(400)  # let the hint text settle

        # 4. Full-page screenshot — captures the summary card, the
        # tenant-settings editor, and the new policy card.
        out = OUT_DIR / "admin_tenant_settings.png"
        await page.screenshot(path=str(out), full_page=True)
        print(f"Saved {out}")

        # 5. Also a tighter crop of just the policy card for the chat.
        crop = OUT_DIR / "admin_assignment_policy_card.png"
        await card.screenshot(path=str(crop))
        print(f"Saved {crop}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
