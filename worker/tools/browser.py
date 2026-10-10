"""Browser tools - the agent's hands and eyes on web applications (Playwright).

The agent sees a page as compact text with numbered element refs (see
dom_snapshot.js) and acts by ref: ``browser_click(ref=12)``. After every
action that can change the page, the tool returns a *fresh snapshot*, so
"observe the result of each action" is built into every step rather than
left to the model's discipline.

Every action is also screenshotted to runs/<id>/screens/ for the live UI and
the evidence report.

Safety: before any navigation, form submission or click, the tool describes
the action to the Guard (worker/safety/policy.py), which can block it or ask
the human for approval.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ..config import Settings
from ..runtime.context import RunContext
from ..safety.policy import ProposedAction
from .base import ErrorKind, Tool, ToolError, ToolResult, schema

_SNAPSHOT_JS = (Path(__file__).parent / "dom_snapshot.js").read_text(encoding="utf-8")

# Inspects what clicking element `ref` will do: follow a link? submit a form (with which fields)?
_ACTION_INFO_JS = """
(ref) => {
  const el = document.querySelector(`[data-agent-ref="${ref}"]`);
  if (!el) return null;
  const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 80);
  const form = el.form || el.closest('form');
  const tag = el.tagName, type = (el.getAttribute('type') || '').toLowerCase();
  const isSubmitter = form && ((tag === 'BUTTON' && (type === '' || type === 'submit')) ||
                               (tag === 'INPUT' && (type === 'submit' || type === 'image')));
  const formInfo = f => {
    const fields = {};
    for (const [k, v] of new FormData(f).entries()) if (typeof v === 'string') fields[k] = v;
    f.querySelectorAll('input[type=password]').forEach(p => { if (p.name) fields[p.name] = '********'; });
    return { method: (f.getAttribute('method') || 'GET').toUpperCase(), action: f.action, fields,
             hasPassword: !!f.querySelector('input[type=password]') };
  };
  return { tag, type, text, href: tag === 'A' ? el.href : null,
           isField: tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT',
           submit: isSubmitter ? formInfo(form) : null,
           enterSubmits: (tag === 'INPUT' && form) ? formInfo(form) : null };
}
"""


class BrowserSession:
    """Owns the Playwright browser for one run (lazy start, one tab)."""

    def __init__(self, settings: Settings, run_dir: Path) -> None:
        self.settings = settings
        self.run_dir = run_dir
        self._pw = None
        self._browser = None
        self.context = None
        self.page: Page | None = None
        self.last_status: int | None = None
        self.shot_counter = 0

    async def start(self) -> None:
        if self.page:
            return
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.settings.headless,
                                                       slow_mo=self.settings.slow_mo_ms or None)
        kwargs: dict[str, Any] = {"viewport": {"width": 1280, "height": 860}, "accept_downloads": True}
        if self.settings.record_video:
            kwargs["record_video_dir"] = str(self.run_dir / "video")
            kwargs["record_video_size"] = {"width": 1280, "height": 860}
        self.context = await self._browser.new_context(**kwargs)
        self.page = await self.context.new_page()
        self.page.set_default_timeout(15000)
        self.page.on("response", self._on_response)

    def _on_response(self, response) -> None:
        try:
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                self.last_status = response.status
        except Exception:  # noqa: BLE001
            pass

    async def ensure_page(self) -> Page:
        await self.start()
        assert self.page is not None
        return self.page

    async def close(self) -> None:
        try:
            if self.context:
                await self.context.close()   # finalises the video file
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self.page = None

    async def snapshot(self) -> dict[str, Any]:
        page = await self.ensure_page()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except PlaywrightTimeout:
            pass
        return await page.evaluate(_SNAPSHOT_JS, self.settings.snapshot_max_chars)

    async def screenshot(self, label: str) -> str | None:
        if not self.page:
            return None
        self.shot_counter += 1
        path = self.run_dir / "screens" / f"{self.shot_counter:03d}-{re.sub(r'[^a-z0-9]+', '-', label.lower())[:40]}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self.page.screenshot(path=str(path))
            return str(path)
        except PlaywrightError:
            return None


# ----------------------------------------------------------------------------- helpers
def _status_note(status: int | None) -> str:
    return f"HTTP status: {status}" if status else "HTTP status: (no navigation)"


async def _observe(ctx: RunContext, label: str, status: int | None = None) -> tuple[str, dict, str | None]:
    """Snapshot + screenshot after an action. Returns (text for model, raw snapshot, screenshot path)."""
    b = ctx.browser
    assert b is not None
    snap = await b.snapshot()
    shot = await b.screenshot(label)
    if shot:
        ctx.events.emit("screenshot", step=ctx.step, path=shot, url=snap["url"], title=snap["title"])
    header = [f"URL: {snap['url']}", f"Title: {snap['title']}", _status_note(status)]
    if snap.get("hasPasswordField"):
        header.append("Note: this page contains a sign-in form (password field).")
    body = snap["text"]
    if snap.get("truncated"):
        body += "\n[... snapshot truncated - use browser_extract_text to read the full page ...]"
    return "\n".join(header) + "\n---\n" + body, snap, shot


def _classify_status(status: int | None) -> ErrorKind | None:
    if status is None:
        return None
    if status >= 500:
        return ErrorKind.TRANSIENT
    if status in (401, 403):
        return ErrorKind.AUTH
    if status == 404:
        return ErrorKind.NOT_FOUND
    if status in (400, 409, 422):
        return ErrorKind.VALIDATION
    return None


async def _locate(ctx: RunContext, ref: int):
    page = await ctx.browser.ensure_page()
    loc = page.locator(f'[data-agent-ref="{int(ref)}"]')
    if await loc.count() == 0:
        raise ToolError(ErrorKind.NOT_FOUND, f"No element with ref {ref} on the current page (refs are renewed "
                                             f"after every snapshot).")
    return page, loc.first


async def _perform_page_action(ctx: RunContext, label: str, action, info: dict | None) -> ToolResult:
    """Runs a click/enter that may navigate or submit a form, then observes and classifies the result."""
    b = ctx.browser
    b.last_status = None
    submit = (info or {}).get("submit")
    try:
        await action()
        try:
            await b.page.wait_for_load_state("domcontentloaded", timeout=10000)
            await b.page.wait_for_timeout(250)
        except PlaywrightTimeout:
            pass
    except PlaywrightTimeout as e:
        if "intercepts pointer events" in str(e):
            raise ToolError(ErrorKind.INVALID_INPUT,
                            f"Could not {label}: another element (probably a pop-up or modal dialog) is covering it. "
                            f"Nothing was clicked.",
                            "Close the dialog first (look for a 'No thanks' / 'Close' / 'Dismiss' button in a fresh "
                            "snapshot), then retry.")
        raise ToolError(ErrorKind.TRANSIENT, f"Timed out while trying to {label}.")
    text, snap, shot = await _observe(ctx, label, b.last_status)
    kind = _classify_status(b.last_status)
    is_write = bool(submit and submit["method"] != "GET" and not submit.get("hasPassword"))
    if kind:
        msg = f"After '{label}' the server responded with HTTP {b.last_status}.\n{text}"
        hint = None
        if kind == ErrorKind.TRANSIENT and is_write:
            hint = ("The form submission failed with a server error. It may or may not have been saved: "
                    "check the target system (e.g. search for the record) BEFORE submitting again. If the web "
                    "form keeps failing, look for an alternative route such as a documented API.")
        if kind == ErrorKind.AUTH and submit and submit.get("hasPassword"):
            hint = "Sign-in failed. Check that you used the correct {{secret:...}} placeholders for this system."
        return ToolResult.failure(kind, msg, hint, screenshot=shot, retry_safe=False if is_write else None,
                                  data={"url": snap["url"], "status": b.last_status})
    if is_write:
        ctx.record_action(f"Submitted form on {submit['action']}", url=snap["url"], fields=submit["fields"],
                          result_title=snap["title"], screenshot=shot)
        if shot:
            ctx.add_evidence(shot, f"After submitting form: {snap['title']}")
    return ToolResult.success(text, screenshot=shot, side_effect=is_write,
                              data={"url": snap["url"], "status": b.last_status})


# ----------------------------------------------------------------------------- tools
class BrowserNavigate(Tool):
    name = "browser_navigate"
    description = ("Open a URL in the browser and return a text snapshot of the page. Interactive elements "
                   "appear as [ref:kind \"label\"]; use the ref number with the other browser tools.")
    input_schema = schema({"url": {"type": "string", "description": "Absolute URL, e.g. http://localhost:8001/invoices"}},
                          ["url"])

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        url = args["url"].strip()
        await ctx.guard.authorize(ProposedAction("navigate", f"Open {url}", url=url), ctx)
        page = await ctx.browser.ensure_page()
        ctx.browser.last_status = None
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeout:
            raise ToolError(ErrorKind.TRANSIENT, f"Timed out loading {url}.")
        except PlaywrightError as e:
            if "Download is starting" in str(e):
                return ToolResult.failure(ErrorKind.INVALID_INPUT, f"{url} is a file download, not a page.",
                                          "Use browser_download with this URL to save the file, then file_read it.")
            if "ERR_CONNECTION_REFUSED" in str(e):
                raise ToolError(ErrorKind.TRANSIENT, f"Could not connect to {url} (connection refused).")
            raise
        text, snap, shot = await _observe(ctx, f"open {url}", ctx.browser.last_status)
        kind = _classify_status(ctx.browser.last_status)
        if kind:
            return ToolResult.failure(kind, f"HTTP {ctx.browser.last_status} for {url}.\n{text}", screenshot=shot)
        return ToolResult.success(text, screenshot=shot, data={"url": snap["url"]})


class BrowserSnapshot(Tool):
    name = "browser_snapshot"
    description = "Return a fresh text snapshot of the current page (use after refs became stale or to re-read the page)."
    input_schema = schema({})

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        text, snap, shot = await _observe(ctx, "snapshot", ctx.browser.last_status)
        return ToolResult.success(text, screenshot=shot, data={"url": snap["url"]})


class BrowserClick(Tool):
    name = "browser_click"
    description = ("Click an element by its ref (link, button, checkbox...). Returns the resulting page snapshot. "
                   "Clicking a submit button submits its form; data-changing submissions are checked by company "
                   "policy and may require the user's approval.")
    input_schema = schema({"ref": {"type": "integer", "description": "Element ref number from the latest snapshot"}},
                          ["ref"])
    idempotent = False

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        page, loc = await _locate(ctx, args["ref"])
        info = await page.evaluate(_ACTION_INFO_JS, int(args["ref"]))
        await _authorize_click(ctx, info)
        label = f"click {info.get('text') or info.get('tag', 'element')}"
        return await _perform_page_action(ctx, label, lambda: loc.click(timeout=6000), info)


async def _authorize_click(ctx: RunContext, info: dict | None) -> None:
    info = info or {}
    if info.get("href"):
        await ctx.guard.authorize(ProposedAction("navigate", f"Follow link '{info.get('text')}'",
                                                 url=info["href"], text=info.get("text")), ctx)
    sub = info.get("submit")
    if sub:
        kind = "form_submit" if sub["method"] != "GET" else "navigate"
        await ctx.guard.authorize(ProposedAction(
            kind, f"Submit form '{info.get('text') or 'submit'}' ({sub['method']} {sub['action']})",
            url=sub["action"], method=sub["method"], fields=sub["fields"], text=info.get("text"),
            is_login=sub["hasPassword"]), ctx)
    else:
        await ctx.guard.authorize(ProposedAction("click", f"Click '{info.get('text')}'", text=info.get("text")), ctx)


class BrowserType(Tool):
    name = "browser_type"
    description = ("Replace the content of an input/textarea (by ref) with the given text. Use {{secret:name}} "
                   "placeholders for credentials - they are filled in securely. Set submit=true to press Enter "
                   "afterwards (submits the form).")
    input_schema = schema({
        "ref": {"type": "integer"},
        "text": {"type": "string"},
        "submit": {"type": "boolean", "description": "Press Enter after typing (default false)"},
    }, ["ref", "text"])

    def is_idempotent(self, args: dict[str, Any]) -> bool:
        return not args.get("submit")   # pressing Enter may submit a form - never auto-retry that

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        page, loc = await _locate(ctx, args["ref"])
        info = await page.evaluate(_ACTION_INFO_JS, int(args["ref"]))
        if not info or not info.get("isField") or info.get("tag") == "SELECT":
            raise ToolError(ErrorKind.INVALID_INPUT, f"Element {args['ref']} is not a text field.",
                            "Use browser_select for dropdowns and browser_click for buttons/links.")
        try:
            value = ctx.vault.resolve(args["text"])
        except KeyError as e:
            raise ToolError(ErrorKind.INVALID_INPUT, str(e))
        await loc.fill(value, timeout=10000)
        shown = "********" if value != args["text"] else args["text"]
        if not args.get("submit"):
            return ToolResult.success(f"Typed {shown!r} into element {args['ref']}.")
        sub = info.get("enterSubmits")
        if sub:
            info = {**info, "submit": {**sub, "fields": sub["fields"]}}
            await _authorize_click(ctx, {"submit": sub, "text": "Enter"})
        return await _perform_page_action(ctx, "press Enter", lambda: loc.press("Enter"), info)


class BrowserSelect(Tool):
    name = "browser_select"
    description = "Choose an option in a dropdown (select element) by its visible label or value."
    input_schema = schema({"ref": {"type": "integer"}, "option": {"type": "string"}}, ["ref", "option"])

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        page, loc = await _locate(ctx, args["ref"])
        opt = args["option"]
        try:
            chosen = await loc.select_option(label=opt, timeout=5000)
        except (PlaywrightError, PlaywrightTimeout):
            try:
                chosen = await loc.select_option(value=opt, timeout=5000)
            except (PlaywrightError, PlaywrightTimeout):
                options = await loc.evaluate("el => [...el.options].map(o => o.text.trim())")
                raise ToolError(ErrorKind.VALIDATION, f"Option {opt!r} not found. Available options: {options}",
                                "Pick one of the available options exactly as written.")
        return ToolResult.success(f"Selected {opt!r} (value {chosen}) in element {args['ref']}.")


class BrowserExtractText(Tool):
    name = "browser_extract_text"
    description = "Return the full visible text of the current page (no element refs). Useful for long documents."
    input_schema = schema({})

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        page = await ctx.browser.ensure_page()
        text = await page.evaluate("() => document.body.innerText")
        return ToolResult.success(f"URL: {page.url}\n---\n{text[:20000]}")


class BrowserBack(Tool):
    name = "browser_back"
    description = "Go back to the previous page and return its snapshot."
    input_schema = schema({})

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        page = await ctx.browser.ensure_page()
        ctx.browser.last_status = None
        await page.go_back(wait_until="domcontentloaded")
        text, snap, shot = await _observe(ctx, "back", ctx.browser.last_status)
        return ToolResult.success(text, screenshot=shot)


class BrowserScreenshot(Tool):
    name = "browser_screenshot"
    description = ("Take a screenshot of the current page. Set look=true to also see the image yourself "
                   "(use only when the text snapshot is not enough, e.g. for visual layout).")
    input_schema = schema({"look": {"type": "boolean"}})

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        await ctx.browser.ensure_page()
        shot = await ctx.browser.screenshot("screenshot")
        if not shot:
            raise ToolError(ErrorKind.FATAL, "Could not take a screenshot.")
        ctx.events.emit("screenshot", step=ctx.step, path=shot, url=ctx.browser.page.url)
        ctx.add_evidence(shot, f"Screenshot of {ctx.browser.page.url}")
        return ToolResult.success(f"Screenshot saved to {shot}.", screenshot=shot,
                                  image_path=shot if args.get("look") else None)


class BrowserDownload(Tool):
    name = "browser_download"
    description = ("Download a file (e.g. a PDF invoice) using the browser's signed-in session, either by "
                   "clicking a link ref or from a URL. Saves into workspace/downloads/ and returns the path; then "
                   "use file_read to read it.")
    input_schema = schema({"ref": {"type": "integer"}, "url": {"type": "string"}})

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        page = await ctx.browser.ensure_page()
        dest_dir = ctx.workspace / "downloads"
        dest_dir.mkdir(parents=True, exist_ok=True)
        url = args.get("url")
        if url is None and args.get("ref") is not None:
            _, loc = await _locate(ctx, args["ref"])
            href = await loc.get_attribute("href")
            url = urljoin(page.url, href) if href else None
        if not url:
            raise ToolError(ErrorKind.INVALID_INPUT, "Provide either a link ref or a url to download.")
        url = urljoin(page.url, url)
        await ctx.guard.authorize(ProposedAction("navigate", f"Download {url}", url=url), ctx)
        resp = await ctx.browser.context.request.get(url)
        if resp.status >= 400:
            kind = _classify_status(resp.status) or ErrorKind.FATAL
            raise ToolError(kind, f"Download failed: HTTP {resp.status} for {url}.")
        body = await resp.body()
        cd = resp.headers.get("content-disposition", "")
        m = re.search(r'filename="?([^";]+)"?', cd)
        name = m.group(1) if m else (url.rstrip("/").split("/")[-1] or "download.bin")
        name = re.sub(r"[^A-Za-z0-9._\-]", "_", name)
        path = dest_dir / name
        path.write_bytes(body)
        rel = path.relative_to(ctx.workspace).as_posix()
        return ToolResult.success(f"Downloaded {len(body):,} bytes from {url} to {rel}. Use file_read('{rel}') to read it.",
                                  data={"path": rel, "url": url})


BROWSER_TOOLS: list[type[Tool]] = [BrowserNavigate, BrowserSnapshot, BrowserClick, BrowserType, BrowserSelect,
                                   BrowserExtractText, BrowserBack, BrowserScreenshot, BrowserDownload]
