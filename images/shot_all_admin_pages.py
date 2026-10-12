"""Screenshot every SPA page an admin can reach.

Logs in once, then walks a fixed list of routes, screenshotting each.
Full-page captures so the result includes scrolled content.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

OUT_DIR = Path(__file__).resolve().parent / "admin_pages"
OUT_DIR.mkdir(exist_ok=True)

LOGIN_URL = "http://localhost:8080/login"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "Demo123!"

# (route, output filename, optional wait selector)
# The wait selectors are the most-stable testids we picked per page; we
# skip the wait for pages that don't have a single "the content is here"
# marker (e.g. an empty list).
PAGES = [
    # ----- general (auth-guarded) -----
    ("/inbox", "01_inbox.png", "inbox-page, [data-testid='inbox-list'], h1"),
    ("/inbox/01HZDEMO00000000000000004", "02_inbox_detail.png", "h1, [data-testid='inbox-detail']"),
    ("/kb", "03_kb.png", "h1"),
    ("/kb/01HZDEMO0000000000000000B", "04_kb_detail.png", "h1"),
    ("/settings", "05_settings.png", "h1"),
    # ----- admin-only -----
    ("/admin/dashboard", "10_admin_dashboard.png", "h1"),
    ("/admin/sla-policies", "11_admin_sla_policies.png", "h1"),
    ("/admin/budget", "12_admin_budget.png", "h1"),
    ("/admin/tickets", "13_admin_tickets.png", "h1"),
    ("/admin/tickets/01HZDEMOT0P0OUTAGE0TICKET", "14_admin_ticket_detail.png", "h1"),
    ("/admin/kb-drafts", "15_admin_kb_drafts.png", "h1"),
    ("/admin/llm-chain", "16_admin_llm_chain.png", "h1"),
    ("/admin/llm-configs", "17_admin_llm_configs.png", "h1"),
    ("/admin/users", "18_admin_users.png", "h1"),
    ("/admin/tenant-settings", "19_admin_tenant_settings.png", "assignment-policy-card"),
    ("/admin/widget-config", "20_admin_widget_config.png", "h1"),
    ("/admin/search", "21_admin_search.png", "h1"),
]


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        page = await ctx.new_page()

        # --- 1. Log in once ---
        await page.goto(LOGIN_URL, wait_until="networkidle")
        await page.fill('input[type="email"]', ADMIN_EMAIL)
        await page.fill('input[type="password"]', ADMIN_PASSWORD)
        await page.wait_for_timeout(800)
        await page.get_by_role("button", name="登录").click()
        await page.wait_for_url("**/inbox", timeout=15_000)

        # --- 2. Walk every page ---
        results: list[tuple[str, str, bool, str]] = []
        for route, fname, wait_selector in PAGES:
            url = f"http://localhost:8080{route}"
            try:
                await page.goto(url, wait_until="networkidle", timeout=15_000)
            except Exception as e:
                results.append((route, fname, False, f"nav: {e}"))
                continue
            # Try to wait for any of the comma-separated selectors.
            ok = False
            for sel in wait_selector.split(","):
                sel = sel.strip()
                if not sel:
                    continue
                try:
                    await page.wait_for_selector(sel, timeout=4_000, state="visible")
                    ok = True
                    break
                except Exception:
                    continue
            # Even if none of the wait selectors resolved, give the page
            # a beat to settle (e.g. data fetching / skeleton → real).
            await page.wait_for_timeout(600)
            out_path = OUT_DIR / fname
            try:
                await page.screenshot(path=str(out_path), full_page=True)
                results.append((route, fname, ok, "ok" if ok else "screenshot-only"))
            except Exception as e:
                results.append((route, fname, ok, f"shot: {e}"))

        # --- 3. Report ---
        print()
        print("== Admin page screenshots ==")
        for route, fname, ok, note in results:
            mark = "OK" if ok else "??"
            print(f"  [{mark}]  {route:55}  ->  {fname}  ({note})")
        print()
        print(f"Files: {OUT_DIR}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
