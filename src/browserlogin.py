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
import os
import re
import shutil
import sys
import threading
import time

import booker as _bk

# 【V4.1 架构清理】CAS 地址的唯一真源收归 booker.py。
#
# 以前这里自己 base64 算一遍回跳后缀，booker.py 那边也各算一遍
# （AUTH_BASE + CAS_LOGIN_PATH + CAS_APP_BASE + _b64url_path），
# 两边拼出的是同一个 .../hdsc/app/L3VzZXI。平台一旦改回跳路径就得改两处，
# 漏一处的症状是「浏览器能登进去，但服务端拿不到 token」——极难排查。
# 现在统一从 booker 派生，只此一份。
AUTH_ORIGIN = _bk.AUTH_BASE
CAS_LOGIN_URL = _bk.AUTH_BASE + _bk.CAS_LOGIN_PATH
CAS_SERVICE = _bk.CAS_SERVICE          # https://hdscs.hainanu.edu.cn/hdsc/app/L3VzZXI

# 登录页（默认渲染扫码登录）
CAS_URL = CAS_LOGIN_URL + '?service=' + CAS_SERVICE

# 【V4.1】直跳「账号密码登录」的地址。
# 页面上那个「账号登录」其实是个 <a id='userNameLogin_a'>，href 就是
#   /authserver/login?type=userNameLogin&service=...
# 实测：直接 goto 这个地址，CAS 会把账号密码表单渲染成**主表单** ——
#   · #pwdFromId / #username / #password / #login_submit 天然可见（约 0.9s）
#   · 隐藏字段 #cllt 直接就是 userNameLogin（不用我们再改）
#   · 验证码 div 自带 hide（不用输验证码）
#   · 页面上只剩 2 个 form，不再有 4 个同名 #username 的歧义
# 而不带 type 打开时，#pwdLoginDiv 和 SECTION.main 两层 display:none 把表单
# 藏得死死的，只能靠 FORCE_SHOW_JS 硬改样式 —— 那正是「要手动点切换、
# 有时不自动填」的根源。
CAS_PWD_URL = CAS_LOGIN_URL + '?type=userNameLogin&service=' + CAS_SERVICE

# 【V4.1】「可以下手填表了」的判定：账号框和密码框都真的可见。
# 顺带看验证码 —— 若验证码框冒出来了（连续输错后 CAS 会开），
# 就不要自作主张提交，交回给人。
JS_LOGIN_READY = """() => {
  const g = s => document.querySelector(s);
  const vis = e => !!e && !!(e.offsetWidth || e.offsetHeight
                             || e.getClientRects().length);
  const u = g('#pwdFromId #username');
  const p = g('#pwdFromId #password');
  const cp = g('#pwdFromId #captcha') || g('#captcha');
  const cd = g('#captchaDiv');
  const captchaOn = vis(cp) || (!!cd && !String(cd.className || '').includes('hide')
                                && vis(cd));
  return {ready: vis(u) && vis(p), captcha: captchaOn};
}"""
TOKEN_RE = re.compile(r'szblTK=([A-Za-z0-9._\-]{20,})')

# 进入应用域之后，给 SPA 留多少秒把新 token 写进 localStorage，
# 在这之前不采信 localStorage 里的值（那里大概率还是上一次的旧值）。
# 实测 SPA 从拿到 szblTK 到落盘约 0.4~1 秒，取 2 秒留足余量。
STORAGE_GRACE_SEC = 2.0

# 【V4.2 修复「账号四拿到旧 token」——页面内捕获脚本】
#
# 实测（2026-09-24 实地观察账号四）发现：新 token 并不是在某个网络请求里
# 出现的，而是出现在 **URL 的 hash fragment** 上：
#
#   https://hdscw.hainanu.edu.cn/#/login?redirect=L3VzZXI&szblTK=<新token>
#
# fragment（# 后面的东西）**根本不会发到服务器** —— 所以网络层钩子
# （request / response）永远看不到它，再怎么监听都没用。
#
# 而这一跳只存在约 0.4 秒（0.8s 出现 → 1.2s 被 SPA 改成 #/user），
# 靠 Python 侧 0.6 秒一轮的 page.url 轮询去撞，运气不好就整个错过。
# 错过之后唯一剩下的来源是 localStorage，而那儿躺着的可能还是上一次
# 留下的旧值 —— 于是「刷新成功」了，拿回来的 token 却是 401。
# 账号四的「刷新完还是 401」就是这样来的。
#
# 所以改成**浏览器端实时捕获**：往每个页面注入这段脚本，
# 它自己盯着 hashchange / popstate / pushState / replaceState，
# 一看到 URL 里冒出 szblTK 就存进 window.__TKCAP。
# Python 侧再来取，就不会受轮询粒度影响。
JS_TK_CAPTURE = """(() => {
  if (window.__TKCAP) return;
  window.__TKCAP = {tok: null, url: null, at: 0};
  const BAD = ['1401', '1402'];
  const scan = (u) => {
    try {
      const m = String(u || '').match(/szblTK=([A-Za-z0-9._\\-]{20,})/);
      if (!m) return;
      if (BAD.indexOf(m[1]) >= 0) { window.__TKCAP.bad = m[1]; return; }
      window.__TKCAP.tok = m[1];
      window.__TKCAP.url = String(u).split('?')[0];
      window.__TKCAP.at = Date.now();
    } catch (e) {}
  };
  scan(location.href);
  window.addEventListener('hashchange', () => scan(location.href));
  window.addEventListener('popstate', () => scan(location.href));
  for (const fn of ['pushState', 'replaceState']) {
    const orig = history[fn];
    if (typeof orig !== 'function') continue;
    history[fn] = function () {
      const r = orig.apply(this, arguments);
      setTimeout(() => scan(location.href), 0);
      return r;
    };
  }
})();"""


def _install_token_capture(page):
    """给页面装上「URL 里一出现 token 就记下来」的钩子。

    必须用 add_init_script：它在**每个新文档创建时、页面自己的脚本之前**
    执行，所以 hdscw 的 SPA 一加载（哪怕第一跳就是 #/login?szblTK=…）
    立刻就能扫到，不吃 Python 侧轮询的粒度。
    """
    try:
        page.add_init_script(JS_TK_CAPTURE)
        return True
    except Exception:
        return False


def _read_captured_token(page, bucket=None):
    """读页面里捕获到的 token（含 hash fragment 里的那一个）。

    读到就顺手写进 bucket，让上层「网络层已拿到」的判断也成立。
    """
    try:
        d = page.evaluate('() => (window.__TKCAP || {})')
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    tok = d.get('tok')
    if bucket is not None and isinstance(bucket, dict):
        if d.get('bad') and not bucket.get('err_code'):
            bucket['err_code'] = d.get('bad')
        if tok and not bucket.get('token'):
            bucket['token'] = tok
    return tok or None

# 登录表单选择器。
# 注意：页面里同时存在 4 个表单（fidoLogin / dynamicLogin / qrLogin / userNameLogin），
# #username 和 #login_submit 都各有两个，所以必须限定在密码表单 #pwdFromId 作用域内，
# 否则会填错表单、点错按钮。
PWD_FORM = '#pwdFromId'
SEL_USER = '#pwdFromId #username'
SEL_PASS = '#pwdFromId #password'
SEL_SUBMIT = '#pwdFromId #login_submit'
SEL_SALT = '#pwdFromId #pwdEncryptSalt'
SEL_CAPTCHA_DIV = '#pwdFromId #captchaDiv'

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

# 只填学号（没存密码时用）：把人最容易敲错的 11 位数字先填好，
# 剩下的密码交给用户 —— 比整个表单都空着等手输要省事。
JS_SET_USER = """(u) => {
  const el = document.querySelector('#pwdFromId #username');
  if (!el) return false;
  el.focus(); el.value = u;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  el.blur(); return true;
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


def _storage_token_decision(st, exclude, trust_storage):
    """localStorage 里扫到的 token 收不收。返回 (收吗, 备注)。

    单独抽成纯函数，好让 selftest 不装浏览器也能测 —— 这段判断就是
    「账号四卡在 #/user」的症结所在，必须有断言钉着。

    备注含义：'' 不用说、'stale' 像旧值先不收、'same' 和上次一样但该收。
    """
    if not st:
        return False, ''
    if st == exclude and not trust_storage:
        return False, 'stale'
    if st == exclude:
        return True, 'same'
    return True, ''


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


def _close_watcher(page, ctx=None):
    """监听浏览器被**人**手动关掉。返回一个 dict，closed['v']=True 即已关闭。

    【为什么需要】用户直接点窗口的 × 时，Playwright 这边的等待循环还在
    一秒一秒地睡 —— 不监听的话会干等到超时（最长十几分钟），期间任务槽
    一直被占着，界面全部按钮都锁死，只能把整个程序关掉。注册 page 和
    context 两个 close 事件：关窗口两个都会触发，双保险。
    """
    closed = {'v': False}

    def mark(*_a, **_k):
        closed['v'] = True

    for target in (page, ctx):
        if target is None:
            continue
        try:
            target.on('close', mark)
        except Exception:
            pass
    return closed


def _is_closed(closed, page=None):
    """closed 标记 / page.is_closed() 任一成立都算浏览器已关。"""
    if closed and closed.get('v'):
        return True
    if page is not None:
        try:
            return bool(page.is_closed())
        except Exception:
            return True      # 连 is_closed 都调不通，基本就是已经断了
    return False


# 浏览器被手动关闭时的统一说辞
ERR_BROWSER_CLOSED = '浏览器窗口已被关闭，登录未完成（重新点一次登录即可）'


def wait_for_token(page, timeout, log, stop, bucket=None, quiet=False,
                   exclude=None, closed=None, trust_storage=False):
    """在浏览器里等 MFA 完成 + token 出现。返回 (token, err)。

    quiet=True 用于「先悄悄看几秒会不会自己跳过去」的场景（设备信任免验证），
    不打 URL 流水账，超时也不算错误。

    exclude：这个账号**上一次**的 token。固定档案浏览器的 localStorage 里
    会留着上一次登录的旧 token，页面刚打开时就能扫到 —— 如果不排除它，
    「重新获取」会一路顺风地拿回那个已经失效的旧值，
    表现为「刷新完了还是不可用」。网络层捕获的不受此限制
    （那一定是本次跳转才产生的）。

    trust_storage：**本轮确实发生过一次登录**时为 True。

    【V4.2 实测修正 —— 这个参数存在的理由】
    海大这个平台发的 token 是 **HS512 + payload 只有 login_user_key**，
    没有 exp / iat / jti / 随机数。**同一个账号无论登多少次，拿回来的
    184 字符串一模一样。** 也就是说「新 token」和「上一次留下的旧 token」
    在数值上根本无法区分。

    于是「按值排除」这条规则有个致命副作用：一旦本次登录的 token 只出现在
    localStorage（没走网络层 URL），它必然等于 exclude，被当成旧值丢弃 ——
    程序就永远等下去。实测账号四正是这样：设备信任失效 → 改走密码登录 →
    落到 hdscw/#/user → 扫到的永远「像是旧的」→ 干等到超时/用户手动关窗。

    所以改成**按事件**判断而不是按值判断：
      还没确认本轮登录发生过 → 仍然排除（保住原来的防呆）
      已经登录过了（自动提交成功 / 人手动完成 / 过了 MFA）→ 收下，
      哪怕它和上次一模一样 —— 那恰恰说明登录成功了，服务端会话已刷新。

    closed：_close_watcher() 的返回值。用户手动关掉浏览器窗口时立刻收工，
    不再干等到超时。
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
        if _is_closed(closed, page):
            log('  × 检测到浏览器窗口已被关闭')
            return None, ERR_BROWSER_CLOSED
        # ① 网络层最先看（最及时，302 也不会漏）
        if bucket and bucket.get('token'):
            tok = bucket['token']
            log(f'  ✓ 从网络层捕获 token（{len(tok)} 字符）')
            return tok, None
        # ② 页面内捕获：URL 的 hash fragment 里那个 szblTK 只有这里抓得到。
        #    fragment 不上网（网络层钩子看不见），而且只存在约 0.4 秒，
        #    Python 侧 0.6 秒一轮的轮询会整个错过 —— 错过就只能去读
        #    localStorage，而那儿可能是上一轮留下的旧值（表现为刷新完还是 401）。
        cap = _read_captured_token(page, bucket)
        if cap:
            log(f'  ✓ 从页面路由捕获到 token（{len(cap)} 字符）')
            return cap, None
        if bucket and bucket.get('err_code'):
            code = bucket['err_code']
            return None, (f'平台返回错误码 {code}（1401/1402）：学号可能尚未绑定'
                          '订场平台账号。请先在微信里正常进一次小程序再回来重试。')
        try:
            url = page.url or ''
        except Exception:
            # 读不到 URL 基本都是窗口刚被关掉
            log('  × 浏览器连接断开（窗口可能已被关闭）')
            return None, ERR_BROWSER_CLOSED
        if url != last:
            last = url
            if 'reAuth' in url or 'multifactor' in url.lower():
                if not seen_mfa:
                    seen_mfa = True
                    log('      → 已进入多因子认证页，等待你手动完成…')
            elif not quiet:
                # 以前这里 split('?')[0] 把 fragment 连同 szblTK 一起砍掉了，
                # 排查「token 到底出现在哪一跳」时日志里什么都看不到。
                # 现在打码后再截，能一眼看出是哪一跳带来的新 token。
                shown = TOKEN_RE.sub('szblTK=<新token>', url)
                log('      → …' + shown[-52:])
        # 「已经进了应用域多久」——给 localStorage 判定用（见下）。
        # 原来这个计时只在下面的兜底分支里维护，localStorage 判定时它还
        # 是 None，没法拿来做「SPA 写入宽限」的依据。
        if (('hdscs.hainanu.edu.cn' in url or 'hdscw.hainanu.edu.cn' in url)
                and 'authserver' not in url):
            if hdscs_since is None:
                hdscs_since = time.time()
        else:
            hdscs_since = None
        # ③ URL / 页面 / localStorage 三处兜底
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
            take, note = _storage_token_decision(st, exclude, trust_storage)
            # 【V4.2 修复「账号四拿到旧 token」】刚踏进应用域的头 2 秒里，
            # localStorage 里躺着的**多半还是上一次留下的旧值** —— SPA 要等
            # 拿到 szblTK 之后才会把新值写进去（实测约 0.4~1 秒）。
            # 这一段就是「刷新成功了、token 拿回来了、一校验却是 401」的来源。
            # URL / 页面内捕获已经抢在前面了，所以这里多等 2 秒代价极小：
            # 等的是 SPA 落盘，不是白等。
            if take and hdscs_since is not None and \
                    time.time() - hdscs_since < STORAGE_GRACE_SEC:
                take = False
                note = ''
            if note == 'stale':
                # 本轮还没确认登录发生过 —— 这可能是档案里躺着的旧值，先别收
                if not warned_stale:
                    warned_stale = True
                    log('  · 扫到的是上一次留下的旧 token，继续等新的…')
            elif take:
                if note == 'same':
                    # 已经确认本轮登录成功，而该平台对同一账号发的 token 是固定的
                    log('  · 本次登录成功；该平台对同一账号发的 token 是固定的，'
                        '所以这个串和上次一致（服务端会话已刷新）')
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
        # ③ 卡在应用页（没带 szblTK）→ 主动再走一次 CAS，别干等到超时。
        #
        # 【V4.2 实测修正】原来这里只认 hdscs（后端域），可**实际卡住的是
        # hdscw（H5 前端）的 #/user**：密码登录成功后浏览器就停在那儿，
        # 兜底条件永远不成立，于是从「卡 8 秒」变成「卡到超时 / 用户手动关窗」。
        # 现在两个域都算「停在应用页」。
        #
        # 另外重试目标从 hdscw/login 改成 CAS 入口：此刻 CAS 里已经有刚用
        # 密码换来的 TGT，再走一次 service 跳转必然重新签发一次票据，
        # 比指望 SPA 自己想起来重新认证可靠得多。
        # 计时统一在上面的「进入应用域」处维护，这里只管触发。
        if (hdscs_since is not None and not fallback_used
                and time.time() - hdscs_since > 8
                and not (bucket or {}).get('token')):
            fallback_used = True
            log('      ⚠ 停在应用页却一直没拿到 token，'
                '主动再走一次统一身份认证…')
            try:
                page.goto(CAS_URL, timeout=30000,
                          wait_until='domcontentloaded')
                # 跳走后 URL 变成 authserver，循环开头会把 hdscs_since 清成
                # None；等它再跳回应用域时重新开始计时（宽限期重新生效）。
                hdscs_since = None
            except Exception as e:
                log(f'      × 兜底跳转失败：{str(e)[:70]}')
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


def _fill_and_submit(page, username, password, log, submit=True):
    """自动填学号密码并提交。密码仍由页面自己的 JS 加密，我们只做填表。

    注意：页面里 #username / #login_submit 各有多个同名元素（4 个表单并存），
    Playwright 严格模式下必须 .first，否则会报 strict mode violation。

    submit=False 时只填不交（页面要求验证码时用）。
    """
    try:
        if not page.locator(SEL_USER).count():
            log('  × 页面上找不到学号输入框（页面结构可能变了）')
            return False
        # fill() 要求元素可见，给个短超时，卡住就立刻换脚本兜底，
        # 别让人对着空白的登录页干等 30 秒。
        try:
            page.locator(SEL_USER).first.fill(username, timeout=4000)
            page.locator(SEL_PASS).first.fill(password, timeout=4000)
        except Exception:
            ok = page.evaluate(JS_SET_VALUES, [username, password])
            if not ok:
                log('  × 学号密码填不进去（输入框既不可见也改不动）')
                return False
            log('  · 输入框仍不可见，已用脚本填入（加密照样由页面完成）')
        log('  ✓ 学号密码已自动填好')
        if not submit:
            return True          # 只要填，不提交（等用户补验证码）
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


# CAS 判定失败时页面上的常见说法（命中任意一个就说明这把凭据不对）
# 说法以实测为准：海大 CAS 输错时页面上写的是
# 「该账号非常用账号或用户名密码有误」，所以「有误」这一类必须涵盖到。
_LOGIN_ERR_WORDS = ('用户名或密码错误', '密码错误', '账号或密码错误',
                    '凭证错误', '用户名密码错误', '账号不存在',
                    '用户不存在', '密码不正确', '账号已被锁定',
                    '用户名密码有误', '用户名或密码有误', '密码有误',
                    '非常用账号',
                    'Authentication failure', 'Invalid credentials')


def _login_error_text(page):
    """提交后页面里有没有「密码不对」之类的提示；有就返回那句话，没有返回 ''. """
    try:
        txt = page.inner_text('body') or ''
    except Exception:
        return ''
    txt = txt[:4000]
    for w in _LOGIN_ERR_WORDS:
        if w in txt:
            # 顺手把提示那一小段抠出来，日志里一眼能看懂
            i = txt.find(w)
            return txt[max(0, i - 12): i + len(w) + 12].strip().replace('\n', ' ')
    return ''


def _looks_like_mfa(page):
    try:
        u = page.url or ''
    except Exception:
        return False
    return ('reAuth' in u) or ('multifactor' in u.lower())


def _profile_has_session(profile_dir):
    """档案里有没有留下过浏览器会话 —— 判断「信任此设备」是否可能生效。

    全新档案（第一次登录）里不可能有信任 cookie，那就没必要等信任宽限期，
    直接开填，省下那 2.5 秒。
    """
    try:
        if not profile_dir or not os.path.isdir(profile_dir):
            return False
        # Cookies 的位置随 Chromium 版本/目录布局变：
        #   老布局 <profile>/Cookies
        #   新布局 <profile>/Default/Cookies 或 <profile>/Default/Network/Cookies
        # 所以这里递归找，别硬编码路径（漏了就会把已信任的老档案当成新档案，
        # 跳过宽限期 → 设备信任再也不生效）。
        for root, dirs, files in os.walk(profile_dir):
            dirs[:] = [d for d in dirs if d not in
                       ('Cache', 'Code Cache', 'GPUCache', 'ShaderCache',
                        'Crashpad', 'blob_storage')]
            if 'Cookies' in files:
                p = os.path.join(root, 'Cookies')
                if os.path.getsize(p) > 0:
                    return True
            if len(root) > len(profile_dir) + 60:
                break
    except Exception:
        pass
    return False


def _wait_before_fill(page, grace, log, stop, bucket, closed=None):
    """在「信任宽限期」内等免验证直通 —— 只等 grace 秒，不再盲等 10 秒。

    【为什么改】旧实现是固定 wait_for_token(10 秒) 盲等：不管档案里有没有
    「信任此设备」，都得先干等 10 秒才动手填表。实测一次登录 14.4 秒里
    有 10 秒纯耗在这上面 —— 这就是「多账号登录很慢」的直接原因。

    这里改成：只看两件事，谁先到走谁
      · 网络层出现 szblTK        → 'token'（信任生效，直接收工）
      · 页面已经离开 CAS 登录页  → 'left'（多半进了 MFA，交给 wait_for_token）
    都没有 → 'timeout'，立刻自己动手填，不浪费时间。
    """
    deadline = time.time() + max(0.0, float(grace or 0))
    while True:
        if stop():
            return 'stopped'
        if _is_closed(closed, page):
            return 'closed'
        if bucket.get('token'):
            return 'token'
        # 免验证直通时，token 可能出现在 URL 的 hash 里（fragment 不上网，
        # 网络层钩子看不到），所以这里也要问一次页面内捕获。
        if _read_captured_token(page, bucket):
            return 'token'
        if bucket.get('err_code'):
            return 'err'
        try:
            u = page.url or ''
        except Exception:
            return 'closed'
        # 必须是真实的 http 页面才算「离开登录页」—— about:blank / chrome-error
        # 之类的过渡态不算，否则会误判成 left，跳过自动填表干等超时。
        if u.startswith('http') and ('authserver' not in u
                                     or '/authserver/login' not in u):
            return 'left'
        if time.time() >= deadline:
            return 'timeout'
        page.wait_for_timeout(100)


def _wait_form_ready(page, timeout, log, closed=None):
    """等账号密码框真正可见。返回 (可以下手吗, 'captcha' 或 None)。

    'captcha' 表示页面把验证码放出来了 —— 这时候不能闷头提交，
    得把学号密码填好、交回给用户补验证码。
    """
    deadline = time.time() + max(0.5, float(timeout or 5))
    why = None
    while time.time() < deadline:
        if _is_closed(closed, page):
            return False, 'closed'
        try:
            d = page.evaluate(JS_LOGIN_READY)
        except Exception:
            d = None
        if d:
            if d.get('ready'):
                return True, None
            if d.get('captcha'):
                why = 'captcha'
        page.wait_for_timeout(120)
    return False, why


def _persistent_login(username, password, profile_dir, timeout, log, stop,
                      old_token=None, trust_grace=2.5, headless=False):
    """用固定档案走完整登录：能免验证就免验证，不能就自动填表 + 等你过 MFA。

    第一次：自动填学号密码 → 卡在 MFA → 你手动过一次（可以勾「信任此设备」）
    之后  ：档案里已有信任 cookie / CAS 会话 → 打开就直接带 szblTK 回来，零操作

    【V4.1 提速】
      ① 不再盲等 10 秒，只等 trust_grace 秒（默认 2.5s）看信任是否生效；
      ② 需要自己登时，直跳 ?type=userNameLogin —— 账号密码表单天然可见，
         不用再靠 JS 硬掰 display:none，也不用人工去点「账号登录」。
    """
    # 注意：必须在**开浏览器之前**判断 —— Chromium 一启动就会把 Cookies
    # 之类的文件写出来，事后再看永远是「有会话」。
    fresh_profile = not _profile_has_session(profile_dir)
    try:
        with sync_playwright() as pw:
            ctx, closer = _open_context(pw, log, profile_dir=profile_dir,
                                        headless=headless)
            if ctx is None:
                return None, '无法启动浏览器（需要系统装有 Edge 或 Chrome）'
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                # 监听「人把浏览器窗口关了」—— 一关立刻收工，不干等超时
                closed = _close_watcher(page, ctx)
                bucket = {'token': None, 'err_code': None}
                on_req, on_resp = _net_hook(bucket)
                page.on('request', on_req)
                page.on('response', on_resp)
                # 装页面内捕获钩子（必须在开页面之前）：hash fragment 里的
                # szblTK 网络层看不到，只有它能抓到。
                _install_token_capture(page)

                log('  打开统一身份认证页…')
                try:
                    page.goto(CAS_URL, timeout=60000, wait_until='domcontentloaded')
                except Exception as e:
                    if _is_closed(closed, page):
                        return None, ERR_BROWSER_CLOSED
                    return None, f'打开认证页失败：{str(e)[:70]}'

                # ① 只等很短的一小会儿（默认 2.5s）看「信任此设备」有没有生效。
                #    以前这里是死等 10 秒 —— 不管能不能免验证都先干等，
                #    实测一次登录 14.4 秒里有 10 秒纯耗在这上面。
                # 全新档案 → 不可能有信任 cookie，宽限期直接给 0，开填就完事
                grace = 0.0 if fresh_profile else float(trust_grace or 0)
                st = _wait_before_fill(page, grace, log, stop, bucket,
                                       closed=closed)
                if st == 'stopped':
                    return None, '已中止'
                if st == 'closed':
                    log('  × 检测到浏览器窗口已被关闭')
                    return None, ERR_BROWSER_CLOSED
                if st == 'token':
                    log('  ✓ 设备信任生效，本次免验证直接拿到 token')
                    return bucket['token'], None
                if st == 'err':
                    code = bucket.get('err_code')
                    return None, (f'平台返回错误码 {code}（1401/1402）：学号可能'
                                  '尚未绑定订场平台账号。请先在微信里正常进一次'
                                  '小程序再回来重试。')
                if st == 'left':
                    # 页面离开了 CAS 登录页 —— 可能是旧会话的 302 跳转链正在
                    # 免验证直通，也可能是被弹去 MFA / 弹回登录页。
                    # 【V4.1 修复】以前这里直接 wait_for_token 等满整个超时：
                    # 一旦跳转链最终弹回登录页，浏览器就停在那里什么都不填，
                    # 表现是「浏览器开了，但学号密码没自动填」。
                    # 现在先悄悄等几秒看 token 来不来；不来且有凭据就继续走
                    # 下面的自动填表，绝不干等。
                    tok, werr = wait_for_token(page, min(12.0, float(timeout)),
                                               log, stop, bucket=bucket,
                                               quiet=True, exclude=old_token,
                                               closed=closed)
                    if tok:
                        log('  ✓ 设备信任生效，本次免验证直接拿到 token')
                        return tok, None
                    if werr == ERR_BROWSER_CLOSED:
                        return None, werr
                    if _looks_like_mfa(page):
                        log('      → 已进入多因子认证页，等待你手动完成…')
                        # 人是真的要在这轮里完成一次认证 → 放行 localStorage
                        return wait_for_token(page, timeout, log, stop,
                                              bucket=bucket, exclude=old_token,
                                              closed=closed, trust_storage=True)
                    if not (username and password):
                        # 没存密码：要由人在浏览器里输密码，同样算「本轮会登录」
                        return wait_for_token(page, timeout, log, stop,
                                              bucket=bucket, exclude=old_token,
                                              closed=closed, trust_storage=True)
                    log('  · 免验证没有生效，改用学号密码自动登录…')
                if stop():
                    return None, '已中止'

                # ② 没能免验证 → 直跳「账号密码登录」，表单天然可见，
                #    不用再硬改样式，也不用人工去点「账号登录」。
                submitted_ok = False
                if username and password:
                    try:
                        page.goto(CAS_PWD_URL, timeout=30000,
                                  wait_until='domcontentloaded')
                    except Exception as e:
                        if _is_closed(closed, page):
                            return None, ERR_BROWSER_CLOSED
                        log(f'  ! 跳转账号密码登录页失败（{str(e)[:60]}），就地展开')
                    ok, why = _wait_form_ready(page, 8, log, closed=closed)
                    if why == 'closed':
                        log('  × 检测到浏览器窗口已被关闭')
                        return None, ERR_BROWSER_CLOSED
                    if not ok and why != 'captcha':
                        # 兜底：万一以后 CAS 改版、type 参数失效，
                        # 退回老办法把隐藏的容器硬掰开。
                        reveal_pwd_form(page, log)
                        page.wait_for_timeout(300)
                    if why == 'captcha':
                        log('  ! 页面要求输入验证码 —— 学号密码已填好，'
                            '请你补一下验证码再点登录')
                        _fill_and_submit(page, username, password, log,
                                         submit=False)
                    else:
                        submitted_ok = _fill_and_submit(page, username,
                                                        password, log)
                        if not submitted_ok:
                            log('  → 请在浏览器里手动输入学号密码')
                elif username:
                    # 没存密码：至少把学号先填好，人只要敲密码
                    try:
                        page.goto(CAS_PWD_URL, timeout=30000,
                                  wait_until='domcontentloaded')
                        _wait_form_ready(page, 5, log, closed=closed)
                        if page.evaluate(JS_SET_USER, username):
                            log('  · 学号已自动填好，请输入密码后点登录')
                    except Exception:
                        pass
                    log('  → 请在浏览器里输入密码完成登录'
                        '（在账号行点 🔑 存一次密码，以后全自动）')
                else:
                    log('  → 请在浏览器里手动输入学号密码')

                page.wait_for_timeout(1500)
                _echo_page_hint(page, log)
                # 【V4.1】自动填表提交后先看看是不是被判「密码错误」。
                # 不检查的话，挂机刷新遇到记错的密码会一直干等到超时（十几分钟），
                # 最后一无所获 —— 早失败早提醒。
                bad = _login_error_text(page)
                if bad:
                    log(f'  × 登录页提示：{bad}')
                    return None, (f'学号或密码不对（页面提示「{bad}」）。'
                                  '到账号表里重新保存一次密码再试。')
                if _looks_like_mfa(page):
                    log('      → 已进入多因子认证页，等你手动完成'
                        '（企业微信 / 短信 / 扫码任选，建议勾「信任此设备」）')
                log('  → 完成后会自动收回 token'
                    + (f'，最多等 {timeout // 60} 分钟' if timeout >= 60
                       else f'，最多等 {timeout} 秒'))
                # trust_storage=True：走到这里说明本轮**确实要发生一次登录**
                # （要么刚自动提交成功，要么已经交给人手动完成）。
                # 该平台对同一账号发的 token 是固定的，不打开这个开关的话，
                # 从 localStorage 扫到的新 token 会因为「和旧的一样」被永久丢弃，
                # 表现就是停在 #/user 干等到超时。
                return wait_for_token(page, timeout, log, stop, bucket=bucket,
                                      exclude=old_token, closed=closed,
                                      trust_storage=True)
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

    【V4.1 状态标注】当前主流程已不走这里 —— CAS 登录统一走
    `_persistent_login()` 直跳账号密码页（CAS_PWD_URL）全自动填表。
    本函数及 `api_cas_login()` 保留为「服务端直登被风控 / 需要人工过 MFA」
    时的降级通道，删除会导致该兜底失效。
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
            # 监听「人把浏览器窗口关了」—— 一关立刻收工，不干等超时
            closed = _close_watcher(page, ctx)
            # V3.1：网络层事件监听，302 一闪而过也能抓住 szblTK
            bucket = {'token': None, 'err_code': None}
            on_req, on_resp = _net_hook(bucket)
            page.on('request', on_req)
            page.on('response', on_resp)
            # 同上：hash fragment 里的 szblTK 只有页面内钩子抓得到
            _install_token_capture(page)
            log('  打开多因子认证页…')
            try:
                page.goto(mfa_url, timeout=60000, wait_until='domcontentloaded')
            except Exception as e:
                if _is_closed(closed, page):
                    return None, ERR_BROWSER_CLOSED
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

            tok, err = wait_for_token(page, timeout, log, stop, bucket=bucket,
                                      closed=closed)
            try:
                closer()
            except Exception:
                pass
            return tok, err
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'


def browser_login(username, password, timeout=300, log=print,
                  stop=None, mfa_hint=None, cas_session=None, mfa_url=None,
                  profile_dir=None, old_token=None, trust_grace=2.5,
                  headless=False):
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
                                     timeout, log, stop, old_token=old_token,
                                     trust_grace=trust_grace,
                                     headless=headless)
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
