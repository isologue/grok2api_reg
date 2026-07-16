"""Child-process Chromium workflow for the integrated registration console."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

from .console import install_console
install_console()
from DrissionPage import Chromium, ChromiumOptions

from .cpa_queue import CpaExportQueue
from .mail import MailboxPool, VerificationCodeTimeout

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"


class RetryableMailboxError(RuntimeError):
    """A fresh mailbox and browser profile can safely retry this registration step."""


class BrowserRegistration:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.browser = None
        self.page = None
        self.user_data_dir = ""
        self.virtual_display = None
        self.mail_pool = MailboxPool(config["mail"]["providers"], str(config.get("proxy") or ""))
        self.options = ChromiumOptions()
        for argument in ("--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--disable-software-rasterizer"):
            self.options.set_argument(argument)
        proxy = str(config.get("browser_proxy") or config.get("proxy") or "").strip()
        if proxy:
            self.options.set_proxy(proxy)
            print("[browser] 已配置浏览器代理", flush=True)
        browser_paths = (
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        )
        for path in browser_paths:
            if os.path.isfile(path):
                self.options.set_browser_path(path)
                break
        extension = Path(__file__).with_name("turnstile_patch")
        if extension.is_dir():
            self.options.add_extension(str(extension))
        self.options.set_timeouts(base=2)

    @staticmethod
    def _debug_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _start_browser(self, *, start_display: bool = False) -> None:
        # Windows already has a real desktop. Xvfb is Linux-only and would
        # make an explicitly headed host-browser trial fail before Chrome starts.
        if start_display and os.name != "nt" and not os.environ.get("DISPLAY"):
            try:
                from pyvirtualdisplay import Display
                self.virtual_display = Display(visible=0, size=(1920, 1080))
                self.virtual_display.start()
                print("[browser] Xvfb 虚拟显示器已启动", flush=True)
            except Exception as exc:
                raise RuntimeError(f"无法启动 Xvfb 虚拟显示器: {exc}") from exc
        self.user_data_dir = tempfile.mkdtemp(prefix="grok-register-")
        self.options.set_user_data_path(self.user_data_dir)
        self.options.set_local_port(self._debug_port())
        self.browser = Chromium(self.options)
        tabs = self.browser.get_tabs()
        self.page = tabs[-1] if tabs else self.browser.new_tab()
        print("[browser] 浏览器已启动", flush=True)

    def start(self) -> None:
        self._start_browser(start_display=True)

    def _close_browser(self) -> None:
        if self.browser:
            try:
                self.browser.quit()
            except Exception:
                pass
        self.browser = None
        self.page = None
        if self.user_data_dir:
            shutil.rmtree(self.user_data_dir, ignore_errors=True)
            self.user_data_dir = ""

    def restart(self) -> None:
        self._close_browser()
        self._start_browser()

    def close(self) -> None:
        self._close_browser()
        self.mail_pool.close()
        if self.virtual_display:
            try:
                self.virtual_display.stop()
            except Exception:
                pass
            self.virtual_display = None

    def _js(self, script: str, *args: Any) -> Any:
        return self.page.run_js(script, *args)

    def _refresh_active_page(self) -> None:
        """Follow a popup/new-tab handoff made by the xAI signup flow."""
        if not self.browser:
            raise RuntimeError("浏览器未启动")
        try:
            tabs = self.browser.get_tabs()
            candidate = tabs[-1] if tabs else self.browser.new_tab()
            previous_id = getattr(self.page, "tab_id", None)
            candidate_id = getattr(candidate, "tab_id", None)
            if candidate_id != previous_id:
                self.page = candidate
                print("[browser] 已切换到注册流程的最新页面", flush=True)
        except Exception as exc:
            raise RuntimeError(f"刷新当前注册页面失败: {exc}") from exc

    def _turnstile_state(self) -> str:
        return str(self._js("""
            const challenge=document.querySelector('input[name="cf-turnstile-response"]');
            return !challenge ? 'not-found' : (challenge.value ? 'ready' : 'pending');
        """) or "not-found")

    def _turnstile_diagnostics(self) -> dict[str, Any]:
        """Capture widget shape without exposing a response token or page data."""
        try:
            state = self._js("""
              return (() => {
                const visible=(node)=>node&&(()=>{const s=getComputedStyle(node),r=node.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;})();
                const response=document.querySelector('input[name="cf-turnstile-response"]');
                const frames=[...document.querySelectorAll('iframe')]
                  .filter((node)=>/turnstile|challenges[.]cloudflare[.]com/i.test(`${node.src||''} ${node.title||''}`))
                  .map((node)=>({title:String(node.title||'').slice(0,80),visible:visible(node),width:Math.round(node.getBoundingClientRect().width),height:Math.round(node.getBoundingClientRect().height)}));
                return {state:!response?'not-found':(response.value?'ready':'pending'),response_inputs:document.querySelectorAll('input[name="cf-turnstile-response"]').length,widget_frames:frames};
              })();
            """)
            return state if isinstance(state, dict) else {"state": self._turnstile_state()}
        except Exception as exc:
            return {"state": "diagnostic-error", "error": type(exc).__name__}

    def _try_turnstile(self) -> bool:
        """Ask a normally rendered checkbox to run; never fabricate a token."""
        if self._turnstile_state() != "pending":
            return False
        print("[browser] 检测到 Turnstile，正在等待/触发当前页面的正常验证", flush=True)
        try:
            # A CSS locator remains stable when React replaces the hidden field.
            challenge = self.page.ele('css:input[name="cf-turnstile-response"]', timeout=2)
            if not challenge:
                print(f"[browser] Turnstile 控件尚未挂载: {json.dumps(self._turnstile_diagnostics(), ensure_ascii=False, sort_keys=True)}", flush=True)
                return False
            parent = challenge.parent()
            shadow_root = getattr(parent, "shadow_root", None) if parent else None
            iframe = shadow_root.ele("tag:iframe", timeout=2) if shadow_root else None
            body = iframe.ele("tag:body", timeout=2) if iframe else None
            body_shadow_root = getattr(body, "shadow_root", None) if body else None
            checkbox = body_shadow_root.ele("tag:input", timeout=2) if body_shadow_root else None
            if not checkbox:
                print(f"[browser] Turnstile 控件暂不可交互: {json.dumps(self._turnstile_diagnostics(), ensure_ascii=False, sort_keys=True)}", flush=True)
                return False
            iframe.run_js("window.dtp=1;Object.defineProperty(MouseEvent.prototype,'screenX',{value:900});Object.defineProperty(MouseEvent.prototype,'screenY',{value:500});")
            checkbox.click()
            time.sleep(2)
            solved = self._turnstile_state() == "ready"
            print(f"[browser] Turnstile 状态: {'已完成' if solved else '仍未完成'}", flush=True)
            return solved
        except Exception as exc:
            print(f"[browser] Turnstile 控件暂不可用: {type(exc).__name__}; {json.dumps(self._turnstile_diagnostics(), ensure_ascii=False, sort_keys=True)}", flush=True)
            return False

    def _await_turnstile_ready(self, timeout: int = 30) -> bool:
        """Do not submit the profile while xAI's verification token is empty."""
        if self._turnstile_state() != "pending":
            return True
        deadline = time.monotonic() + timeout
        last_report = ""
        while time.monotonic() < deadline:
            if self._turnstile_state() == "ready":
                print("[browser] Turnstile 已完成，继续提交账户资料", flush=True)
                return True
            self._try_turnstile()
            diagnostics = json.dumps(self._turnstile_diagnostics(), ensure_ascii=False, sort_keys=True)
            if diagnostics != last_report:
                print(f"[browser] 等待 Turnstile: {diagnostics}", flush=True)
                last_report = diagnostics
            time.sleep(1)
        print(f"[browser] Turnstile 等待超时: {last_report or json.dumps(self._turnstile_diagnostics(), ensure_ascii=False, sort_keys=True)}", flush=True)
        return False

    def _sso_diagnostics(self) -> dict[str, Any]:
        """Return redacted browser state to make cookie failures actionable."""
        try:
            page_state = self._js("""
              const visible=(node)=>node&&(()=>{const s=getComputedStyle(node),r=node.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;})();
              const clean=(value)=>String(value||'').replace(/[A-Z0-9]{3,4}-[A-Z0-9]{3,4}/ig,'<code>').replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/ig,'<email>').replace(/\\s+/g,' ').trim().slice(0,500);
              const buttons=[...document.querySelectorAll('button,a,[role="button"],input[type="submit"]')].filter(visible).map((node)=>(node.innerText||node.textContent||node.value||'').trim()).filter(Boolean).slice(0,8);
              const inputs=[...document.querySelectorAll('input')].filter(visible).map((node)=>({name:node.name||'',type:node.type||'',testid:node.dataset.testid||''})).slice(0,12);
              const challenge=document.querySelector('input[name="cf-turnstile-response"]');
              return {url:location.origin+location.pathname,title:document.title||'',turnstile:!challenge?'not-found':(challenge.value?'ready':'pending'),buttons,inputs,text:clean(document.body?.innerText)};
            """)
        except Exception as exc:
            page_state = {"state_error": f"{type(exc).__name__}: {exc}"}
        try:
            cookies = [
                {"name": str(cookie.get("name") or ""), "domain": str(cookie.get("domain") or "")}
                for cookie in self.page.cookies(all_info=True, all_domains=True)
                if cookie.get("name")
            ]
            page_state["cookies"] = cookies
        except Exception as exc:
            page_state["cookies_error"] = f"{type(exc).__name__}: {exc}"
        return page_state

    @staticmethod
    def _visible_js() -> str:
        return """const visible=(node)=>node&&(()=>{const s=getComputedStyle(node),r=node.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;})();"""

    def _open_email_flow(self, timeout: int = 25) -> None:
        self.page.get(SIGNUP_URL)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            clicked = self._js('''
              const nodes=[...document.querySelectorAll('button,a,[role="button"]')];
              const target=nodes.find((node)=>{const text=(node.innerText||node.textContent||'').replace(/\\s+/g,'').toLowerCase();return text.includes('signupwithemail')||text.includes('signupemail')||text.includes('continuewithemail')||text.includes('使用邮箱注册');});
              if(!target)return false;target.click();return true;
            ''')
            if clicked:
                return
            time.sleep(0.5)
        raise RuntimeError("未找到“使用邮箱注册”入口")

    def _submit_email(self, address: str, timeout: int = 25) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self._js('''
              const address=arguments[0];
              const visible=(node)=>node&&(()=>{const s=getComputedStyle(node),r=node.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;})();
              const input=[...document.querySelectorAll('input[data-testid="email"],input[name="email"],input[type="email"]')].find(visible);
              if(!input)return 'waiting';
              input.focus();const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')?.set;
              setter?setter.call(input,address):input.value=address;
              input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));
              const button=[...document.querySelectorAll('button')].find((node)=>{const text=(node.innerText||node.textContent||'').replace(/\\s+/g,'').toLowerCase();return text==='注册'||text.includes('signup')||text.includes('sign up')||text.includes('next')||text.includes('继续');});
              if(!button||button.disabled||button.getAttribute('aria-disabled')==='true')return 'filled';button.click();return 'submitted';
            ''', address)
            if result == "submitted":
                return
            time.sleep(0.7)
        raise RetryableMailboxError("填写/提交邮箱超时")

    def _wait_for_code_page(self, timeout: int = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if "verify" in self.page.url.lower() or "code" in self.page.url.lower():
                return
            found = self._js('''return !!document.querySelector('input[data-input-otp="true"],input[name="code"],input[autocomplete="one-time-code"]')''')
            if found:
                return
            text = str(self._js("return document.body ? document.body.innerText : ''") or "").lower()
            if "too many requests" in text or "rate limited" in text:
                raise RuntimeError("xAI 已触发频率限制")
            if any(marker in text for marker in ("rejected", "已被拒绝", "使用其他邮箱", "use another email", "use a different email")):
                raise RetryableMailboxError("邮箱域名被 xAI 拒绝")
            time.sleep(1)
        raise RetryableMailboxError("未进入验证码页面")

    def _submit_code(self, code: str, timeout: int = 45) -> None:
        code = "".join(char for char in code.upper() if char.isalnum())
        deadline = time.monotonic() + timeout
        submitted = False
        while time.monotonic() < deadline:
            self._refresh_active_page()
            if self._has_profile_form():
                return
            filled = self._js('''
              const code=arguments[0];
              const visible=(node)=>node&&(()=>{const s=getComputedStyle(node),r=node.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;})();
              const put=(node,value)=>{const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')?.set;setter?setter.call(node,value):node.value=value;node.dispatchEvent(new Event('input',{bubbles:true}));node.dispatchEvent(new Event('change',{bubbles:true}));node.dispatchEvent(new Event('blur',{bubbles:true}));};
              const single=[...document.querySelectorAll('input[data-input-otp="true"],input[name="code"],input[autocomplete="one-time-code"]')].find(visible);
              if(single){put(single,code);return true;}
              const boxes=[...document.querySelectorAll('input')].filter((node)=>visible(node)&&(node.maxLength===1||node.getAttribute('inputmode')==='numeric'));
              if(boxes.length>=code.length){[...code].forEach((char,index)=>put(boxes[index],char));return true;}return false;
            ''', code)
            if filled and not submitted:
                submitted = True
                print(f"[run] 验证码已填入: {code}", flush=True)
                self._js('''
                  const visible=(node)=>node&&(()=>{const s=getComputedStyle(node),r=node.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;})();
                  const nodes=[...document.querySelectorAll('button,[role="button"],input[type="submit"]')].filter(visible);
                  const button=nodes.find((node)=>/verify|验证|next|继续/i.test(node.innerText||node.textContent||node.value||''));
                  if(button&&!button.disabled&&button.getAttribute('aria-disabled')!=='true'){button.click();return true;}
                  return false;
                ''')
            text = str(self._js("return document.body ? document.body.innerText : ''") or "").lower()
            if "too many requests" in text or "rate limited" in text:
                raise RuntimeError("验证码提交后被上游限流")
            if "rejected" in text or "已被拒绝" in text:
                raise RetryableMailboxError("验证码提交后邮箱被 xAI 拒绝")
            time.sleep(1)
        raise RetryableMailboxError("验证码已提交但未进入账户资料页")

    def _has_profile_form(self) -> bool:
        return bool(self._js('''
          const visible=(node)=>node&&(()=>{const s=getComputedStyle(node),r=node.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;})();
          const selectors=['input[data-testid="givenName"]','input[name="givenName"]','input[autocomplete="given-name"]'];
          return selectors.some((selector)=>[...document.querySelectorAll(selector)].some(visible));
        '''))

    def _read_sso_cookie(self) -> str | None:
        """Read a completed SSO cookie without logging its value."""
        try:
            for cookie in self.page.cookies(all_info=True, all_domains=True):
                if str(cookie.get("name") or "").strip().lower() == "sso" and cookie.get("value"):
                    return str(cookie["value"])
        except Exception:
            return None
        return None

    def _profile_submission_state(self) -> dict[str, Any]:
        """Return a redacted state used to confirm the profile form really advanced."""
        try:
            state = self._js("""
              const visible=(node)=>node&&(()=>{const s=getComputedStyle(node),r=node.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;})();
              const clean=(value)=>String(value||'').replace(/[A-Z0-9]{3,4}-[A-Z0-9]{3,4}/ig,'<code>').replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/ig,'<email>').replace(/\\s+/g,' ').trim().slice(0,240);
              const profileSelectors=['input[data-testid="givenName"]','input[name="givenName"]','input[autocomplete="given-name"]'];
              const profileForm=profileSelectors.some((selector)=>[...document.querySelectorAll(selector)].some(visible));
              const errors=[...document.querySelectorAll('[role="alert"],[aria-live="assertive"],[data-testid*="error"],.error')]
                .filter(visible).map((node)=>clean(node.innerText||node.textContent)).filter(Boolean).slice(0,4);
              const turnstile=document.querySelector('input[name="cf-turnstile-response"]');
              const submit=[...document.querySelectorAll('button,input[type="submit"]')].find((node)=>visible(node)&&(/complete sign up|finish|create account|sign up/i.test(node.innerText||node.textContent||node.value||'')||node.type==='submit'));
              const fields=[...document.querySelectorAll('input[name="givenName"],input[name="familyName"],input[name="password"]')]
                .filter(visible).map((node)=>({name:node.name||'',valid:node.checkValidity(),value_length:(node.value||'').length}));
              return {profile_form:profileForm,url:location.origin+location.pathname,errors,turnstile:!turnstile?'not-found':(turnstile.value?'ready':'pending'),submit:submit?{disabled:!!submit.disabled,aria_disabled:submit.getAttribute('aria-disabled'),form_valid:submit.form?submit.form.checkValidity():null}:null,fields};
            """)
            return state if isinstance(state, dict) else {"profile_form": self._has_profile_form()}
        except Exception as exc:
            return {"profile_form": self._has_profile_form(), "state_error": type(exc).__name__}

    def _find_profile_submit_button(self) -> Any | None:
        """Prefer a real browser click so React receives the normal pointer flow."""
        for locator in (
            "@@type=submit",
            "text:Complete sign up",
            "text:Finish",
            "text:Create account",
            "text:Sign up",
        ):
            try:
                button = self.page.ele(locator, timeout=2)
            except Exception:
                continue
            if button:
                return button
        return None

    def _click_profile_submit(self) -> str:
        button = self._find_profile_submit_button()
        if button:
            try:
                waiter = getattr(button, "wait", None)
                if waiter:
                    waiter.enabled(timeout=5)
                button.click(timeout=3)
                print("[profile] 已通过浏览器原生点击触发提交", flush=True)
                return "native"
            except Exception as exc:
                print(f"[profile] 原生提交点击失败，改用页面兜底: {type(exc).__name__}", flush=True)

        result = self._js("""
          const visible=(node)=>node&&(()=>{const s=getComputedStyle(node),r=node.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;})();
          const nodes=[...document.querySelectorAll('button,[role="button"],input[type="submit"]')].filter(visible);
          const button=nodes.find((node)=>{
            const text=(node.innerText||node.textContent||node.value||'').replace(/\\s+/g,' ').trim().toLowerCase();
            return /complete sign up|完成注册|finish|create account|sign up|create/i.test(text) || node.type==='submit';
          });
          if(!button)return 'not-found';
          if(button.disabled||button.getAttribute('aria-disabled')==='true')return 'disabled';
          const form=button.form||button.closest('form');
          if(form&&typeof form.requestSubmit==='function'){form.requestSubmit(button);return 'request-submitted';}
          button.click();
          return 'js-clicked';
        """)
        if result in {"request-submitted", "js-clicked"}:
            print("[profile] 已通过页面兜底触发提交", flush=True)
        return str(result or "not-found")

    @staticmethod
    def _is_transient_page_transition(exc: Exception) -> bool:
        """Whether Chromium is replacing the document after a successful submit."""
        message = f"{type(exc).__name__}: {exc}".lower()
        return "contextlost" in message or "page disconnected" in message or "页面已被刷新" in str(exc)

    def _await_profile_submission(self, timeout: int = 15) -> None:
        """Wait for the form to advance before entering the SSO-cookie wait loop."""
        deadline = time.monotonic() + timeout
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                self._refresh_active_page()
                if self._read_sso_cookie():
                    print("[profile] 提交后已直接获得 SSO Cookie", flush=True)
                    return
                state = self._profile_submission_state()
            except Exception as exc:
                if self._is_transient_page_transition(exc):
                    # A native submit can swap React documents before the old
                    # CDP execution context is released. That is expected and
                    # must not turn a completed signup into a failed mailbox.
                    print("[profile] 提交后页面正在跳转，继续等待 SSO Cookie", flush=True)
                    time.sleep(0.75)
                    continue
                raise
            last_state = state
            errors = state.get("errors")
            if errors:
                raise RetryableMailboxError(f"账户资料提交被页面拒绝: {'?'.join(map(str, errors))}")
            if not state.get("profile_form"):
                print("[profile] 资料表单已离开，继续等待 SSO Cookie", flush=True)
                return
            time.sleep(0.75)
        print(
            "[profile] 提交后仍停留在资料页: "
            f"{json.dumps(last_state or self._sso_diagnostics(), ensure_ascii=False, sort_keys=True)}",
            flush=True,
        )
        raise RetryableMailboxError("账户资料提交后仍停留在注册表单")

    def _submit_profile(self, timeout: int = 45) -> dict[str, str]:
        profile = {
            "given_name": f"G{secrets.token_hex(3)}",
            "family_name": f"R{secrets.token_hex(3)}",
            "password": "N" + secrets.token_hex(5) + "!a7#" + secrets.token_urlsafe(6),
        }
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._refresh_active_page()
            ready = self._js('''
              const [given,family,password]=arguments;
              const visible=(node)=>node&&(()=>{const s=getComputedStyle(node),r=node.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;})();
              const put=(selector,value)=>{const node=[...document.querySelectorAll(selector)].find((item)=>visible(item)&&!item.disabled);if(!node)return false;const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')?.set;setter?setter.call(node,value):node.value=value;node.dispatchEvent(new Event('input',{bubbles:true}));node.dispatchEvent(new Event('change',{bubbles:true}));node.dispatchEvent(new Event('blur',{bubbles:true}));return true;};
              return put('input[name="givenName"],input[data-testid="givenName"],input[autocomplete="given-name"]',given)&&put('input[name="familyName"],input[data-testid="familyName"],input[autocomplete="family-name"]',family)&&put('input[name="password"],input[data-testid="password"],input[type="password"]',password);
            ''', profile["given_name"], profile["family_name"], profile["password"])
            if not ready:
                time.sleep(0.75)
                continue
            if not self._await_turnstile_ready():
                raise RetryableMailboxError("账户资料页 Turnstile 未完成，未提交表单")
            result = self._click_profile_submit()
            if result in {"native", "request-submitted", "js-clicked"}:
                self._await_profile_submission()
                print("[run] 账户资料已确认提交，正在等待 SSO Cookie", flush=True)
                return profile
            print(f"[run] 账户资料提交按钮尚未可用: {result}", flush=True)
            time.sleep(1)
        raise RetryableMailboxError("账户资料提交失败或按钮始终不可用")

    def _wait_for_sso(self, timeout: int = 120) -> str:
        deadline = time.monotonic() + timeout
        next_report = 0.0
        last_state = ""
        while time.monotonic() < deadline:
            self._refresh_active_page()
            sso = self._read_sso_cookie()
            if sso:
                print("[sso] 已捕获 SSO Cookie", flush=True)
                return sso
            text = str(self._js("return document.body ? document.body.innerText : ''") or "").lower()
            if "access denied" in text or "too many requests" in text or "rate limited" in text:
                raise RuntimeError("注册完成后被上游拦截或限流")
            if self._turnstile_state() == "pending":
                self._try_turnstile()
            now = time.monotonic()
            if now >= next_report:
                state = self._sso_diagnostics()
                marker = json.dumps(state, ensure_ascii=False, sort_keys=True)
                if marker != last_state:
                    print(f"[sso] 等待 SSO 的当前页面状态: {marker}", flush=True)
                    last_state = marker
                next_report = now + 15
            time.sleep(3)
        state = self._sso_diagnostics()
        print(f"[sso] SSO 超时现场: {json.dumps(state, ensure_ascii=False, sort_keys=True)}", flush=True)
        raise RetryableMailboxError("等待 SSO Cookie 超时")

    @staticmethod
    def _oauth_field_name(name: str) -> str | None:
        normalized = "".join(char for char in str(name).lower() if char.isalnum())
        aliases = {
            "clientid": "client_id", "accesstoken": "access_token", "refreshtoken": "refresh_token",
            "idtoken": "id_token", "tokentype": "token_type", "scope": "scope",
            "expiresat": "expires_at", "expiresin": "expires_in", "email": "email",
            "userid": "user_id", "principalid": "principal_id", "teamid": "team_id",
        }
        return aliases.get(normalized)

    @staticmethod
    def _jwt_claims(token: Any) -> dict[str, Any]:
        """Read non-verified JWT claims only to fill missing archive metadata."""
        value = str(token or "")
        parts = value.split(".")
        if len(parts) != 3:
            return {}
        try:
            import base64
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
            return decoded if isinstance(decoded, dict) else {}
        except Exception:
            return {}

    def _collect_oauth_artifacts(self) -> dict[str, Any]:
        """Collect known OAuth-shaped values from the completed browser session."""
        found: dict[str, Any] = {}
        try:
            raw = self._js(r'''
              return (() => {
                const wanted=new Set(['clientid','accesstoken','refreshtoken','idtoken','tokentype','scope','expiresat','expiresin','email','userid','principalid','teamid']);
                const out={}; const norm=(key)=>String(key||'').toLowerCase().replace(/[^a-z0-9]/g,'');
                const take=(key,value)=>{const name=norm(key);if(!wanted.has(name)||value===null||value===undefined||typeof value==='object')return;out[name]=String(value);};
                const walk=(value,depth=0)=>{if(depth>4||value===null||value===undefined)return;if(Array.isArray(value)){value.forEach((item)=>walk(item,depth+1));return;}if(typeof value==='object'){Object.entries(value).forEach(([key,item])=>{take(key,item);walk(item,depth+1)});}};
                for(const storage of [window.localStorage,window.sessionStorage]){
                  for(let i=0;i<storage.length;i++){const key=storage.key(i);const value=storage.getItem(key);take(key,value);try{walk(JSON.parse(value||''));}catch(_){}}
                }
                return out;
              })();
            ''')
            if isinstance(raw, dict):
                for key, value in raw.items():
                    field = self._oauth_field_name(key)
                    if field and value not in (None, ""):
                        found[field] = value
        except Exception:
            pass
        try:
            for cookie in self.page.cookies(all_info=True, all_domains=True):
                field = self._oauth_field_name(str(cookie.get("name") or ""))
                value = cookie.get("value")
                if field and value not in (None, "") and field not in found:
                    found[field] = str(value)
        except Exception:
            pass
        for token_field in ("id_token", "access_token"):
            for claim_key, claim_value in self._jwt_claims(found.get(token_field)).items():
                field = self._oauth_field_name(claim_key)
                if field and claim_value not in (None, "") and field not in found:
                    found[field] = claim_value
        if "expires_in" in found:
            try:
                found["expires_in"] = int(float(found["expires_in"]))
            except (TypeError, ValueError):
                found.pop("expires_in", None)
        return found

    def run_one(self, *, mailbox_attempts: int = 5, code_timeout_sec: int = 120) -> dict[str, Any]:
        """Register one account, replacing the mailbox after address-level failures.

        A rejected domain or a mailbox that never receives the OTP must not
        consume the user's requested registration count. Each retry starts a
        fresh browser profile and asks the mailbox pool for a new address.
        """
        attempts = max(1, mailbox_attempts)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            mailbox = self.mail_pool.acquire()
            print(
                f"[run] 第 {attempt}/{attempts} 次邮箱尝试: {mailbox.address}",
                flush=True,
            )
            try:
                self._open_email_flow()
                self._submit_email(mailbox.address)
                self._wait_for_code_page()
                try:
                    code = self.mail_pool.wait_for_code(
                        mailbox.address,
                        mailbox.token(),
                        timeout=code_timeout_sec,
                    )
                except VerificationCodeTimeout as exc:
                    raise RetryableMailboxError(str(exc)) from exc
                self._submit_code(code)
                profile = self._submit_profile()
                sso = self._wait_for_sso()
                oauth = self._collect_oauth_artifacts()
                if not oauth.get("email"):
                    oauth["email"] = mailbox.address
                return {"email": mailbox.address, "sso": sso, "oauth": oauth, **profile}
            except RetryableMailboxError as exc:
                last_error = exc
                print(
                    f"[run] 邮箱 {mailbox.address} 不可用: {exc}",
                    flush=True,
                )
                if attempt >= attempts:
                    break
                print("[run] 正在更换邮箱并重置浏览器指纹后重试", flush=True)
                self.restart()
                time.sleep(1)

        raise RuntimeError(f"连续 {attempts} 次邮箱尝试均失败: {last_error}") from last_error


def import_registered_accounts(config: dict[str, Any], accounts: list[dict[str, Any]]) -> None:
    if not accounts:
        return
    api = config.get("api") or {}
    endpoint, token = str(api.get("endpoint") or ""), str(api.get("token") or "")
    if not endpoint or not token:
        raise RuntimeError("账号池导入接口未配置")
    payload = [
        {
            "token": str(item.get("sso") or ""),
            "email": str(item.get("email") or ""),
            "password": str(item.get("password") or ""),
            "oauth": item.get("oauth") if isinstance(item.get("oauth"), dict) else {},
            "provider": "grok_build",
        }
        for item in accounts
        if item.get("sso")
    ]
    headers = {"Authorization": f"Bearer {token}"}
    if endpoint.rstrip("/").endswith("/tokens/add"):
        # Compatibility with pre-archive standalone runner configurations.
        account = config.get("account") or {}
        response = requests.post(endpoint, headers=headers, json={"tokens": [item["token"] for item in payload], "pool": account.get("pool") or "basic", "tags": account.get("tags") or []}, timeout=60)
    else:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    data = response.json()
    print(f"[import] 账号已导入账号池：新增 {data.get('count', 0)} 个，跳过 {data.get('skipped', 0)} 个", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    count = int((config.get("run") or {}).get("count") or 1)
    if not 1 <= count <= 100:
        raise SystemExit("run.count 必须在 1 到 100 之间")
    worker = BrowserRegistration(config)
    run_settings = config.get("run") or {}
    mailbox_attempts = int(run_settings.get("mailbox_attempts") or 5)
    code_timeout_sec = int(run_settings.get("code_timeout_sec") or 120)
    collected: list[dict[str, Any]] = []
    cpa_queue = CpaExportQueue(config)
    try:
        worker.start()
        for current in range(1, count + 1):
            print(f"[run] 开始第 {current}/{count} 轮注册", flush=True)
            try:
                result = worker.run_one(
                    mailbox_attempts=mailbox_attempts,
                    code_timeout_sec=code_timeout_sec,
                )
                collected.append(result)
                cpa_queue.submit(result, worker.page)
                print(f"[run] 第 {current} 轮成功: {result['email']}", flush=True)
            except Exception as exc:
                print(f"[run] 第 {current} 轮失败: {type(exc).__name__}: {exc}", flush=True)
            if current < count:
                worker.restart()
    finally:
        try:
            cpa_queue.drain()
            import_registered_accounts(config, collected)
        finally:
            worker.close()
    print(f"[run] 注册任务结束：成功 {len(collected)}/{count}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
