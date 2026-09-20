# -*- coding: utf-8 -*-
"""
浏览器辅助登录（V3）—— 不绕过 MFA，只把 MFA 那一步交回给人。

账号开了多因子认证时，CAS 不会直接发 token。这个模块的做法是：
  1. 开一个真实浏览器（Edge/Chrome）
  2. 自动填好学号密码并提交（密码仍由页面自己的 JS 加密，我们不插手）
  3. 卡在 MFA 页面时**停下来等**，用户在浏览器里扫码 / 收短信完成验证
  4. 验证通过后 CAS 会回调带 szblTK 的地址，我们捕获它 —— 这就是 token
  5. 关掉浏览器，返回 token

全程使用浏览器（优先用随 exe 打包的内置 Chromium，其次系统已装的 Edge/Chrome），
不注入、不破解。内置 Chromium 让没有装任何浏览器、或系统浏览器在冻结包里
起不来的环境也能正常弹窗。

【V4.0】设备信任持久化
  以前每次都 new_context() 开全新上下文，关掉浏览器 cookie 就没了，
  CAS 的「信任此设备」白勾 —— 于是每次登录都得再过一次企业微信。
  现在改成**每个学号一个固定的浏览器档案目录**（launch_persistent_context）：
  勾了「信任此设备」之后，那个 cookie 和 CAS 会话会留在磁盘上，
  下次再登就直接带着 szblTK 跳回来，什么都不用点。
"""
import base64 as _b64
import os
import re
import shutil
import sys
import threading
import time

# 【V3.1】与 booker.CAS_SERVICE 保持一致：service 必须带 base64url 回跳后缀，
# 否则 MFA 通过后后端无处可跳，浏览器会停在 404 页上拿不到 token。
_CAS_REDIRECT_B64 = _b64.urlsafe_b64encode(b'/user').decode('ascii').rstrip('=')
CAS_URL = ('https://authserver.hainanu.edu.cn/authserver/login'
           '?service=https://hdscs.hainanu.edu.cn/hdsc/app/' + _CAS_REDIRECT_B64)
AUTH_ORIGIN = 'https://authserver.hainanu.edu.cn'
TOKEN_RE = re.compile(r'szblTK=([A-Za-z0-9._\-]{20,})')

# 登录表单选择器。
# 注意：页面里同时存在 4 个表单（fidoLogin / dynamicLogin / qrLogin / userNameLogin），
# #username 和 #login_submit 都各有两个，所以必须限定在密码表单 #pwdFromId 作用域内，
# 否则会填错表单、点错按钮。
PWD_FORM = '#pwdFromId'
SEL_PWD_DIV = '#pwdLoginDiv'
SEL_USER = '#pwdFromId #username'
SEL_PASS = '#pwdFromId #password'
SEL_SUBMIT = '#pwdFromId #login_submit'
SEL_SALT = '#pwdFromId #pwdEncryptSalt'
SEL_CAPTCHA_DIV = '#pwdFromId #captchaDiv'
SEL_CAPTCHA = '#pwdFromId #captcha'

# 把「账号密码登录」整个区块连同所有祖先容器一起显示出来。
# 只改 #pwdLoginDiv 自身没用 —— 祖先还藏着，输入框对 Playwright 来说依然
# 是「不可见」，fill() 会超时，自动填表就 silently 失败了。
FORCE_SHOW_JS = """() => {
  const target = document.querySelector('#pwdFromId')
               || document.querySelector('#pwdLoginDiv');
  if (!target) return {ok: false, why: '找不到 #pwdFromId 和 #pwdLoginDiv'};
  let n = target, deep = 0;
  while (n && n.tagName && n.tagName !== 'HTML') {
    n.style.setProperty('display', 'block', 'important');
    n.style.setProperty('visibility', 'visible', 'important');
    n.style.setProperty('opacity', '1', 'important');
    n.style.setProperty('height', 'auto', 'important');
    n = n.parentElement; deep++;
  }
  document.querySelectorAll('#pwdFromId, #pwdFromId *').forEach(e => {
    e.style.setProperty('display', 'block', 'important');
    e.style.setProperty('visibility', 'visible', 'important');
  });
  return {ok: true, deep: deep};
}"""

# 提交登录。
# 【为什么不能只是「点一下登录按钮」】实测下来 CAS 这个页面有两个坑：
#   1) 「登录」按钮的 click 事件只绑在 phoneFromId 那个表单的 <a> 上，
#      #pwdFromId 里的按钮压根没有事件处理器 —— 点了毫无反应；
#   2) 隐藏字段 #cllt 的默认值是 fidoLogin，checkForm() 因此直接跳过
#      「用户名密码」分支，连校验都不做。
# 所以这里自己走一遍页面原本的流程：明文填进 #password → 用它自己的
# encryptPassword() 算好密文塞进 #saltPassword → 明文框 disable 掉
# （和页面自己提交前一模一样，明文不会上网）→ cllt 改成 userNameLogin → 提交。
JS_SUBMIT_LOGIN = """([u, p]) => {
  const f = document.querySelector('#pwdFromId');
  if (!f) return 'no #pwdFromId';
  const uEl = f.querySelector('#username');
  const pEl = f.querySelector('#password');
  const saltEl = document.querySelector('#pwdEncryptSalt');
  const spEl = document.querySelector('#saltPassword');
  if (!uEl || !pEl || !saltEl || !spEl) return '表单字段不全';
  uEl.value = u; pEl.value = p;
  let enc = null;
  try { enc = encryptPassword(p, saltEl.value); } catch (e) { enc = null; }
  if (!enc) return 'encryptPassword 调用失败';
  spEl.value = enc;
  try { pEl.setAttribute('disabled', 'disabled'); } catch (e) {}
  const cllt = document.querySelector('#cllt');
  if (cllt) cllt.value = 'userNameLogin';
  f.submit();
  return 'ok';
}"""

# 万一上面的展开还是没让输入框可见（页面结构改了），用它兜底：
# 直接给 input 赋值并派发 input/change，页面自己的加密逻辑照旧会跑。
JS_SET_VALUES = """([u, p]) => {
  const set = (sel, v) => {
    const el = document.querySelector(sel);
    if (!el) return false;
    el.focus(); el.value = v;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.blur(); return true;
  };
  return set('#pwdFromId #username', u) && set('#pwdFromId #password', p);
}"""

HAS_PLAYWRIGHT = False
_PW_ERR = ''
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception as e:
    _PW_ERR = str(e)


def browser_available():
    return HAS_PLAYWRIGHT, _PW_ERR


# 系统浏览器常见安装位置。用 channel='msedge' 时 Playwright 自己也会找，
# 这里只是为了在报错时能把「你机器上其实有 Edge」这件事说清楚。
_BROWSER_PATHS = {
    'Edge': [
        r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
        r'%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe',
    ],
    'Chrome': [
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe',
    ],
}


def find_browsers():
    """返回 {'Edge': '路径', 'Chrome': '路径'}，没装的不出现。"""
    out = {}
    for name, paths in _BROWSER_PATHS.items():
        for p in paths:
            p = os.path.expandvars(p)
            if p and os.path.isfile(p):
                out[name] = p
                break
    return out


def browser_env():
    """把「浏览器模块为什么不可用」这件事一次性说清楚，省得瞎猜。"""
    bundled = _bundled_chrome_exe()
    return {
        'python': sys.executable,
        'version': sys.version.split()[0],
        'frozen': bool(getattr(sys, 'frozen', False)),
        'playwright': HAS_PLAYWRIGHT,
        'error': '' if HAS_PLAYWRIGHT else _PW_ERR,
        'bundled_chromium': bool(bundled),
        'browsers': find_browsers(),
    }


def _first_existing(paths):
    """在候选路径里返回第一个真实存在的文件，找不到返回 ''。"""
    for p in paths:
        p = os.path.expandvars(p)
        if p and os.path.isfile(p):
            return p
    return ''


def _bundled_chrome_exe():
    """内置 Chromium 的可执行文件（打进 exe 或放在项目目录下）。

    打包时通过 --add-data 把整个 chrome-win 目录塞进 exe，运行时
    PyInstaller 会把它们解包到 sys._MEIPASS；源码直接跑时则在项目根的
    bundled_chromium/ 下。返回找到的路径，找不到返回 ''。
    """
    roots = []
    # 1) 单文件 exe 若用 --add-data 把浏览器打进了包内，解包目录在 _MEIPASS
    if getattr(sys, 'frozen', False) and getattr(sys, '_MEIPASS', ''):
        roots.append(sys._MEIPASS)
    # 2) 发布目录里 exe 旁边的兄弟文件夹（首选：避免每次启动都解包几百 MB）
    try:
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    except Exception:
        pass
    # 3) 源码直接跑时，项目根 = src/ 的上级
    try:
        roots.append(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
    except Exception:
        pass
    for r in roots:
        for sub in ('chrome-win64', 'chrome-win'):
            cand = os.path.join(r, 'bundled_chromium', sub, 'chrome.exe')
            if os.path.isfile(cand):
                return cand
    return ''


def _browser_attempts(headless=False):
    """生成浏览器启动的尝试清单，按可靠性排序。

    【V4.0 内置浏览器版】首选内置的 Chromium（随 exe 一起打包，不依赖
    用户机器上是否装了 Edge/Chrome），其次才是系统浏览器，最后兜底用
    Playwright 的 channel（冻结包里偶尔解析失灵，但聊胜于无）。
    """
    out = []
    bundled = _bundled_chrome_exe()
    if bundled:
        out.append((None, bundled))     # 内置 Chromium，最稳
    edge = _first_existing(_BROWSER_PATHS['Edge'])
    chrome = _first_existing(_BROWSER_PATHS['Chrome'])
    if edge:
        out.append((None, edge))        # 直接指系统 Edge 可执行文件
    if chrome:
        out.append((None, chrome))      # 直接指系统 Chrome 可执行文件
    out.append(('msedge', None))        # 兜底：channel
    out.append(('chrome', None))
    out.append((None, None))            # 兜底：Playwright 自带 chromium
    return out


def _launch(pw, log, headless=False):
    """优先用系统 Edge，其次 Chrome，最后 Playwright 自带的 chromium。"""
    for ch, exe in _browser_attempts(headless):
        try:
            kw = {'headless': headless}
            if not headless:
                kw['args'] = ['--start-maximized', '--no-first-run',
                              '--no-default-browser-check']
            if ch:
                kw['channel'] = ch
            elif exe:
                kw['executable_path'] = exe
            b = pw.chromium.launch(**kw)
            tag = os.path.basename(exe) if exe else (ch or 'chromium')
            log(f'  浏览器已启动（{tag}，{"无头" if headless else "可见"}）')
            return b
        except Exception as e:
            tag = os.path.basename(exe) if exe else (ch or 'chromium')
            msg = f'  {tag} 启动失败：{str(e)[:90]}'
            log(msg)
            print('[browserlogin] ' + msg, file=sys.stderr, flush=True)
    print('[browserlogin] 所有浏览器启动方式都失败了', file=sys.stderr, flush=True)
    return None


def reveal_pwd_form(page, log=print):
    """把默认隐藏的「账号密码登录」区块显示出来。

    CAS 默认停在扫码登录，#pwdLoginDiv 是 display:none，页面上也没有可点的
    切换 tab，所以直接改样式，但不碰表单提交逻辑 —— 密码仍由页面自己的 JS
    （encryptPassword + pwdEncryptSalt）加密。

    【关键点】只把 #pwdLoginDiv 设成 display:block 是不够的：它的**祖先容器**
    同样是隐藏的，Playwright 仍判定输入框不可见，fill() 会一直等到超时，
    表现就是「浏览器开了，但学号密码没自动填，得自己敲」。
    所以这里要沿着 DOM 一路向上把 display/visibility 全部放开。
    """
    try:
        res = page.evaluate(FORCE_SHOW_JS)
        if not res or not res.get('ok'):
            why = str((res or {}).get('why') or '未知原因')[:60]
            log(f'  × 展开登录区块失败：{why}')
            return False
        log(f'  已展开账号密码登录区块（向上放开 {res.get("deep")} 层）')
        return True
    except Exception as e:
        log(f'  × 展开登录区块失败：{str(e)[:80]}')
        return False


def probe_page(log=print, timeout=40):
    """只读探测：确认登录表单的选择器是否对得上。不填表、不提交。"""
    if not HAS_PLAYWRIGHT:
        return {'ok': False, 'error': f'Playwright 不可用：{_PW_ERR}'}
    info = {}
    try:
        with sync_playwright() as pw:
            b = _launch(pw, log, headless=True)
            if b is None:
                return {'ok': False, 'error': '无法启动任何浏览器'}
            page = b.new_page(locale='zh-CN')
            page.goto(CAS_URL, timeout=timeout * 1000, wait_until='domcontentloaded')
            page.wait_for_timeout(1500)
            info['url'] = page.url
            info['title'] = page.title()

            reveal_pwd_form(page, log)
            page.wait_for_timeout(400)

            for name, sel in (('username', SEL_USER), ('password', SEL_PASS),
                              ('submit', SEL_SUBMIT), ('salt', SEL_SALT),
                              ('form', PWD_FORM)):
                try:
                    loc = page.locator(sel).first
                    info[name] = {'count': page.locator(sel).count(),
                                  'visible': loc.is_visible()}
                except Exception as e:
                    info[name] = {'error': str(e)[:80]}
            # 加密盐的值（16 位，用于 AES key）
            try:
                info['salt_value'] = page.eval_on_selector(
                    SEL_SALT, "e => (e.value||'').slice(0,20)") if page.locator(
                    SEL_SALT).count() else ''
            except Exception:
                info['salt_value'] = ''
            # 验证码当前是否需要（hide 表示不用）
            try:
                info['captcha_needed'] = page.eval_on_selector(
                    SEL_CAPTCHA_DIV, "e => !e.className.includes('hide')")
            except Exception:
                info['captcha_needed'] = False
            b.close()
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    info['ok'] = True
    return info


def _cookies_for_playwright(cookiejar):
    """requests 的 cookiejar → Playwright 的 add_cookies 格式。"""
    out = []
    for c in cookiejar:
        item = {'name': c.name, 'value': c.value,
                'path': c.path or '/'}
        dom = (c.domain or '').lstrip('.')
        if dom:
            item['domain'] = dom
        else:
            item['url'] = AUTH_ORIGIN
        if c.expires:
            item['expires'] = int(c.expires)
        out.append(item)
    return out


def _echo_page_hint(page, log):
    """把认证页上「要你做什么」的原话打出来，省得猜。"""
    try:
        txt = page.eval_on_selector('body', 'e => (e.innerText||"")')
    except Exception:
        return
    txt = re.sub(r'\n{2,}', '\n', txt or '').strip()
    if not txt:
        return
    keep = []
    for ln in txt.split('\n'):
        ln = ln.strip()
        if not ln or ln in ('学校官网', '|', '操作指南'):
            continue
        if any(k in ln for k in (
                '你好', '多因子', '认证', '验证', '扫码', '短信', '邮箱',
                '企业微信', '可信', '个人中心', '偏好设置', '确认', '登录')):
            keep.append(ln)
        if len(keep) >= 8:
            break
    if keep:
        for ln in keep:
            log('      | ' + ln[:70])


def _net_hook(bucket):
    """构造 Playwright 网络监听器：任何请求/响应 URL 里出现 szblTK 立刻收下。

    V3 用 0.6 秒轮询 page.url，302 一闪而过就错过了 —— 这就是
    「MFA 通过了却拿不到 token」的直接原因之一。V3.1 改成事件驱动。
    """
    def on_req(req):
        if bucket.get('token'):
            return
        m = TOKEN_RE.search(req.url or '')
        if m:
            bucket['token'] = m.group(1)
        else:
            m2 = re.search(r'szblTK=(1401|1402)(?:&|$)', req.url or '')
            if m2:
                bucket['err_code'] = m2.group(1)

    def on_resp(resp):
        on_req(resp)

    return on_req, on_resp


def _scan_storage(page, log):
    """兜底：SPA 登录成功后会把 token 放进 localStorage（pinia 持久化）。"""
    try:
        return page.evaluate(
            '''() => {
                let hit = null;
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    const v = localStorage.getItem(k) || '';
                    const m = v.match(/szblTK=([A-Za-z0-9._\\-]{20,})/)
                           || v.match(/"token"\\s*:\\s*"([A-Za-z0-9._\\-]{40,})"/);
                    if (m) { hit = m[1]; break; }
                }
                return hit;
            }''')
    except Exception:
        return None


def wait_for_token(page, timeout, log, stop, bucket=None, quiet=False,
                   exclude=None):
    """在浏览器里等 MFA 完成 + token 出现。返回 (token, err)。

    quiet=True 用于「先悄悄看几秒会不会自己跳过去」的场景（设备信任免验证），
    不打 URL 流水账，超时也不算错误。

    exclude：这个账号**上一次**的 token。固定档案浏览器的 localStorage 里
    会留着上一次登录的旧 token，页面刚打开时就能扫到 —— 如果不排除它，
    「重新获取」会一路顺风地拿回那个已经失效的旧值，
    表现为「刷新完了还是不可用」。网络层捕获的不受此限制
    （那一定是本次跳转才产生的）。
    """
    deadline = time.time() + timeout
    seen_mfa = False
    err_checked = False
    fallback_used = False
    hdscs_since = None
    warned_stale = False
    last = ''
    while time.time() < deadline:
        if stop():
            return None, '已中止'
        # ① 网络层最先看（最及时，302 也不会漏）
        if bucket and bucket.get('token'):
            tok = bucket['token']
            log(f'  ✓ 从网络层捕获 token（{len(tok)} 字符）')
            return tok, None
        if bucket and bucket.get('err_code'):
            code = bucket['err_code']
            return None, (f'平台返回错误码 {code}（1401/1402）：学号可能尚未绑定'
                          '订场平台账号。请先在微信里正常进一次小程序再回来重试。')
        try:
            url = page.url or ''
        except Exception:
            break
        if url != last:
            last = url
            if 'reAuth' in url or 'multifactor' in url.lower():
                if not seen_mfa:
                    seen_mfa = True
                    log('      → 已进入多因子认证页，等待你手动完成…')
            elif not quiet:
                log(f'      → {url.split("?")[0][-42:]}')
        # ② URL / 页面 / localStorage 三处兜底
        m = TOKEN_RE.search(url)
        if not m:
            try:
                m2 = TOKEN_RE.search(page.content())
                if m2:
                    m = m2
            except Exception:
                pass
        if not m:
            # 注意：_scan_storage 返回的是 token 字符串本身，不是正则匹配对象，
            # 不能和上面的 m 混用（否则会 'str' object has no attribute 'group'）。
            st = _scan_storage(page, log)
            if st and st == exclude and not warned_stale:
                warned_stale = True
                log('  · 扫到的是上一次留下的旧 token，继续等新的…')
            if st and st != exclude:
                log(f'  ✓ 从 localStorage 捕获到 token（{len(st)} 字符）')
                return st, None
        if m:
            tok = m.group(1)
            log(f'  ✓ 捕获到 token（{len(tok)} 字符）')
            return tok, None
        me = re.search(r'szblTK=(1401|1402)(?:&|$)', url)
        if me:
            return None, (f'平台返回错误码 {me.group(1)}（1401/1402）：学号可能'
                          '尚未绑定订场平台账号。请先在微信里正常进一次小程序'
                          '再回来重试。')
        # ③ 卡在 hdscs 的 404 页（没带 szblTK）→ 主动走 H5 登录入口兜底一次
        if 'hdscs.hainanu.edu.cn' in url and 'authserver' not in url:
            if hdscs_since is None:
                hdscs_since = time.time()
            elif (not fallback_used and time.time() - hdscs_since > 12
                  and not (bucket or {}).get('token')):
                fallback_used = True
                log('      ⚠ 落在后端页且迟迟没有 token，改走 H5 登录入口重试…')
                try:
                    page.goto('https://hdscw.hainanu.edu.cn/login',
                              timeout=30000, wait_until='domcontentloaded')
                    hdscs_since = time.time()
                except Exception as e:
                    log(f'      × 兜底跳转失败：{str(e)[:70]}')
        else:
            hdscs_since = None
        try:
            body = page.content()
            # 出错早点说，别让人干等
            if not err_checked and time.time() > deadline - timeout + 15:
                err_checked = True
                for sel in ('#showErrorTip', '#showWarnTip', '#formErrorTip'):
                    try:
                        if not page.locator(sel).count():
                            continue
                        tip = (page.eval_on_selector(
                            sel, 'e => (e.innerText||"").trim()') or '')
                        if tip and len(tip) < 60:
                            log(f'  ! 页面提示：{tip}')
                            return None, tip
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(0.6)
    return None, f'{timeout} 秒内没等到认证完成（MFA 超时）'


def _open_context(pw, log, profile_dir=None, headless=False):
    """开浏览器上下文，返回 (context, close_callable)。

    profile_dir 非空 → 用持久档案 launch_persistent_context：
      关掉浏览器后 cookie / localStorage 全留在磁盘上，
      CAS 的「信任此设备」和登录会话得以跨次复用。
    profile_dir 为空 → 退回原来的一次性 new_context（什么都不留）。
    """
    if profile_dir:
        try:
            os.makedirs(profile_dir, exist_ok=True)
        except Exception as e:
            log(f'  ! 档案目录建不出来：{str(e)[:60]}')
        # 清掉上一次没正常关闭留下的 SingletonLock，否则 Chromium 会拒绝打开这个档案
        _lock = os.path.join(profile_dir, 'SingletonLock')
        if os.path.exists(_lock):
            try:
                os.remove(_lock)
                log('  · 清掉了上次遗留的浏览器锁文件')
            except Exception:
                pass
        # 关键修复：必须显式把 headless 写出来。
        # Playwright 的 launch_persistent_context 默认 headless=True —— 之前
        # 只在 headless=True 时才设该参数，False 时干脆不设，于是浏览器
        # 以"无头"模式静默启动：进程在跑、日志说"已打开"，但根本没有可见窗口。
        kw = {'locale': 'zh-CN', 'viewport': None,
              'headless': bool(headless),
              'args': ['--start-maximized', '--no-first-run',
                       '--no-default-browser-check', '--no-sandbox',
                       '--disable-gpu', '--disable-dev-shm-usage']}
        last_err = ''
        for ch, exe in _browser_attempts(headless):
            try:
                k = dict(kw)
                if ch:
                    k['channel'] = ch
                elif exe:
                    k['executable_path'] = exe
                ctx = pw.chromium.launch_persistent_context(profile_dir, **k)
                tag = os.path.basename(exe) if exe else (ch or 'chromium')
                log(f'  浏览器已启动（{tag} · 固定档案）')
                print(f'[browserlogin] 已用 {tag} 打开持久档案浏览器',
                      file=sys.stderr, flush=True)
                return ctx, ctx.close
            except Exception as e:
                last_err = str(e)[:160]
                tag = os.path.basename(exe) if exe else (ch or 'chromium')
                msg = f'  {tag} 启动失败：{last_err}'
                log(msg)
                print('[browserlogin] ' + msg, file=sys.stderr, flush=True)
        print(f'[browserlogin] 持久档案模式所有浏览器都启动失败，最后错误：{last_err}',
              file=sys.stderr, flush=True)
        return None, None
    b = _launch(pw, log, headless=headless)
    if b is None:
        return None, None
    ctx = b.new_context(viewport=None, locale='zh-CN')
    return ctx, b.close


def _fill_and_submit(page, username, password, log):
    """自动填学号密码并提交。密码仍由页面自己的 JS 加密，我们只做填表。

    注意：页面里 #username / #login_submit 各有多个同名元素（4 个表单并存），
    Playwright 严格模式下必须 .first，否则会报 strict mode violation。
    """
    try:
        if not page.locator(SEL_USER).count():
            log('  × 页面上找不到学号输入框（页面结构可能变了）')
            return False
        # fill() 要求元素可见，给个短超时，卡住就立刻换脚本兜底，
        # 别让人对着空白的登录页干等 30 秒。
        try:
            page.locator(SEL_USER).first.fill(username, timeout=6000)
            page.locator(SEL_PASS).first.fill(password, timeout=6000)
        except Exception:
            ok = page.evaluate(JS_SET_VALUES, [username, password])
            if not ok:
                log('  × 学号密码填不进去（输入框既不可见也改不动）')
                return False
            log('  · 输入框仍不可见，已用脚本填入（加密照样由页面完成）')
        log('  ✓ 学号密码已自动填好')
        # 提交：走页面自己的加密 + 原生提交（见 JS_SUBMIT_LOGIN 的说明）
        try:
            r = page.evaluate(JS_SUBMIT_LOGIN, [username, password])
        except Exception as e:
            r = f'提交脚本异常：{str(e)[:60]}'
        if r == 'ok':
            log('  ✓ 已提交（密码由页面自己的 JS 加密）')
            return True
        log(f'  ! 常规提交没成（{r}），改用点击登录按钮…')
        # 兜底：把明文框恢复可编辑，再点按钮 / 直接调 startLogin
        try:
            page.eval_on_selector(
                SEL_PASS, "e => e.removeAttribute('disabled')")
        except Exception:
            pass
        if page.locator(SEL_SUBMIT).count():
            try:
                page.locator(SEL_SUBMIT).first.click(timeout=6000)
                log('  ✓ 已点登录按钮')
                return True
            except Exception:
                try:
                    page.evaluate(
                        "() => { const b = document.querySelector("
                        "'#pwdFromId #login_submit');"
                        " if (window.startLogin) startLogin(b); else b.click(); }")
                    log('  ✓ 已调用页面的 startLogin()')
                    return True
                except Exception as e:
                    log(f'  × 提交失败：{str(e)[:80]}')
                    return False
        return False
    except Exception as e:
        log(f'  × 自动填表失败：{str(e)[:80]}')
        return False


def _looks_like_mfa(page):
    try:
        u = page.url or ''
    except Exception:
        return False
    return ('reAuth' in u) or ('multifactor' in u.lower())


def _persistent_login(username, password, profile_dir, timeout, log, stop,
                      old_token=None):
    """用固定档案走完整登录：能免验证就免验证，不能就自动填表 + 等你过 MFA。

    第一次：自动填学号密码 → 卡在 MFA → 你手动过一次（可以勾「信任此设备」）
    之后  ：档案里已有信任 cookie / CAS 会话 → 打开就直接带 szblTK 回来，零操作
    """
    try:
        with sync_playwright() as pw:
            ctx, closer = _open_context(pw, log, profile_dir=profile_dir)
            if ctx is None:
                return None, '无法启动浏览器（需要系统装有 Edge 或 Chrome）'
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                bucket = {'token': None, 'err_code': None}
                on_req, on_resp = _net_hook(bucket)
                page.on('request', on_req)
                page.on('response', on_resp)

                log('  打开统一身份认证页…')
                try:
                    page.goto(CAS_URL, timeout=60000, wait_until='domcontentloaded')
                except Exception as e:
                    return None, f'打开认证页失败：{str(e)[:70]}'
                page.wait_for_timeout(1200)

                # ① 先安静地等几秒：档案里若是「已信任设备」，会自己带 token 跳回来
                #    这里要把上一次的旧 token 排除掉，否则拿回来的还是那个失效的
                tok, _ = wait_for_token(page, 10, log, stop, bucket=bucket,
                                        quiet=True, exclude=old_token)
                if tok:
                    log('  ✓ 设备信任生效，本次免验证直接拿到 token')
                    return tok, None
                if stop():
                    return None, '已中止'

                # ② 没能直接过 → 自动填学号密码提交
                if username and password:
                    reveal_pwd_form(page, log)
                    page.wait_for_timeout(400)
                    if not _fill_and_submit(page, username, password, log):
                        log('  → 请在浏览器里手动输入学号密码')
                else:
                    log('  → 请在浏览器里手动输入学号密码')

                page.wait_for_timeout(1500)
                _echo_page_hint(page, log)
                if _looks_like_mfa(page):
                    log('      → 已进入多因子认证页，等你手动完成'
                        '（企业微信 / 短信 / 扫码任选，建议勾「信任此设备」）')
                log('  → 完成后会自动收回 token'
                    + (f'，最多等 {timeout // 60} 分钟' if timeout >= 60
                       else f'，最多等 {timeout} 秒'))
                return wait_for_token(page, timeout, log, stop, bucket=bucket,
                                      exclude=old_token)
            finally:
                try:
                    closer()
                except Exception:
                    pass
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'


def clear_profile(profile_dir, log=print):
    """清掉某个账号的浏览器档案（换电脑 / 想重新验证时用）。"""
    if not profile_dir or not os.path.isdir(profile_dir):
        return False
    try:
        shutil.rmtree(profile_dir)
        log(f'  已清除浏览器档案：{profile_dir}')
        return True
    except Exception as e:
        log(f'  × 清除失败：{str(e)[:80]}')
        return False


def browser_mfa(mfa_url, cookiejar=None, timeout=300, log=print, stop=None,
                profile_dir=None):
    """把已登录到 MFA 那一步的 CAS 会话搬进浏览器，剩下的交给人。

    不绕过 MFA：学号密码已由服务端验证通过，这里只是把「扫码 / 收短信」
    这一步用真实浏览器呈现出来，用户在里面点完，我们收 token。
    """
    if not HAS_PLAYWRIGHT:
        return None, f'未装 Playwright：{_PW_ERR}'
    stop = stop or (lambda: False)
    try:
        with sync_playwright() as pw:
            ctx, closer = _open_context(pw, log, profile_dir=profile_dir)
            if ctx is None:
                return None, '无法启动浏览器（需要系统装有 Edge 或 Chrome）'

            if cookiejar and not profile_dir:
                try:
                    ctx.add_cookies(_cookies_for_playwright(cookiejar))
                    log('  已把登录会话带进浏览器（不用再输一次学号密码）')
                except Exception as e:
                    log(f'  ! 会话注入失败（{str(e)[:60]}），可能需要重新登录')

            page = ctx.new_page()
            # V3.1：网络层事件监听，302 一闪而过也能抓住 szblTK
            bucket = {'token': None, 'err_code': None}
            on_req, on_resp = _net_hook(bucket)
            page.on('request', on_req)
            page.on('response', on_resp)
            log('  打开多因子认证页…')
            try:
                page.goto(mfa_url, timeout=60000, wait_until='domcontentloaded')
            except Exception as e:
                log(f'  × 打开失败：{str(e)[:100]}')
                try:
                    closer()
                except Exception:
                    pass
                return None, f'打开认证页失败：{str(e)[:60]}'
            page.wait_for_timeout(1500)
            _echo_page_hint(page, log)
            log('  → 完成后会自动收回 token，最多等 '
                + (f'{timeout // 60} 分钟' if timeout >= 60 else f'{timeout} 秒'))

            tok, err = wait_for_token(page, timeout, log, stop, bucket=bucket)
            try:
                closer()
            except Exception:
                pass
            return tok, err
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'


def browser_login(username, password, timeout=300, log=print,
                  stop=None, mfa_hint=None, cas_session=None, mfa_url=None,
                  profile_dir=None, old_token=None):
    """完整登录：先在服务端过学号密码，MFA 交给浏览器里的人。

    优先复用调用方传进来的 CAS 会话（cas_session / mfa_url），
    避免重复提交一次密码。没有就自己走一遍 CAS。

    【V4.0】profile_dir 非空时改走「固定档案」路径：
    整件事都在那个档案里做（免验证直通 → 自动填表 → 等你过 MFA），
    勾过的「信任此设备」会留在磁盘上，下次就不用再过一次。
    """
    import booker as _b
    stop = stop or (lambda: False)
    if profile_dir:
        log('[1/3] 使用固定浏览器档案（上次勾的「信任此设备」还在）…')
        tok, err = _persistent_login(username, password, profile_dir,
                                     timeout, log, stop, old_token=old_token)
        if tok:
            log('[3/3] ✓ 完成')
        return tok, err
    log('[1/3] 用学号密码过统一身份认证…')
    if not (cas_session and mfa_url):
        tok, err, extra = _b.api_cas_login(username, password)
        if tok:
            log('  ✓ 没开多因子认证，直接拿到 token')
            return tok, None
        if err != _b.MFA_REQUIRED:
            return None, err or 'CAS 登录失败'
        cas_session = (extra or {}).get('session')
        mfa_url = (extra or {}).get('url')
    if not mfa_url:
        return None, '拿不到多因子认证页地址'
    log('  ✓ 密码正确，账号开了多因子认证')
    log('[2/3] 打开浏览器…')
    tok, err = browser_mfa(mfa_url, cookiejar=cas_session.cookies if cas_session else None,
                           timeout=timeout, log=log, stop=stop)
    if tok:
        log('[3/3] ✓ 完成')
    return tok, err
