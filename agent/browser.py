"""The browser, for company systems that only have a web UI (the legacy Finance Portal).

The model never sees HTML. After every action it gets a page *digest*: the title and path, errors and notices
first, any dialog that blocks the page, key/value details, tables as rows, forms as labelled fields with their
current values and options, then links. Actions are semantic: open a URL, fill fields by their visible labels
(and optionally press the form's button), click by visible text. Elements are found the way a person finds them
(labels, roles, text), never by element ids, which legacy UIs regenerate (chaos mode shuffles them).

The runtime keeps two things away from the model: credentials (sign-in happens automatically, from the vault)
and the decision about consequential requests: every form POST goes through `on_post` first, which is where the
policy gate plugs in (Phase 2).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

from agent.result import Result

MAX_ROWS = 20
MAX_OPTIONS = 25

# One pass over the DOM. Every element the model may act on gets a data-nova ref for this snapshot.
_EXTRACT = r"""
() => {
  const isVis = (el) => {
    if (!el || el.closest('[hidden]')) return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  };
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const clean = (el) => {
    if (!el) return '';
    const c = el.cloneNode(true);
    c.querySelectorAll('[aria-hidden="true"],script,style,.req').forEach((x) => x.remove());
    return norm(c.textContent);
  };
  const vtext = (el) => (el ? norm(el.innerText || el.textContent) : '');
  document.querySelectorAll('[data-nova]').forEach((e) => e.removeAttribute('data-nova'));
  let n = 0;
  const ref = (el, p) => { const r = p + (++n); el.setAttribute('data-nova', r); return r; };
  const DLG = '[role=dialog],[role=alertdialog],dialog[open],[aria-modal=true]';
  const headings = [...document.querySelectorAll('h1,h2,h3')].filter((h) => isVis(h) && !h.closest(DLG));
  const headingBefore = (el) => {
    let h = null;
    for (const x of headings) if (x.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) h = x;
    return h;
  };
  const out = {title: document.title, url: location.href, h1: '', header: '', crumbs: '', alerts: [], notes: [],
               dialogs: [], texts: [], tables: [], forms: [], clickables: [], password: false};
  const h1 = headings.find((h) => h.tagName === 'H1');
  if (h1) out.h1 = clean(h1);
  const header = document.querySelector('header');
  if (header && isVis(header)) out.header = vtext(header);
  const crumbs = document.querySelector('[class*=crumb],nav[aria-label*=readcrumb]');
  if (crumbs && isVis(crumbs)) out.crumbs = vtext(crumbs);

  const dialogs = [];
  for (const d of document.querySelectorAll(DLG)) {
    if (!isVis(d) || dialogs.some((x) => x.contains(d))) continue;
    dialogs.push(d);
    const lab = d.getAttribute('aria-labelledby');
    const t = lab ? document.getElementById(lab) : d.querySelector('h1,h2,h3');
    const buttons = [...d.querySelectorAll('button,[role=button],input[type=submit],input[type=button],a[href]')]
      .filter(isVis).map((b) => {
        const r = ref(b, 'b');
        const text = clean(b) || b.value || b.getAttribute('aria-label') || '';
        out.clickables.push({ref: r, text, href: b.getAttribute('href') || '', kind: 'button', area: 'dialog',
                             nav: b.tagName === 'A' || (b.type || '') === 'submit'});
        return text;
      });
    const paras = [...d.querySelectorAll('p')].map(vtext).filter(Boolean);
    out.dialogs.push({title: t ? clean(t) : '', text: paras.join(' ') || vtext(d), buttons});
  }
  const inDialog = (el) => dialogs.some((d) => d.contains(el));

  for (const a of document.querySelectorAll('[role=alert]')) {
    if (!isVis(a) || inDialog(a)) continue;
    const items = [...a.querySelectorAll('li')].map(vtext).filter(Boolean);
    const head = a.querySelector('b,strong');
    out.alerts.push(items.length ? ((head ? vtext(head) + ' ' : '') + items.join(' ')) : vtext(a));
  }
  for (const s of document.querySelectorAll('[role=status]')) if (isVis(s) && !inDialog(s)) out.notes.push(vtext(s));

  for (const t of document.querySelectorAll('table')) {
    if (!isVis(t) || inDialog(t)) continue;
    const ths = [...t.querySelectorAll('thead th')];
    if (ths.length) {
      const rows = []; let empty = '';
      for (const tr of t.querySelectorAll('tbody tr')) {
        const cells = [...tr.children].filter((c) => c.tagName === 'TD' || c.tagName === 'TH');
        if (cells.length === 1 && cells[0].colSpan > 1) { empty = vtext(cells[0]); continue; }
        rows.push(cells.map(vtext));
      }
      const cap = t.querySelector('caption'), h = headingBefore(t);
      out.tables.push({kind: 'grid', name: cap ? clean(cap) : (h && h.tagName !== 'H1' ? clean(h) : ''),
                       headers: ths.map(clean), rows, empty});
    } else {
      const pairs = [];
      for (const tr of t.querySelectorAll('tr')) {
        const cells = [...tr.children];
        for (let i = 0; i + 1 < cells.length; i += 2)
          if (cells[i].tagName === 'TH') pairs.push([clean(cells[i]), vtext(cells[i + 1])]);
      }
      if (pairs.length) out.tables.push({kind: 'kv', pairs});
    }
  }

  for (const f of document.forms) {
    if (!isVis(f) || inDialog(f)) continue;
    const fields = [];
    for (const el of f.querySelectorAll('input,select,textarea')) {
      const type = el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : (el.type || 'text');
      if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type) || !isVis(el)) continue;
      let lab = el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null;
      if (!lab) lab = el.closest('label');
      let label = lab ? clean(lab) : '';
      if (!label) label = norm(el.getAttribute('aria-label'));
      const lb = el.getAttribute('aria-labelledby');
      if (!label && lb && document.getElementById(lb)) label = clean(document.getElementById(lb));
      if (!label) label = norm(el.placeholder || el.name);
      const box = el.closest('.field') || el.parentElement;
      const hintEl = box ? box.querySelector('.hint,[class*=help]') : null;
      const desc = el.getAttribute('aria-describedby');
      const hint = norm((hintEl ? hintEl.textContent : '') ||
                        (desc && document.getElementById(desc) ? document.getElementById(desc).textContent : ''));
      const fd = {ref: ref(el, 'f'), label, name: el.name || '', type, hint, placeholder: el.placeholder || '',
                  required: el.required || el.getAttribute('aria-required') === 'true' || !!(lab && lab.querySelector('.req'))};
      if (type === 'select') {
        fd.options = [...el.options].map((o) => ({value: o.value, label: norm(o.textContent)}));
        const so = el.options[el.selectedIndex];
        fd.value = so ? norm(so.textContent) : '';
        fd.unset = !so || so.value === '';
      } else if (type === 'checkbox' || type === 'radio') {
        fd.value = el.checked ? 'checked' : 'not checked';
      } else if (type === 'password') {
        fd.value = el.value ? '******' : '';
        out.password = true;
      } else {
        fd.value = el.value;
      }
      fields.push(fd);
    }
    const buttons = [...f.querySelectorAll('button,input[type=submit],input[type=button]')].filter(isVis).map((b) => {
      const submit = b.tagName === 'BUTTON' ? (b.getAttribute('type') || 'submit') === 'submit' : b.type === 'submit';
      const text = clean(b) || b.value || '';
      const r = ref(b, 'b');
      out.clickables.push({ref: r, text, href: '', kind: 'button', area: 'form', nav: submit});
      return {ref: r, text, submit};
    });
    const h = headingBefore(f);
    const name = norm(f.getAttribute('aria-label')) ||
      (f.getAttribute('role') === 'search' ? 'Search' + (h ? ' ' + clean(h) : '') : '') ||
      (h ? clean(h) : '') || (buttons[0] ? buttons[0].text : 'form');
    out.forms.push({ref: ref(f, 'F'), name, role: f.getAttribute('role') || '', fields, buttons});
  }

  for (const a of document.querySelectorAll('a[href]')) {
    if (!isVis(a) || inDialog(a)) continue;
    const area = a.closest('nav') ? 'menu' : a.closest('table') ? 'table' : a.closest('header,footer') ? 'header'
      : a.closest('[class*=crumb]') ? 'crumbs' : 'main';
    out.clickables.push({ref: ref(a, 'l'), text: clean(a) || a.getAttribute('aria-label') || a.title || '',
                         href: a.getAttribute('href'), kind: 'link', area, nav: true});
  }
  for (const b of document.querySelectorAll('button,[role=button]')) {
    if (!isVis(b) || b.closest('form') || inDialog(b) || b.hasAttribute('data-nova')) continue;
    out.clickables.push({ref: ref(b, 'b'), text: clean(b), href: '', kind: 'button', area: 'main', nav: false});
  }

  const main = document.querySelector('main') || document.body;
  const seen = new Set();
  for (const el of main.querySelectorAll('p,[class*=notice],[class*=pager],[class*=muted]')) {
    if (!isVis(el) || el.closest('h1,h2,h3,table,form,nav,header,footer,[role=alert],[role=status]') || inDialog(el)) continue;
    const t = vtext(el);
    const linkText = norm([...el.querySelectorAll('a,button')].map((x) => x.innerText).join(' '));
    if (!t || t === linkText || seen.has(t)) continue;
    seen.add(t);
    out.texts.push(t);
  }
  return out;
}
"""


def _norm(s: Any) -> str:
    """Label/option matching key: case, '*', dashes, whitespace and trailing ':' / '.' don't matter."""
    s = str(s).lower().replace("*", " ")
    s = re.sub(r"[–—‒−]", "-", s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"[\s:.]+$", "", s)


def _plain(v: Any) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else repr(float(v))
    return "" if v is None else str(v)


def _placeholder(label: str) -> bool:
    return bool(re.match(r"^\s*([—–-]+|select\b|choose\b|please select)", label, re.I))


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _path(url: str) -> str:
    p = urlsplit(url)
    return p.path + (f"?{p.query}" if p.query else "")


class Browser:
    def __init__(self, sites: dict[str, dict], downloads_dir: Path, headless: bool = True,
                 on_post: Callable[[dict], tuple[bool, str] | None] | None = None, timeout_s: float = 20):
        self.sites = {k: v for k, v in sites.items() if v.get("url")}
        self.downloads_dir = Path(downloads_dir)
        self.headless, self.on_post, self.timeout_s = headless, on_post, timeout_s
        self._pw = self._browser = self._context = self.page = None
        self._navs = 0
        self._pending: list[Any] = []  # downloads started by the last action
        self._blocked: str | None = None  # the policy gate stopped the last request (and why)
        self._post_errors: list[dict] = []  # form submissions the server answered with 5xx (outcome unknown)
        self._secrets = {v["password"] for v in self.sites.values() if v.get("password")}
        self.forms_seen: dict[str, dict] = {}  # page → {"title", "labels", "buttons"} of the forms it had
        self.form_page = ""  # the page of the last form submitted (to go back to it after an error page)

    # ------------------------------------------------------------------ lifecycle
    def _start(self) -> None:
        if self.page is not None:
            return
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(accept_downloads=True)
        self._context.set_default_timeout(self.timeout_s * 1000)
        origins = {_origin(v["url"]) for v in self.sites.values()}
        self._context.route(lambda url: _origin(url) in origins, self._route)
        # egress control: the browser can only reach the company's own web apps, wherever a link points
        self._context.route(lambda url: url.startswith("http") and _origin(url) not in origins,
                            lambda route, request: route.abort("blockedbyclient"))
        self.page = self._context.new_page()
        self.page.on("framenavigated", self._on_nav)
        self.page.on("download", self._on_download)
        self.page.on("response", self._on_response)

    def close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                closer and closer.close()
            except PWError:
                pass
        if self._pw:
            self._pw.stop()
        self._pw = self._browser = self._context = self.page = None

    def _on_nav(self, frame) -> None:
        if self.page is not None and frame == self.page.main_frame:
            self._navs += 1

    def _on_download(self, download) -> None:
        self._pending.append(download)  # saved by the action that caused it (no blocking calls inside handlers)

    def _on_response(self, response) -> None:
        """A form submission the server answered with an error: whether it was saved is unknown (only noted here)."""
        if response.status >= 500 and response.request.method == "POST":
            self._post_errors.append({"path": _path(response.url), "status": response.status})

    def _route(self, route, request) -> None:
        if request.method != "POST" or self.on_post is None:
            route.continue_()
            return
        try:
            form = dict(parse_qsl(request.post_data or "", keep_blank_values=True))
        except (ValueError, UnicodeDecodeError):
            form = {"(body)": "(not a form)"}
        form = {k: ("******" if v in self._secrets or "pass" in k.lower() else v) for k, v in form.items()}
        try:
            verdict = self.on_post({"method": "POST", "url": request.url, "path": _path(request.url), "form": form})
        except Exception as e:  # a broken gate must fail closed
            verdict = (False, f"The request was not sent: the policy check failed ({e}).")
        if verdict is not None and not verdict[0]:
            # 204: the browser stays on the page it was on, so the filled form survives and only the problem is fixed
            self._blocked = verdict[1] or "Blocked by policy."
            route.fulfill(status=204, body="")
        else:
            route.continue_()

    # ------------------------------------------------------------------ public actions
    def open(self, url: str) -> Result:
        try:
            self._start()
            target = self._resolve(url)
            self._pending.clear()
            try:
                self.page.goto(target, wait_until="load")
            except PWError as e:
                if "Download is starting" not in str(e):
                    raise
                deadline = time.monotonic() + 5
                while not self._pending and time.monotonic() < deadline:
                    self.page.wait_for_timeout(50)
            downloaded = self._save_downloads()
            signed = self._sign_in_if_needed()
            return self._result(f"Opened {_path(self.page.url)}", signed=signed, downloaded=downloaded)
        except PWError as e:
            return self._error(e, url)
        except ValueError as e:
            return Result.error(str(e))

    def peek(self, url: str, data_only: bool = False) -> str:
        """Read one page in a separate tab of the same session and close it again: the page Nova is working on stays
        as it is. For the runtime's own read-only lookups. Returns the digest (or with data_only, only the page's
        heading and tables: not its URL, menus or forms, where a searched-for value always appears), "" if the page
        can't be read."""
        try:
            self._start()
            tab = self._context.new_page()
        except (PWError, ValueError):
            return ""
        try:
            target = self._resolve(url)
            tab.goto(target, wait_until="load")
            button = self._fill_login(tab)  # a fresh session (e.g. after a restart) lands on the sign-in page
            if button is not None:
                with tab.expect_navigation(wait_until="load"):
                    button.click(timeout=5000)
                if _path(tab.url) != _path(target):
                    tab.goto(target, wait_until="load")
            snap = tab.evaluate(_EXTRACT)
            signed_out = any(fd["type"] == "password" for f in snap.get("forms", []) for fd in f["fields"])
            if signed_out:  # still a sign-in page: nothing to read
                return ""
            if not data_only:
                return self.digest(snap)
            parts = [snap.get("h1") or ""]
            for t in snap.get("tables", []):
                parts += ([f"{k}: {v}" for k, v in t["pairs"]] if t["kind"] == "kv"
                          else [" | ".join(r) for r in t.get("rows") or []])
            return "\n".join(parts)
        except (PWError, ValueError):
            return ""
        finally:
            try:
                tab.close()
            except PWError:
                pass

    def inspect(self, url: str) -> tuple[Result, dict | None]:
        """Open a page and return its digest and its structured snapshot (for code that checks values)."""
        res = self.open(url)
        try:
            return res, (self._extract() if self.page is not None else None)
        except PWError:
            return res, None

    def read(self) -> Result:
        if self.page is None:
            return Result.error("no page is open yet; use browser_open first")
        try:
            return self._result(f"Read {_path(self.page.url)}")
        except PWError as e:
            return self._error(e)

    def click(self, target: str) -> Result:
        if self.page is None:
            return Result.error("no page is open yet; use browser_open first")
        t = str(target).strip()
        try:
            snap = self._extract()
            c = self._choose_clickable(t, snap["clickables"])
            menu = re.fullmatch(r"(.+?)\s*\((/[^()\s]*)\)", t)  # "Vendors (/vendors)": the way the digest lists links
            if isinstance(c, str) and menu:
                c = self._choose_clickable(menu[1], snap["clickables"])
                if isinstance(c, str):
                    return self.open(menu[2])
            if isinstance(c, str):
                if t.startswith(("/", "http://", "https://")):
                    return self.open(t)
                return Result.error(self._not_clickable(t, snap["forms"]) or c)
            if snap["dialogs"] and c["area"] != "dialog":
                d = snap["dialogs"][0]
                return Result.error(f'a dialog "{d["title"]}" is open and blocks the page. Dismiss it first by '
                                    f'clicking one of its buttons: {", ".join(d["buttons"])}.')
            loc = self.page.locator(f'[data-nova="{c["ref"]}"]')
            navigated = self._do(lambda: loc.click(timeout=5000), expect_nav=c["nav"])
            blocked, self._blocked = self._blocked, None
            downloaded = self._save_downloads()
            signed = self._sign_in_if_needed()
            label = c["text"] or c["href"]
            return self._result(f'Clicked "{label}"' + (f" → {_path(self.page.url)}" if navigated else ""),
                                signed=signed, downloaded=downloaded, blocked=blocked)
        except PWError as e:
            return self._error(e, t)

    def fill(self, fields: dict[str, Any], submit: str | None = None) -> Result:
        if self.page is None:
            return Result.error("no page is open yet; use browser_open first")
        try:
            snap = self._extract()
            if snap["dialogs"]:
                d = snap["dialogs"][0]
                return Result.error(f'a dialog "{d["title"]}" is open and blocks the page. Dismiss it first by '
                                    f'clicking one of its buttons: {", ".join(d["buttons"])}.')
            forms = snap["forms"]
            if not forms:
                return Result.error(f'this page ({_path(self.page.url)} "{snap["title"]}") has no form to fill.'
                                    + self._seen_elsewhere(fields, submit))
            form, button, problem = self._target_form(forms, fields, submit)
            if problem:
                problem += self._seen_elsewhere(fields, submit)
                return Result(f"ERROR: {problem}\n\n{self._forms_text(forms)}", f"ERROR: {problem[:220]}", ok=False)
            if button is None and form["role"] == "search":  # a search box filled and not searched does nothing
                # (seen 2026-10-08: the same search filled five times, never submitted)
                button = next((b for b in form["buttons"] if b["submit"]), None)
            plan, problems = [], []
            for key, value in fields.items():
                fd = self._match_field(key, form["fields"])
                if isinstance(fd, str):
                    problems.append(fd)
                    continue
                if fd["type"] == "select":
                    opt = self._match_option(value, fd)
                    if isinstance(opt, str):
                        problems.append(opt)
                        continue
                    plan.append((fd, opt))
                else:
                    plan.append((fd, value))
            if problems:
                msg = "nothing was filled or submitted: " + " ".join(problems) + self._seen_elsewhere(fields, submit)
                return Result(f"ERROR: {msg}\n\n{self._forms_text([form])}", f"ERROR: {msg[:220]}", ok=False)
            for fd, value in plan:
                loc = self.page.locator(f'[data-nova="{fd["ref"]}"]')
                if fd["type"] == "select":
                    loc.select_option(value=value["value"], timeout=5000)
                elif fd["type"] in ("checkbox", "radio"):
                    loc.set_checked(_norm(value) in ("yes", "true", "1", "checked", "on"), timeout=5000)
                else:
                    loc.fill(_plain(value), timeout=5000)
            filled = ", ".join(fd["label"] for fd, _ in plan)
            if not button:
                return self._result(f'Filled {filled} in form "{form["name"]}" (not submitted)')
            loc = self.page.locator(f'[data-nova="{button["ref"]}"]')
            self.form_page = _path(self.page.url)
            navigated = self._do(lambda: loc.click(timeout=5000), expect_nav=True)
            blocked, self._blocked = self._blocked, None
            signed = self._sign_in_if_needed(during_submit=True)
            return self._result(f'Submitted "{button["text"]}"' + (f" → {_path(self.page.url)}" if navigated else ""),
                                signed=signed, blocked=blocked)
        except PWError as e:
            return self._error(e)

    # ------------------------------------------------------------------ helpers: finding things
    def _resolve(self, url: str) -> str:
        u = re.sub(r"^/+(?=https?://)", "", str(url or "").strip())  # "/http://…/vendors" (seen 2026-10-08)
        if not u:
            raise ValueError("give a URL or a path, e.g. /bills")
        if u in self.sites:
            return self.sites[u]["url"]
        if re.match(r"^https?://", u):
            if _origin(u) not in {_origin(v["url"]) for v in self.sites.values()}:
                raise ValueError(f"{_origin(u)} is not one of the company's web apps ("
                                 + ", ".join(v["url"] for v in self.sites.values()) + ")")
            return u
        first, _, rest = u.lstrip("/").partition("/")
        if first in self.sites:
            return self.sites[first]["url"].rstrip("/") + "/" + rest
        here = self.page.url if self.page is not None and self.page.url.startswith("http") else ""
        base = _origin(here) if here and _origin(here) in {_origin(v["url"]) for v in self.sites.values()} \
            else _origin(next(iter(self.sites.values()))["url"])
        return base + "/" + u.lstrip("/")

    def _choose_clickable(self, t: str, items: list[dict]) -> dict | str:
        key = _norm(t)
        groups = ([c for c in items if _norm(c["text"]) == key],
                  [c for c in items if c["href"] and (c["href"] == t or c["href"].rstrip("/") == t.rstrip("/"))],
                  [c for c in items if key and key in _norm(c["text"])])
        for cands in groups:
            if not cands:
                continue
            if len({(c["href"], c["kind"]) for c in cands}) == 1 or len(cands) == 1:
                return cands[0]
            in_dialog = [c for c in cands if c["area"] == "dialog"]
            if len(in_dialog) == 1:
                return in_dialog[0]
            listing = "; ".join(f'"{c["text"]}"' + (f' → {c["href"]}' if c["href"] else " (button)") for c in cands[:8])
            return f"{len(cands)} things on this page match {t!r}: {listing}. Use the exact text, or browser_open the path."
        return f"nothing on this page is called {t!r}. Use a link or button text from the page digest."

    @staticmethod
    def _not_clickable(t: str, forms: list[dict]) -> str:
        """A click aimed at a form field or one of a dropdown's options: say how to set it instead."""
        key = _norm(t)
        for f in forms:
            for fd in f["fields"]:
                if fd["type"] == "select" and any(_norm(o["label"]) == key for o in fd["options"]):
                    return (f'{t!r} is an option of the field "{fd["label"]}", not a button. Choose it with '
                            f'browser_fill: fields {{"{fd["label"]}": "{t}"}} (with the form\'s other fields and '
                            "submit, to save in one call).")
                if _norm(fd["label"]) == key:
                    return f'"{fd["label"]}" is a form field, not a button: set it with browser_fill.'
        return ""

    @staticmethod
    def _empty_required(snap: dict) -> list[str]:
        """Required fields of the page's forms that are still empty, as "label (options: ...)"."""
        out = []
        for f in snap["forms"]:
            for fd in f["fields"]:
                if not fd["required"] or fd["type"] in ("checkbox", "radio", "hidden", "password"):
                    continue
                if fd["type"] == "select":
                    if fd["unset"] and _placeholder(fd["value"]):
                        opts = [o["label"] for o in fd["options"] if not (o["value"] == "" and _placeholder(o["label"]))]
                        out.append(f'{fd["label"]} (options: {" | ".join(opts[:MAX_OPTIONS])})')
                elif not str(fd["value"]).strip():
                    out.append(fd["label"])
        return out

    def _seen_elsewhere(self, fields: dict, submit: str | None) -> str:
        """The page (seen earlier in this task) whose form has these fields or this button: a small model that wandered
        off to look something up tries to fill the form where it now is (seen 2026-10-09: payment fields "filled" on
        the vendor list, then on the 503 error page)."""
        here = _path(self.page.url) if self.page is not None else ""
        keys = [k for k in fields or {}]
        for page, f in reversed(list(self.forms_seen.items())):
            if page == here:
                continue
            labels = [{"label": label, "name": ""} for label in f["labels"]]
            found = sum(not isinstance(self._match_field(k, labels), str) for k in keys)
            button = submit and any(_norm(submit) in _norm(b) for b in f["buttons"])
            if (keys and found * 2 >= len(keys)) or (not keys and button):
                return (f' Those fields are on the form at {page} ("{f["title"]}"), seen earlier: open it with '
                        f'browser_open("{page}"), then fill it.')
        return ""

    def _target_form(self, forms: list[dict], fields: dict, submit: str | None) -> tuple[dict, dict | None, str]:
        candidates = forms
        if fields:
            keys = list(fields)
            scored = [(sum(not isinstance(self._match_field(k, f["fields"]), str) for k in keys), i)
                      for i, f in enumerate(forms)]
            best = max(s for s, _ in scored)
            candidates = [forms[i] for s, i in scored if s == best]
        if submit:
            hits = [(f, b) for f in candidates for b in f["buttons"] if _norm(b["text"]) == _norm(submit)] or \
                   [(f, b) for f in candidates for b in f["buttons"] if _norm(submit) in _norm(b["text"])]
            if not hits:
                names = "; ".join(f'"{f["name"]}": ' + ", ".join(f'"{b["text"]}"' for b in f["buttons"]) for f in forms)
                return candidates[0], None, f"no button {submit!r} in that form. Buttons by form: {names}"
            return hits[0][0], hits[0][1], ""
        return candidates[0], None, ""

    @staticmethod
    def _match_field(key: str, fields: list[dict]) -> dict | str:
        k = _norm(key)
        loose = lambda s: re.sub(r"[^a-z0-9]+", " ", _norm(s)).strip()  # noqa: E731
        for test in (lambda f: _norm(f["label"]) == k, lambda f: f["name"] and _norm(f["name"]) == k,
                     lambda f: loose(f["label"]) == loose(key)):
            hits = [f for f in fields if test(f)]
            if len(hits) == 1:
                return hits[0]
        part_of_label = [f for f in fields if k and k in _norm(f["label"])]  # "Account" → "Pay to account"
        if len(part_of_label) == 1:
            return part_of_label[0]
        # "Vendor invoice number" → "Vendor invoice no." (the longest label contained in the key)
        in_key = sorted((f for f in fields if f["label"] and _norm(f["label"]) in k), key=lambda f: -len(f["label"]))
        if in_key and (len(in_key) == 1 or len(in_key[0]["label"]) > len(in_key[1]["label"])) and not part_of_label:
            return in_key[0]
        hits = part_of_label or in_key
        if hits:
            return f"{key!r} matches several fields ({', '.join(repr(f['label']) for f in hits)}); use the exact label."
        labels = ", ".join(f'"{f["label"]}"' for f in fields)
        return f"no field labelled {key!r}. The form's fields are: {labels}."

    @staticmethod
    def _match_option(value: Any, fd: dict) -> dict | str:
        v = _norm(_plain(value))
        opts = fd["options"]
        for cands in ([o for o in opts if _norm(o["label"]) == v or (o["value"] and _norm(o["value"]) == v)],
                      [o for o in opts if v and (v in _norm(o["label"]) or v in _norm(o["value"]))],
                      [o for o in opts if _norm(o["label"]) and _norm(o["label"]) in v and not _placeholder(o["label"])]):
            if len(cands) == 1:
                return cands[0]
            if len(cands) > 1:
                return (f'{_plain(value)!r} matches several options of "{fd["label"]}": '
                        + " | ".join(o["label"] for o in cands) + ".")
        shown = " | ".join(o["label"] for o in opts if not (o["value"] == "" and _placeholder(o["label"])))
        return f'"{fd["label"]}" has no option {_plain(value)!r}. Options: {shown}.'

    # ------------------------------------------------------------------ helpers: acting and waiting
    def _do(self, action: Callable[[], None], expect_nav: bool) -> bool:
        """Run an action and wait for what it causes: a navigation (incl. POST → redirect → GET), a download, or
        (for buttons that only run scripts) a short settle. Returns whether the page navigated."""
        navs, downloads = self._navs, len(self._pending)
        self._blocked = None
        action()
        deadline = time.monotonic() + (self.timeout_s if expect_nav else 1.0)
        while time.monotonic() < deadline and self._navs == navs and len(self._pending) == downloads \
                and not self._blocked:
            self.page.wait_for_timeout(50)
        if self._navs != navs:
            try:
                self.page.wait_for_load_state("load", timeout=self.timeout_s * 1000)
            except PWError:
                pass
            return True
        try:
            self.page.wait_for_load_state("networkidle", timeout=2000)
        except PWError:
            pass
        return False

    def _save_downloads(self) -> list[Path]:
        saved = []
        for d in self._pending:
            self.downloads_dir.mkdir(parents=True, exist_ok=True)
            name = Path(d.suggested_filename).name or "download"
            path, n = self.downloads_dir / name, 2
            while path.exists():
                path = self.downloads_dir / f"{Path(name).stem}-{n}{Path(name).suffix}"
                n += 1
            d.save_as(path)
            saved.append(path)
        self._pending.clear()
        return saved

    def _fill_login(self, page):
        """Fill a login form on `page` with the vault's credentials; returns its submit button (None: no login form)."""
        site = next((k for k, v in self.sites.items() if _origin(v["url"]) == _origin(page.url)), None)
        creds = self.sites.get(site) if site else None
        pw = page.locator("input[type=password]")
        if not creds or not creds.get("password") or not pw.count() or not pw.first.is_visible():
            return None
        form = pw.first.locator("xpath=ancestor::form[1]")
        form.locator("input:not([type=hidden]):not([type=password]):not([type=submit]):not([type=button])"
                     ":not([type=checkbox])").first.fill(creds["username"])
        pw.first.fill(creds["password"])
        return form.locator("button[type=submit], input[type=submit], button:not([type])").first

    def _sign_in_if_needed(self, during_submit: bool = False) -> str | None:
        """Sign in with the vault's credentials when a login form appears. Returns a note, or None."""
        button = self._fill_login(self.page)
        if button is None:
            return None
        self._do(lambda: button.click(timeout=5000), expect_nav=True)
        still = self.page.locator("input[type=password]")
        if still.count() and still.first.is_visible():
            return "FAILED"
        return "expired" if during_submit else "signed in"

    # ------------------------------------------------------------------ helpers: describing the page
    def _extract(self) -> dict:
        try:
            return self.page.evaluate(_EXTRACT)
        except PWError as e:  # the page navigated while being read (e.g. a late redirect): read the new one
            if "context was destroyed" not in str(e) and "navigat" not in str(e).lower():
                raise
            self.page.wait_for_load_state("load", timeout=self.timeout_s * 1000)
            return self.page.evaluate(_EXTRACT)

    def _result(self, what: str, signed: str | None = None, downloaded: list[Path] | None = None,
                blocked: str | None = None) -> Result:
        snap = self._extract()
        text = self.digest(snap)
        ok = not snap["alerts"] and not blocked
        head = []
        work = [f for f in snap["forms"] if f["role"] != "search" and
                not any(fd["type"] == "password" for fd in f["fields"])]
        if work:  # where each form was, for a fill attempted somewhere else later
            page = _path(snap["url"])
            self.forms_seen.pop(page, None)  # most recent last
            self.forms_seen[page] = {"title": snap["title"], "labels": [fd["label"] for f in work for fd in f["fields"]],
                                     "buttons": [b["text"] for f in work for b in f["buttons"]]}
        ignored = self._ignored_filters(snap)
        if ignored:  # (seen 2026-10-08: payments "searched" by bill ID, i.e. all 32 payments, unnoticed)
            head.append(f"NOTE: this page can't filter by {', '.join(ignored)}, so the list below is NOT filtered by "
                        "it. Use the page's own search form, or open the record itself (its page lists what belongs "
                        "to it).")
        if blocked:
            head.append(f"ERROR: Blocked by policy: {blocked}\n(The form is still filled in on the page; fix what the "
                        "policy asks for and submit again.)")
        if signed == "FAILED":
            ok = False
            head.append("ERROR: automatic sign-in failed; the credentials on file were refused.")
        elif signed == "expired":
            ok = False
            head.append("ERROR: the session had expired, so the form was NOT saved. Nova was signed in again; fill "
                        "the form again and submit it.")
        elif signed:
            head.append("(Signed in automatically.)")
        for p in downloaded or []:
            head.append(self._download_text(p))
        empty = self._empty_required(snap) if snap["alerts"] else []
        if empty:  # the page refused a submission: name the fields it still needs, in the form's own words
            head.append(f"Required fields still empty: {'; '.join(empty)}. Fill them with browser_fill and submit "
                        "again.")
        summary = f"{what} {self._gist(snap)}" + (f" — still empty: {'; '.join(empty)[:160]}" if empty else "")
        if blocked:
            summary = f"{what} — BLOCKED by policy: {blocked[:240]}"
        elif signed in ("FAILED", "expired"):
            summary = f"{what} — {head[0]}"
        if downloaded:
            summary += " — downloaded " + ", ".join(f"downloads/{p.name}" for p in downloaded)
        errors, self._post_errors = self._post_errors, []
        if errors:  # the server failed the submission: saved or not, nobody can tell from this page
            ok = False
            head.append(f"ERROR: the portal answered the submission with an error ({errors[-1]['status']}). It may "
                        "or may not have been saved.")
        data = {"url": snap["url"], "title": snap["title"], "h1": snap["h1"], "errors": snap["alerts"],
                "notes": snap["notes"], "dialog": snap["dialogs"][0]["title"] if snap["dialogs"] else None,
                "downloads": [f"downloads/{p.name}" for p in downloaded or []],
                "post_error": errors[-1] if errors else None,
                "forms": [{"name": f["name"], "role": f["role"], "fields": [fd["label"] for fd in f["fields"]],
                           "password": any(fd["type"] == "password" for fd in f["fields"])} for f in snap["forms"]]}
        return Result("\n".join([what, *head, "", text]), summary.strip(), ok=ok, data=data)

    @staticmethod
    def _ignored_filters(snap: dict) -> list[str]:
        """Address parameters a list page has no search field for (the page silently shows everything)."""
        query = dict(parse_qsl(urlsplit(snap.get("url") or "").query))
        searches = [f for f in snap.get("forms", []) if f.get("role") == "search"]
        if not query or not searches:
            return []
        names = {fd.get("name") for f in snap["forms"] for fd in f["fields"]}
        return [f"{k}={v}" for k, v in query.items() if v and k not in names and k not in ("page", "flash")]

    @staticmethod
    def _download_text(path: Path) -> str:
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            return f"Downloaded {path.name} → downloads/{path.name}"
        preview = "\n".join(lines[:6])
        return (f"Downloaded {path.name} → downloads/{path.name} ({max(len(lines) - 1, 0)} data rows; read it with "
                f"file_read). First lines:\n{preview}")

    @staticmethod
    def _gist(snap: dict) -> str:
        """The few words worth keeping in the model's history once the digest itself is hidden."""
        bits = [f'"{snap["h1"] or snap["title"]}"']  # first: the page's heading, then what matters on it
        if snap["dialogs"]:
            bits.append(f'DIALOG "{snap["dialogs"][0]["title"]}" is open')
        if snap["alerts"]:
            bits.append("ERROR: " + snap["alerts"][0][:220])
        if snap["notes"]:
            bits.append("NOTE: " + snap["notes"][0][:120])
        if not snap["alerts"]:
            kv = next((t for t in snap["tables"] if t["kind"] == "kv"), None)
            grid = next((t for t in snap["tables"] if t["kind"] == "grid"), None)
            if kv:
                bits.append(" | ".join(f"{k}: {v}" for k, v in kv["pairs"][:4])[:220])
            elif grid:
                n = len(grid["rows"])
                bits.append(f"{n} row{'s' if n != 1 else ''}" + (": " + " | ".join(grid["rows"][0])[:180] if n
                                                               else f" ({grid['empty']})" if grid["empty"] else ""))
        return " — ".join(bits)

    @staticmethod
    def _field_line(fd: dict) -> str:
        star = "*" if fd["required"] else ""
        if fd["type"] == "select":
            opts = [o["label"] for o in fd["options"] if not (o["value"] == "" and _placeholder(o["label"]))]
            shown = " | ".join(opts[:MAX_OPTIONS]) + (f" | … {len(opts) - MAX_OPTIONS} more" if len(opts) > MAX_OPTIONS else "")
            cur = "(none)" if fd["unset"] and _placeholder(fd["value"]) else f'"{fd["value"]}"'
            line = f"{fd['label']}{star}: select = {cur}; options: {shown}"
        else:
            line = f'{fd["label"]}{star}: {fd["type"]} = "{fd["value"]}"'
        extra = fd["hint"] or fd["placeholder"]
        return line + (f"  ({extra})" if extra else "")

    def _forms_text(self, forms: list[dict]) -> str:
        lines = []
        for f in forms:
            buttons = ", ".join(f'"{b["text"]}"' for b in f["buttons"] if b["submit"])
            lines.append(f'FORM "{f["name"]}"' + (f" — submit button: {buttons}" if buttons else ""))
            lines += ["  " + self._field_line(fd) for fd in f["fields"]]
        return "\n".join(lines)

    def digest(self, snap: dict) -> str:
        lines = [f'PAGE "{snap["title"]}" — {_path(snap["url"])}']
        if snap["header"]:
            lines.append(snap["header"])
        if snap["crumbs"]:
            lines.append(snap["crumbs"])
        if snap["h1"]:
            lines.append(f"# {snap['h1']}")
        lines += [f"ERROR: {a}" for a in snap["alerts"]]
        lines += [f"NOTE: {s}" for s in snap["notes"]]
        for d in snap["dialogs"]:
            lines.append(f'DIALOG "{d["title"]}" (it blocks the page until dismissed): {d["text"]} Buttons: '
                         + " | ".join(d["buttons"]))
        if snap["texts"]:
            lines.append("TEXT: " + " / ".join(snap["texts"][:5])[:600])
        for t in snap["tables"]:
            if t["kind"] == "kv":
                lines.append("DETAILS")
                pairs = t["pairs"]
                lines += ["  " + " | ".join(f"{k}: {v}" for k, v in pairs[i:i + 2]) for i in range(0, len(pairs), 2)]
                continue
            n = len(t["rows"])
            lines.append((f'TABLE "{t["name"]}"' if t["name"] else "TABLE") + f" ({n} row{'s' if n != 1 else ''}): "
                         + " | ".join(t["headers"]))
            if not n:
                lines.append("  (no rows" + (f": {t['empty']})" if t["empty"] else ")"))
            lines += ["  " + " | ".join(r) for r in t["rows"][:MAX_ROWS]]
            if n > MAX_ROWS:
                lines.append(f"  … {n - MAX_ROWS} more rows not shown")
        if snap["forms"]:
            lines.append(self._forms_text(snap["forms"]))
        seen: set[str] = set()
        links = [c["text"] for c in snap["clickables"] if c["kind"] == "link" and c["area"] == "main" and c["text"]
                 and not (c["text"] in seen or seen.add(c["text"]))]
        menu = [f'{c["text"]} ({c["href"]})' if c["href"] else c["text"]
                for c in snap["clickables"] if c["area"] == "menu" and c["text"]]
        buttons = [c["text"] for c in snap["clickables"] if c["kind"] == "button" and c["area"] == "main" and c["text"]]
        if links:
            lines.append("LINKS: " + " · ".join(links))
        if buttons:
            lines.append("BUTTONS: " + " · ".join(buttons))
        if menu:
            lines.append("MENU: " + " · ".join(menu))
        return "\n".join(lines)

    def _error(self, e: Exception, what: str = "") -> Result:
        msg = str(e).splitlines()[0]
        if "has been closed" in msg or "Target closed" in msg:
            try:
                self.close()
            except Exception:  # the browser is already gone; start clean on the next action
                self._pw = self._browser = self._context = self.page = None
            return Result.error("the browser window was closed (when watching Nova's browser, don't close or click "
                                "in it). It reopens on the next browser action: open the page again.")
        if "ERR_CONNECTION_REFUSED" in msg or "ERR_CONNECTION" in msg:
            return Result.error(f"the web app is not reachable ({msg}). Is it running?")
        if "Timeout" in msg or "timeout" in msg:
            hint = "The page did not respond in time"
            try:
                snap = self._extract()
                if snap["dialogs"]:
                    hint += f'; a dialog "{snap["dialogs"][0]["title"]}" is open and may block the page'
            except PWError:
                pass
            return Result.error(f"{hint}. Look at the page again (browser_open the same path) before retrying.")
        return Result.error(f"browser: {msg}" + (f" ({what})" if what else ""))
