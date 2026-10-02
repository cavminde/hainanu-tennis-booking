# -*- coding: utf-8 -*-
"""
【V5 · 订单截图】把订单详情页截成 PNG，按日期分文件夹存起来。

输出结构（app_dir = 配置文件所在目录，也就是 exe 旁边那个 dist）：
    <app_dir>/订单截图/<场地使用日期>/<学号>_<姓名>.png

只读：只查订单、只开页面截图，**不提交订单、不退款、不核销、不点任何按钮**。

踩过的三个坑（别改回去）：
  1. 必须把 <app_dir>/.profiles/<账号id> 挂成 user_data_dir，
     否则 SPA 判定未登录，#/order/<id> 会被渲染成场馆列表。
  2. 光挂档案不够 —— 档案里存的是**旧 token**。必须在 **goto 之前**用
     add_init_script 把新 token 写进 localStorage 的 token / Admin-Token /
     szblTK 三个 key。goto 之后再 evaluate 注入是无效的：
     SPA 的第一批请求已经带着旧 token 出去了。
  3. 档案会话会独立于 token 过期，失效时会被跳到 CAS 登录页
     （正文出现「扫码登录」「账号激活」，或 URL 落到 authserver）。
     检测到就明确报「登录态失效」，不要把登录页截成图存下来。
"""
import os
import re
import time

import booker as b

VW, VH = 480, 950          # 竖屏手机尺寸，和平台 H5 一致
UA = ('Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36')

# SPA 在 localStorage 里读 token 用这三个 key，缺一个都可能判定未登录
INIT_JS = ("try{localStorage.setItem('token','%s');"
           "localStorage.setItem('Admin-Token','%s');"
           "localStorage.setItem('szblTK','%s');}catch(e){}")

_BAD_CHARS = re.compile(r'[\\/:*?"<>|]')

# 状态 → 中文
STATUS_CN = {'OK': '成功', '登录态失效': '登录态失效', '无档案': '没有信任档案',
             '页面异常': '页面异常', '异常': '异常'}


def safe_name(s):
    """文件名里不能有的字符全替换掉（Windows）。"""
    return _BAD_CHARS.sub('-', str(s or '')).strip() or '未知'


def shot_root(app_dir):
    """截图总目录：<app_dir>/订单截图"""
    return os.path.join(app_dir, '订单截图')


def find_chrome(app_dir):
    """内置 Chromium 的路径。找不到返回 ''。"""
    for sub in ('chrome-win64', 'chrome-win'):
        p = os.path.join(app_dir, 'bundled_chromium', sub, 'chrome.exe')
        if os.path.isfile(p):
            return p
    # 兜底：复用 browserlogin 的查找逻辑（源码模式 / 目录结构变了也认）
    try:
        import browserlogin as bl
        return bl._bundled_chrome_exe() or ''
    except Exception:
        return ''


def available(app_dir):
    """(能不能跑, 原因)。playwright 没装 / 没内置浏览器都能提前挡掉。"""
    try:
        import playwright  # noqa: F401
    except Exception as e:
        return False, '没有 playwright（%s）' % type(e).__name__
    chrome = find_chrome(app_dir)
    if not chrome:
        return False, '找不到内置浏览器 bundled_chromium/chrome.exe'
    return True, ''


def pick_targets(rows, day, mode='create', tennis_only=True):
    """挑要截的订单。

    mode='create'   —— 下单日期 == day（抢完单当天就用这个）
    mode='service'  —— 场地使用日期 == day
    tennis_only     —— 只留网球场（游泳馆之类的跳过）
    """
    out = []
    for it in rows or []:
        name = it.get('productName') or ''
        if tennis_only and '网球' not in name:
            continue
        key = 'createTime' if mode == 'create' else 'serviceDate'
        if (it.get(key) or '')[:10] != day:
            continue
        out.append(it)
    return out


def _file_name(acc, order):
    """<学号>_<姓名>.png —— 用户要的命名。姓名取订单里的真实姓名。"""
    uid = (acc.get('username') or '').strip() or (acc.get('name') or '未知')
    real = (order.get('createBy') or '').strip() or (acc.get('name') or '未知')
    return '%s_%s.png' % (safe_name(uid), safe_name(real))


def _uniq_path(path):
    """同名就加 _2 / _3 —— 同一天同一个人有多单时不互相覆盖。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while os.path.exists('%s_%d%s' % (stem, i, ext)):
        i += 1
    return '%s_%d%s' % (stem, i, ext)


def shoot_one(pw, acc, order, out_file, app_dir, headless=True):
    """截一张。返回 (状态, 路径或 None, 说明)。

    状态 ∈ OK / 登录态失效 / 无档案 / 页面异常 / 异常
    """
    oid = str(order.get('id') or '')
    if not oid:
        return '页面异常', None, '订单没有 id'

    chrome = find_chrome(app_dir)
    if not chrome:
        return '异常', None, '找不到内置浏览器'

    pdir = os.path.join(app_dir, '.profiles', str(acc.get('id') or ''))
    if not os.path.isdir(pdir):
        return '无档案', None, '该账号没有浏览器信任档案（先在软件里登录一次并勾选信任）'

    try:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=pdir, executable_path=chrome, headless=headless,
            args=['--no-first-run', '--no-default-browser-check',
                  '--disable-blink-features=AutomationControlled'],
            viewport={'width': VW, 'height': VH}, user_agent=UA)
    except Exception as e:
        return '异常', None, '浏览器启动失败：%s' % repr(e)[:90]

    try:
        tok = acc.get('token') or ''
        # ★ 必须在 goto 之前注入，否则 SPA 第一批请求带着档案里的旧 token 出去了
        ctx.add_init_script(INIT_JS % (tok, tok, tok))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        body = ''
        for _round in range(3):
            page.goto('https://hdscw.hainanu.edu.cn/#/order/' + oid,
                      wait_until='domcontentloaded')
            for _ in range(12):
                time.sleep(1)
                try:
                    body = page.inner_text('body')
                except Exception:
                    body = ''
                if '订单详情' in body:
                    break
            if '订单详情' in body:
                break
            # 掉到 CAS 登录页 → 档案会话过期，重试也没用
            if ('扫码登录' in body or '账号激活' in body
                    or 'authserver' in (page.url or '')):
                return ('登录态失效', None,
                        '档案会话过期，被跳到 CAS 登录页（先在软件里刷新一次 token）')

        if '订单详情' not in body:
            return '页面异常', None, '没渲染出订单详情：' + re.sub(r'\s+', ' ', body)[:70]

        os.makedirs(os.path.dirname(out_file), exist_ok=True)
        page.screenshot(path=out_file, full_page=True)
        return 'OK', out_file, ''
    except Exception as e:
        return '异常', None, repr(e)[:110]
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def run_job(app_dir, accounts, day, mode='create', tennis_only=True,
            log=None, stop=None, headless=True):
    """跑一轮截图。逐个账号、逐张截图，每步都回调 log()。

    accounts —— 已经过滤好的账号 dict 列表（带 token）。
    返回 {'total','ok','failed':[{...}], 'out_dir'}
    """
    log = log or (lambda m: None)
    stop = stop or (type('E', (), {'is_set': lambda self: False})())

    ok, why = available(app_dir)
    if not ok:
        raise RuntimeError(why)

    root = os.path.join(shot_root(app_dir), day)
    total = 0
    done = 0
    failed = []

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        for a in accounts:
            if stop.is_set():
                log('⏹ 已停止（后面的账号不截了）')
                break
            name = a.get('name') or (a.get('username') or '?')
            if not (a.get('token') or '').strip():
                log('· %s：没有 token，跳过' % name)
                failed.append({'account': name, 'status': '没有 token', 'note': ''})
                continue
            sess = b.build_session(4)
            rows, err = None, ''
            # 查订单走的是校园网，夜里经常 10 秒回不来 —— 超时放宽到 25 秒，
            # 再给一次重试（失败一次就跳过整个账号太亏了）。
            for _try in range(2):
                if stop.is_set():
                    break
                try:
                    rows, err = b.api_my_orders_raw(
                        sess, (a.get('token') or '').strip(),
                        app_user_id=str(a.get('app_user_id') or ''),
                        size=60, timeout=25)
                except Exception as e:
                    rows, err = None, '%s: %s' % (type(e).__name__, e)
                if rows is not None:
                    break
                if _try == 0:
                    log('· %s：第一次没查到（%s），重试一次…' % (name, str(err)[:50]))
            if rows is None:
                log('· %s：查不到订单（%s）' % (name, err))
                failed.append({'account': name, 'status': 'token 不可用',
                               'note': str(err)[:80]})
                continue

            hits = pick_targets(rows, day, mode, tennis_only)
            if not hits:
                log('· %s：%s 没有符合条件的订单' % (
                    name, '下单日期' if mode == 'create' else '使用日期'))
                continue

            for od in hits:
                if stop.is_set():
                    break
                total += 1
                fname = _file_name(a, od)
                out_file = _uniq_path(os.path.join(root, fname))
                log('[%d] %s · %s %s %s …' % (
                    total, name, (od.get('serviceDate') or '')[:10],
                    od.get('productName') or '', od.get('serviceTime') or ''))
                st, path, note = shoot_one(pw, a, od, out_file, app_dir,
                                           headless=headless)
                done += 1
                if st == 'OK':
                    log('    ✓ %s' % os.path.basename(path))
                else:
                    log('    × %s %s' % (st, note))
                    failed.append({'account': name, 'status': st, 'note': note})

    ok_n = total - len([f for f in failed if f.get('status') != '没有 token'])
    log('=' * 46)
    log('截图完成：共 %d 张，成功 %d 张，失败 %d 张' % (total, max(ok_n, 0), len(failed)))
    log('输出目录：%s' % root)
    return {'total': total, 'ok': max(ok_n, 0), 'failed': failed,
            'out_dir': root, 'root': shot_root(app_dir)}
