# -*- coding: utf-8 -*-
"""
海南大学网球定场 V4.3 · Tennis Everyday · 本地控制服务
启动： python app_server.py    然后浏览器打开 http://127.0.0.1:8085

V4.3 在这一层的新增：
  · 开火提前量下限 fire_lead_ms（默认 200ms）—— 校时校不掉「服务端放号任务
    自己晚触发」，所以校时之外再往前推一截，宁可早发几发「未开放」。

V4.2 在服务这一层新增：
  · 【C1】运行日志落盘 —— 每次任务写一份 logs/run_YYYYMMDD_HHMMSS.log，
          写盘前逐行脱敏（token / 密码 / CAS 票据 / 学号都不落地），
          只保留最近 14 份。V4.1 的日志只在内存里，进程一退就全丢，
          导致「今早到底抢没抢到、第一枪多快」事后一个字都查不到。
  · 【C3】日志导出 —— /api/log/export（当前会话）、/api/log/file（读落盘文件）、
          /api/log/info（日志目录与当前文件名）。导出同样过一遍脱敏。
  · 【B4】显式把 Windows 计时精度提到 1ms，不依赖别的进程顺手顶上去。

相对 V3.1 的新增：
  · 账号库 —— 一个账号一行（名字 / 学号 / 用户ID / Bearer token）
  · 方式一（学号 + 门户密码）可以反复登录，往库里追加账号二、账号三……
  · 每个账号有自己的时间窗口 + 场地优先顺序（前端做成浏览器标签页，切页即切换账号）
  · 执行时可选「只用当前账号」或「全部账号依次尝试，抢到即停」

接口：
  GET  /                      页面
  GET  /api/config            读取配置（含 accounts 账号库）
  POST /api/config            整包保存配置
  GET  /api/status            任务状态 + 日志
  GET  /api/log/export        下载当前会话日志（已脱敏）
  GET  /api/log/file          下载 logs/ 里的某个落盘日志（默认 latest.log）
  GET  /api/log/info          日志目录 + 当前运行日志文件名
  GET  /api/accounts          账号库列表
  POST /api/accounts/add      新增空白账号
  POST /api/accounts/remove   删除账号
  POST /api/accounts/verify   重新校验某个账号的 token
  POST /api/accounts/active   切换当前账号
  POST /api/caslogin          方式一：学号 + 门户密码 → token → 写进账号库
  POST /api/login             备用登录（H5 接口，无 UI 入口，仅供脚本/调试调用）
  POST /api/preview           侦察空场 + 生成候选（不提交）
  POST /api/start_now         立即抢
  POST /api/start_sched       定时抢
  POST /api/stop              中止
  POST /api/bench             接口测速
  POST /api/diagnose          下单有效性检测
  POST /api/salt              重置反冲突随机签
  POST /api/accounts/password 设置/清除某账号的「记住密码」（挂机自动刷新用）
  POST /api/refresh           立即刷新 token（手动触发）
  抓包相关：/api/capture  /api/capture/stop  /api/capture/status
            /api/ca/install  /api/ca/remove
"""
import json
import os
import re
import sys
import threading
import time
import webbrowser
from datetime import date as date_cls, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, quote

import booker as b
import accounts as acc

try:
    import tokencap as tc
    HAS_TC = True
except Exception as _e:
    tc = None
    HAS_TC = False
    _TC_ERR = str(_e)

try:
    import browserlogin as bl
    HAS_BL = bl.HAS_PLAYWRIGHT
    _BL_ERR = '' if HAS_BL else bl._PW_ERR
except Exception as _e:
    bl = None
    HAS_BL = False
    _BL_ERR = str(_e)

HOST = '127.0.0.1'
PORT = 8085      # V2=8081 V3=8082 V3.1=8083 V4.0/V4.1/V4.2=8085
                 # ⚠ 与 V4.1 同端口，两版不能同时跑（Server.allow_reuse_address=False
                 #   会让后启动的那个直接报「端口已被占用」）。想并行就改这里。

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 打包成 exe 后，配置文件放 exe 同目录，方便用户直接改
if getattr(sys, 'frozen', False):
    CONFIG_PATH = os.path.join(os.path.dirname(sys.executable), 'config.json')
else:
    CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

STATE = {'running': False, 'log': [], 'result': None, 'task': None}
_LOCK = threading.Lock()
_STOP = threading.Event()

# 抓包状态
CAP = {'active': False, 'token': None, 'error': None, 'port': None,
       'ca_ready': False, 'proxy': None, 'cap_obj': None}
_CAP_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 【V4.2 C1】运行日志落盘
# ---------------------------------------------------------------------------
# V4.1 的日志只活在内存 STATE['log'] 里（上限 3000 行），进程一退就全没了。
# 后果是用户早上 8:00 抢完场，回头想知道「第一枪到底多快、有没有被限流、
# 成没成交」，一个字都查不到 —— 只能靠浏览器档案的 History 反推时间线。
# 现在每次任务都写一份到 exe 旁边的 logs/，并在写盘前脱敏。
LOG_DIR = os.path.join(os.path.dirname(CONFIG_PATH), 'logs')
LOG_KEEP = 14          # 只保留最近 14 个运行日志，超出的删最旧
_LOG_FILE = None       # 当前运行的日志文件句柄
_LOG_NAME = ''
# 【V4.2 E4 验收发现】任务跑完时 close_run_log() 会把 _LOG_NAME 清零，
# 于是「抢完之后回头问：刚才那次写在哪个文件里？」—— /api/log/info 只能回空串，
# 界面上就成了「本次日志同时写入：…\logs（，已脱敏）」这种空文件名。
# 单独记一份「最近一次」的名字，任务结束后照样能告诉用户文件名。
_LAST_LOG_NAME = ''

# 落盘前必须抹掉的凭据特征。日志里出现 token / 密码 / 票据，
# 等于把「日志导出」变成新的泄漏面 —— 这正是用户这次要堵的洞。
_REDACT_PATTERNS = [
    (re.compile(r'Bearer\s+[A-Za-z0-9._\-]+'), 'Bearer [REDACTED]'),
    (re.compile(r'eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}'),
     '[REDACTED-JWT]'),
    (re.compile(r'\bv1:[A-Za-z0-9_\-]{4,}'), '[REDACTED-PWD]'),
    (re.compile(r'szblTK=[^&\s"\']+'), '[REDACTED-SZBLTK]'),
    (re.compile(r'ticket=ST-[^&\s"\']+'), '[REDACTED-TICKET]'),
    # 学号：11 位、以 20 开头 → 只留前 4 位
    (re.compile(r'\b(20\d{2})\d{7}\b'), r'\1*******'),
]


def redact(text):
    """把一行日志里的凭据特征替换成 [REDACTED]。脱敏失败也不能丢日志，
    所以任何异常都退回原文的保守截断版本。"""
    try:
        for pat, rep in _REDACT_PATTERNS:
            text = pat.sub(rep, text)
        return text
    except Exception:
        return text


def _rotate_logs():
    """删掉最旧的运行日志，只留最近 LOG_KEEP 个。"""
    try:
        names = sorted(f for f in os.listdir(LOG_DIR)
                       if f.startswith('run_') and f.endswith('.log'))
        for old in names[:-LOG_KEEP]:
            try:
                os.remove(os.path.join(LOG_DIR, old))
            except Exception:
                pass
    except Exception:
        pass


def start_run_log():
    """为一次任务开一个新的日志文件。重复调用会先关掉上一个。"""
    global _LOG_FILE, _LOG_NAME, _LAST_LOG_NAME
    close_run_log()
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        # latest.log 是「最近这一次」的快捷入口：每次开新日志时清零重写，
        # 避免一次次运行叠成无限长（写副本，不用符号链接：Windows 上要权限）。
        try:
            open(os.path.join(LOG_DIR, 'latest.log'), 'w', encoding='utf-8').close()
        except Exception:
            pass
        _LOG_NAME = 'run_%s.log' % datetime.now().strftime('%Y%m%d_%H%M%S')
        _LAST_LOG_NAME = _LOG_NAME
        _LOG_FILE = open(os.path.join(LOG_DIR, _LOG_NAME), 'a', encoding='utf-8')
        _rotate_logs()
        head = ('=' * 66 + '\n'
                ' 运行开始 %s\n'
                ' 日志文件 %s\n'
                ' （本文件已脱敏：token / 密码 / 票据 / 学号均不落地）\n'
                % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), _LOG_NAME)
                + '=' * 66)
        _LOG_FILE.write(head + '\n')
        _LOG_FILE.flush()
        # 【V4.2 E4 验收发现】head 只写进了 run_*.log，latest.log 里没有 ——
        # 而 latest.log 恰恰是「懒得找文件名」的人唯一会打开的那个。
        # 顺手同步一份，两个文件的开头就一致了。
        try:
            with open(os.path.join(LOG_DIR, 'latest.log'), 'a',
                      encoding='utf-8') as f:
                f.write(head + '\n')
        except Exception:
            pass
    except Exception:
        # 写不了日志绝不能把抢场流程带崩
        _LOG_FILE = None
        _LOG_NAME = ''


def close_run_log():
    global _LOG_FILE, _LOG_NAME
    if _LOG_FILE is not None:
        try:
            _LOG_FILE.write('\n--- 运行结束 %s ---\n'
                            % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            _LOG_FILE.flush()
            _LOG_FILE.close()
        except Exception:
            pass
    _LOG_FILE = None
    _LOG_NAME = ''


def _write_run_log(msg):
    """把一行（已脱敏）追加到当前运行日志，并同步一份到 latest.log。"""
    if _LOG_FILE is None:
        return
    stamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
    line = '%s %s\n' % (stamp, redact(str(msg)))
    try:
        _LOG_FILE.write(line)
        _LOG_FILE.flush()
    except Exception:
        return
    # latest.log 是给「懒得找文件名」的人用的快捷入口（副本在 start_run_log 时已清零）
    try:
        with open(os.path.join(LOG_DIR, 'latest.log'), 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass


def append_log(msg):
    with _LOCK:
        STATE['log'].append(str(msg))
        if len(STATE['log']) > 3000:
            STATE['log'] = STATE['log'][-3000:]
        # 【V4.2 C1】同步落盘。在同一个锁里做，保证顺序；
        # 内部所有异常都已吞掉，不会因为磁盘问题拖挂抢场。
        _write_run_log(msg)


# 浏览器启动失败的原因单独写一份到 exe 旁边，方便排查（控制台不一定看得到）
BL_LOG = os.path.join(os.path.dirname(CONFIG_PATH), 'browser_launch.log')


def dump_bl_error(msg):
    try:
        with open(BL_LOG, 'a', encoding='utf-8') as f:
            f.write(datetime.now().strftime('%m-%d %H:%M:%S ') + msg + '\n')
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def read_config():
    return acc.load(CONFIG_PATH)


def write_config(cfg):
    old = read_config()
    old.update(cfg or {})
    return acc.save(CONFIG_PATH, old)


def account_params(cfg, acc_id, payload=None):
    """把「全局参数 + 某个账号自己的参数」合成一份 run_booking 参数。"""
    a = acc.get_account(cfg, acc_id)
    if a is None:
        a = acc.get_account(cfg, cfg.get('active'))
    if a is None:
        a = (cfg.get('accounts') or [{}])[0]
    p = {k: v for k, v in (cfg or {}).items() if k != 'accounts'}
    p.update(payload or {})
    # 账号专属参数最后覆盖（时间窗口 / 场地顺序 / token 都以账号自己的为准）
    p['token'] = (a.get('token') or '').strip()
    p['app_user_id'] = str(a.get('app_user_id') or '')
    p['window_start'] = a.get('window_start') or '18:00'
    p['window_end'] = a.get('window_end') or '22:00'
    p['court_order'] = list(a.get('court_order') or [])
    p['court_enabled'] = dict(a.get('court_enabled') or {})
    p['sort_strategy'] = a.get('sort_strategy') or 'court_rand'
    p['base_dir'] = os.path.dirname(CONFIG_PATH)
    p['_account_id'] = a.get('id')
    p['_account_name'] = a.get('name')
    return p


def params_from(payload):
    cfg = read_config()
    acc_id = (payload or {}).get('account_id') or cfg.get('active')
    p = account_params(cfg, acc_id, payload)
    p['_payload'] = dict(payload or {})
    return p


def parse_schedule(raw):
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        parts = [int(x) for x in raw.split(':')]
        while len(parts) < 3:
            parts.append(0)
        h, m, s = parts[:3]
    except Exception:
        return None
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        return None
    return (h, m, s)


# ---------------------------------------------------------------------------
# 【V4.1】开抢前 N 分钟自动刷新 token（挂机过夜用）
# ---------------------------------------------------------------------------
# 为什么要这一步：token 是服务端会话凭据，实测寿命不到 10 小时。
# 晚上 23 点挂机、早上 8 点开抢，中间隔了 9 小时，旧 token 必然已经 401。
# 而「信任此设备」的会话同样会过期 —— 实测隔几小时后免密刷新会直接落到
# 扫码页拿不到 token。所以无人值守刷新必须**记住密码并重新登录一次**。

def scheduled_target(schedule):
    """定时任务的开抢时刻（今天该点已过就顺延到明天）。

    【V4.2 E4 验收发现】原来这里直接 `h, m, s = schedule`，而传进来的可能是
    config.json 里的**字符串** '07:59:58' —— 字符串按字符解包成 8 份，
    立刻抛 ValueError: too many values to unpack。
    预览 / 接口测速 / 有效性检测 走的都是 params_from() 拿到的原始字符串，
    于是每跑一次就日志里多一行「[刷新阶段异常] ValueError」，白吓人一跳。
    这里统一先过一遍 parse_schedule 归一化。
    """
    if isinstance(schedule, str):
        schedule = parse_schedule(schedule)
    if not schedule:
        return None
    h, m, s = schedule
    target = datetime.now().replace(hour=h, minute=m, second=s, microsecond=0)
    if target <= datetime.now():
        target += timedelta(days=1)
    return target


def refresh_one_account(cfg, a, timeout, log, stop):
    """用记住的密码给单个账号重登一次，换新 token 并落盘。"""
    name = a.get('name') or '?'
    pwd = acc.account_password(a)
    if not pwd:
        log(f'  · {name}：没记住密码，跳过'
            f'（在「账号」里勾「记住密码」并填一次就能全自动）')
        return False
    if not (HAS_BL or bl):
        log(f'  × {name}：浏览器组件不可用（{_BL_ERR}），无法自动登录')
        return False

    prof = acc.profile_dir(os.path.dirname(CONFIG_PATH), a.get('id'))
    old = (a.get('token') or '').strip()
    log(f'  → {name}（学号 {a.get("username") or "-"}）：重新登录…')
    t0 = time.time()
    try:
        tok, err = bl.browser_login(a.get('username') or '', pwd,
                                    timeout=timeout,
                                    log=lambda m: log('      ' + str(m)),
                                    stop=stop, profile_dir=prof,
                                    old_token=old or None,
                                    trust_grace=2.5, headless=False)
    except Exception as e:
        log(f'  × {name}：{type(e).__name__}: {e}')
        return False
    if not tok:
        log(f'  × {name}：{err or "没拿到 token"}'
            f' —— 旧 token 保留，开抢时可能失效')
        return False

    ok, why = b.verify_token(tok)
    if not ok:
        log(f'  × {name}：新 token 校验不通过（{why}），保留旧 token')
        return False

    # 写回磁盘：重新读一份再改，避免覆盖掉这期间别人（界面）的改动
    fresh = read_config()
    hit = False
    for x in (fresh.get('accounts') or []):
        if x.get('id') == a.get('id'):
            x['token'] = tok
            x['token_time'] = datetime.now().strftime('%m-%d %H:%M')
            x['token_ok'] = True
            x['token_why'] = ''
            hit = True
    if hit:
        acc.save(CONFIG_PATH, fresh)
    a['token'] = tok
    log(f'  ✓ {name}：token 已刷新（{time.time() - t0:.1f}s）')
    if old and tok == old:
        # 这个平台发的 token 是 HS512 + payload 只有 login_user_key，
        # 没有 exp / 时间戳 / 随机数 —— 同一个账号每次登录拿回来的串都一样。
        # 所以「串没变」不等于「没刷新成功」：真正被刷新的是服务端会话。
        # 不写明的话，用户会以为刷新失败了。
        log('    · token 串与上次相同，这是正常的：该平台对同一账号固定发同一个'
            ' token，本次重新登录刷新的是服务端会话')
    return True


def refresh_all_accounts(cfg, target, log, stop):
    """逐个账号刷新。串行进行 —— 同时开好几个浏览器会互相抢档案锁。"""
    todo = [a for a in (cfg.get('accounts') or [])
            if a.get('enabled', True) and not acc.is_closed(a)
            and (a.get('username') or '').strip()]
    if not todo:
        log('  × 没有可刷新的账号（需要有学号、没被停用、且没关场）')
        return 0, 0
    ok_n = 0
    for a in todo:
        if stop():
            break
        # 留给刷新的时间 = 距开抢还剩多久；不能拖过开抢时刻
        left = (target - datetime.now()).total_seconds()
        if left <= 5:
            log('  ! 已经到开抢时刻，剩余账号停止刷新，直接开抢')
            break
        timeout = min(int(cfg.get('refresh_timeout') or 900),
                      max(10, int(left) - 5))
        if refresh_one_account(cfg, a, timeout, log, stop):
            ok_n += 1
    return ok_n, len(todo)


def roll_target_date(cfg, log):
    """挂机跨天：把目标日期滚到「今天 + 可提前天数」。

    不滚的话，昨晚填的日期到明早就超出预约窗口，直接抢不了。
    """
    if not cfg.get('auto_roll_date', True):
        return cfg
    ahead = int(cfg.get('max_days_ahead') or 2)
    new_d = (date_cls.today() + timedelta(days=ahead)).isoformat()
    old_d = cfg.get('target_date') or ''
    if new_d != old_d:
        log(f'  · 目标日期自动跟随：{old_d or "未设置"} → {new_d}'
            f'（今天 + {ahead} 天）')
        cfg['target_date'] = new_d
        acc.save(CONFIG_PATH, cfg)
    return cfg


def preflight_refresh(params, log, stop):
    """定时任务的开抢前准备：等 → 刷新 token → 滚动日期 → 回写 params。

    只在定时任务里跑（params['schedule'] 非空）。立即抢不跑，
    因为那是「现在就想发一枪」，等几十秒的登录反而误事。
    """
    sched = params.get('schedule')
    # 【V4.2 E4 验收发现】config.json 里存的是字符串 '07:59:58'；
    # 只有 /api/start_sched 会先 parse 成 (h,m,s) 元组，预览 / 测速 / 有效性检测
    # 拿到的是原样字符串。这里统一归一化，顺带把「没定时」和「定时格式不对」都挡掉。
    if isinstance(sched, str):
        sched = parse_schedule(sched)
    if not sched:
        return
    cfg = read_config()
    target = scheduled_target(sched)
    if target is None:
        log('   ⚠ 定时时间解析不出来，跳过开抢前刷新（本次按立即执行处理）。')
        return

    if cfg.get('auto_refresh_token', True):
        lead = int(cfg.get('refresh_lead_min') or 15)
        refresh_at = target - timedelta(minutes=lead)
        now = datetime.now()
        if refresh_at > now:
            log('=' * 66)
            log(f' 挂机中：{refresh_at.strftime("%m-%d %H:%M:%S")} 自动刷新 token，'
                f'{target.strftime("%m-%d %H:%M:%S")} 开抢')
            log(f'   （提前 {lead} 分钟刷新；现在 '
                f'{now.strftime("%m-%d %H:%M:%S")}，还需等 '
                f'{(refresh_at - now).total_seconds() / 60:.1f} 分钟）')
            log('   中途想取消，按「停止」即可。')
            log('=' * 66)
            while not stop():
                left = (refresh_at - datetime.now()).total_seconds()
                if left <= 0:
                    break
                time.sleep(min(5.0, max(0.2, left)))
            if stop():
                log('  已取消（刷新前）。')
                return
            cfg = read_config()

        log('')
        log('=' * 66)
        log(f' [刷新 token] 距开抢 {lead} 分钟，开始逐个账号重新登录…')
        log('=' * 66)
        ok_n, total = refresh_all_accounts(cfg, target, log, stop)
        log(f' [刷新 token] 完成：{ok_n}/{total} 个账号拿到新 token')
        if ok_n < total:
            log('   ⚠ 有账号没刷新成功，它们会用旧 token 开抢，'
                '很可能是 401 —— 早上起来看一眼日志。')
        cfg = read_config()

    cfg = roll_target_date(cfg, log)

    # 把刷新结果回写进本次任务的参数（单账号直跑时用的是 params 里的 token）
    a = acc.get_account(cfg, params.get('_account_id'))
    if a is None:
        a = acc.get_account(cfg, cfg.get('active'))
    if a is not None:
        params['token'] = (a.get('token') or '').strip()
    params['target_date'] = cfg.get('target_date')
    if params.get('_payload') is not None:
        params['_payload']['target_date'] = cfg.get('target_date')


# ---------------------------------------------------------------------------
# 任务调度
# ---------------------------------------------------------------------------

def spawn(kind, params):
    def runner():
        STATE['running'] = True
        _STOP.clear()
        t0 = time.time()
        # 【V4.2 C1】每次任务开一份自己的日志文件，进程退了也查得到
        start_run_log()
        append_log(f'[任务] {STATE.get("task") or kind}')
        # 【V4.1】定时任务：先等到「开抢前 N 分钟」刷新 token + 滚动日期，
        # 再交给下面的抢单流程。立即抢没有 schedule，这里会直接返回。
        #
        # 【V4.2 E4 验收发现】只有**真要开抢**的任务才走这一步。
        # 预览 / 接口测速 / 有效性检测 的 params 是从 config 整包拷来的，
        # 里面那个 schedule='07:59:58' 是「定时抢」用的，跟它们没关系；
        # 让它们也跑 preflight，就会傻等到第二天早上才继续（等于卡死）。
        if kind in ('book', 'book_multi'):
            try:
                preflight_refresh(params, append_log,
                                  lambda: _STOP.is_set())
            except Exception as e:
                append_log(f'[刷新阶段异常] {type(e).__name__}: {e}')
        if _STOP.is_set():
            append_log('  已取消（刷新阶段被中止）。')
            close_run_log()
            STATE['running'] = False
            return
        try:
            if kind == 'browser_login':
                res = run_browser_login(params)
            elif kind == 'retoken':
                res = run_retoken(params)
            elif kind == 'capture':
                res = run_capture(params)
            elif kind == 'preview':
                params['submit'] = False
                res = b.run_booking(params, log=append_log, stop=lambda: _STOP.is_set())
            elif kind == 'preview_multi':
                params['submit'] = False
                res = run_multi(params, lambda: _STOP.is_set())
            elif kind == 'book_multi':
                params['submit'] = True
                res = run_multi(params, lambda: _STOP.is_set())
            elif kind == 'bench':
                res = b.benchmark(params, log=append_log, stop=lambda: _STOP.is_set())
            elif kind == 'diagnose':
                res = b.diagnose(params, log=append_log)
            else:
                params['submit'] = True
                res = b.run_booking(params, log=append_log, stop=lambda: _STOP.is_set())
            with _LOCK:
                STATE['result'] = res
        except Exception as e:
            append_log(f'[异常] {type(e).__name__}: {e}')
            with _LOCK:
                STATE['result'] = {'success': False, 'error': f'{type(e).__name__}: {e}'}
        finally:
            append_log(f'[完成] 耗时 {time.time() - t0:.1f}s')
            close_run_log()
            STATE['running'] = False

    threading.Thread(target=runner, daemon=True).start()


def spawn_refresh(accounts_todo, timeout):
    """手动「刷新 token」按钮：在后台线程里逐个账号重登。"""
    def runner():
        STATE['running'] = True
        _STOP.clear()
        t0 = time.time()
        start_run_log()          # 【V4.2 C1】刷新过程也要留档
        append_log('=' * 66)
        append_log(f' 手动刷新 token（{len(accounts_todo)} 个账号）')
        append_log('=' * 66)
        cfg = read_config()
        ok_n = 0
        for a in accounts_todo:
            if _STOP.is_set():
                append_log('  已中止。')
                break
            if refresh_one_account(cfg, a, timeout, append_log,
                                   lambda: _STOP.is_set()):
                ok_n += 1
        with _LOCK:
            STATE['result'] = {'refreshed': ok_n, 'total': len(accounts_todo)}
        append_log(f' 完成：{ok_n}/{len(accounts_todo)} 个账号拿到新 token'
                   f'（耗时 {time.time() - t0:.1f}s）')
        close_run_log()
        STATE['running'] = False

    threading.Thread(target=runner, daemon=True).start()


def run_multi(params, stop):
    """多账号【真并行】：每个账号一个独立线程，各自按自己的时间窗口同时开抢。

    和旧版的区别：旧版是一个号跑完才轮到下一个（前一个抢到后面就不跑了），
    第二个账号会迟到几秒甚至等到第二天；现在所有账号在同一个时刻各自开火。

    并行安全的依据：
      - 每个账号用 account_params() 合成独立参数（token / 时间窗口 / 场地顺序都是自己的），
        线程之间不共享任何可变对象；
      - booker.run_booking 不写配置也不写文件，只是纯读 + 发 HTTP 请求；
      - 写日志的 append_log 内部有锁，多线程同时写不会乱。

    抢到一个是否停手由 params['stop_on_first'] 决定：
      - True（默认）：任何一个账号抢到就立刻通知其他线程收手，避免占多个场；
      - False：各抢各的，谁抢到都继续，适合「不同账号抢不同时间段」。
    """
    log = append_log
    cfg = read_config()
    todo = acc.runnable(cfg)
    if not todo:
        msg = '账号库里没有可抢的账号（需要有 token，且没被停用）'
        log('× ' + msg)
        return {'success': False, 'error': msg, 'multi': True, 'runs': []}

    stop_on_first = bool(params.get('stop_on_first', True))

    log('=' * 66)
    log(f' 多账号【并行】开抢：共 {len(todo)} 个账号同时参与')
    log('   ' + ' ｜ '.join(a.get('name') or '?' for a in todo))
    log(f'   抢到后：{"立刻全体收手" if stop_on_first else "各自继续（互不影响）"}')
    log('=' * 66)

    merged = {'success': False, 'multi': True, 'runs': [],
              'accounts': len(todo), 'parallel': True}
    merged_lock = threading.Lock()
    # 抢到之后用来叫停其他账号（和全局 _STOP 是两回事：
    # 用户按「停止」要停全部，某个账号抢到只该停其余账号）
    won = threading.Event()

    def one_account(idx, a):
        """单个账号的完整抢单流程，跑在自己的线程里。"""
        name = a.get('name') or '?'
        # 单个账号的停止条件：用户喊停 / 已经有人抢到且要求抢到即停
        def acc_stop():
            return stop() or (stop_on_first and won.is_set())

        log('')
        log('-' * 66)
        log(f' [{idx}/{len(todo)}] {name}'
            f'（学号 {a.get("username") or "-"}，ID {a.get("app_user_id") or "-"}）')
        log('-' * 66)
        try:
            p = account_params(cfg, a['id'], params.get('_payload') or {})
            p['submit'] = bool(params.get('submit', True))
            if params.get('schedule'):
                p['schedule'] = params['schedule']
            r = b.run_booking(p, log=log, stop=acc_stop)
        except Exception as e:
            log(f'  [异常] {name}：{type(e).__name__}: {e}')
            r = {'success': False, 'error': f'{type(e).__name__}: {e}'}

        rec = {
            'id': a.get('id'), 'account': name,
            'success': bool(r.get('success')),
            'error': r.get('error') or '',
            'booked': r.get('booked'),
        }
        with merged_lock:
            merged['runs'].append(rec)
            # 空场图：第一个带图的结果就留下（多个账号的图基本一样）
            if r.get('matrix') and not merged.get('matrix'):
                merged['matrix'] = r['matrix']
                merged['slots'] = r.get('slots')
                merged['weekday'] = r.get('weekday')
            if r.get('success') and not merged['success']:
                merged['success'] = True
                merged['booked'] = r.get('booked')
                merged['account'] = name
                merged['account_id'] = a.get('id')
                won.set()
                log(f'\n✓✓ 账号「{name}」抢到了！')
                if stop_on_first:
                    log('   正在通知其他账号收手…')

    threads = []
    for i, a in enumerate(todo, 1):
        t = threading.Thread(target=one_account, args=(i, a), daemon=True)
        t.start()
        threads.append(t)

    # 等所有账号都结束（抢到即停时，其余线程会被 acc_stop 叫停，很快返回）
    for t in threads:
        t.join()

    runs = merged['runs']
    if merged['success']:
        got = [r['account'] for r in runs if r['success']]
        log(f'\n✓ 本次成交账号：{"、".join(got)}')
        if stop_on_first and len(runs) < len(todo):
            log(f'  （{len(todo) - len(runs)} 个账号因已抢到而未走到最后）')
    else:
        merged['error'] = '全部账号都没抢到'
        log('\n△ 全部账号都试过了，没抢到，详见上方日志。')
    return merged


def run_browser_login(params):
    """开真实浏览器走 CAS。MFA 那一步交回给人，我们不绕过。"""
    username = params.get('username') or ''
    password = params.get('password') or ''
    timeout = int(params.get('mfa_timeout') or 300)
    profile_dir = params.get('profile_dir')
    if profile_dir:
        append_log('  使用固定浏览器档案：' + os.path.basename(profile_dir))
        print(f'[caslogin] 即将用固定档案打开浏览器'
              f'（{os.path.basename(profile_dir)}）…', file=sys.stderr, flush=True)
    cfg0 = read_config()
    a0 = (acc.get_account(cfg0, params.get('acc_id'))
          or acc.find_by_username(cfg0, username))
    old_token = ((a0 or {}).get('token') or '').strip()
    try:
        tok, err = bl.browser_login(username, password, timeout=timeout,
                                    log=append_log, stop=lambda: _STOP.is_set(),
                                    cas_session=params.get('cas_session'),
                                    mfa_url=params.get('mfa_url'),
                                    profile_dir=profile_dir,
                                    old_token=old_token)
    except Exception as e:
        tok, err = None, f'{type(e).__name__}: {e}'
    if not tok:
        msg = '[浏览器登录失败] ' + (err or '未知原因（浏览器没能打开）')
        print(msg, file=sys.stderr, flush=True)
        dump_bl_error(msg)
        append_log(f'× {err}')
        return {'success': False, 'error': err, 'account_id': params.get('acc_id')}
    ok, why = b.verify_token(tok)
    same = bool(old_token) and tok == old_token
    if same:
        append_log('  ! 注意：拿回来的和上一次是同一个 token')
        append_log('    —— 浏览器档案里的登录会话还有效，CAS 直接免验证放行了，')
        append_log('       没有真的换发新 token。如果它就是失效的那个，请点')
        append_log('       「🧹 清除本账号的设备信任」再用密码完整登录一次。')
    uid, uname = b.api_profile(tok)
    cfg = read_config()
    a = acc.get_account(cfg, params.get('acc_id'))
    remember = bool(params.get('remember_password')) and bool(password)
    if a is None:
        a, _ = acc.upsert_login(cfg, username, tok, app_user_id=uid,
                                ok=ok, why=why, mfa=True)
    else:
        a['token'] = tok
        a['token_ok'] = ok
        a['token_why'] = why
        a['token_time'] = datetime.now().strftime('%m-%d %H:%M')
        a['mfa_seen'] = True
        if username and not a.get('username'):
            a['username'] = username
        if uid and not a.get('app_user_id'):
            a['app_user_id'] = uid
        cfg['active'] = a['id']
    if remember:
        # 用户勾了「记住密码」：混淆后存本机，之后重新获取 token /
        # 开抢前自动刷新都不用再输密码。
        a['password'] = acc.encode_password(password)
        a['remember_password'] = True
        append_log('  · 密码已按你的选择保存在本机（混淆存储），'
                   '以后重新获取 token 全自动')
    acc.save(CONFIG_PATH, cfg)
    append_log(f'✓ 已写入账号库：{a.get("name")}（学号 {a.get("username") or "-"}）')
    append_log(f'  校验：{why}')
    return {'success': True, 'token': tok, 'valid': ok, 'why': why,
            'same_as_before': same,
            'account_id': a.get('id'), 'account_name': a.get('name'),
            'account': _one_view(cfg, a.get('id')),
            'accounts': _view_accounts(cfg), 'active': cfg.get('active')}


def _retoken_targets(ids=None, only_invalid=False):
    """算出「重新获取 token」要处理哪些账号，顺带标出哪些需要补密码。

    返回 [{id, name, username, has_profile, need_password, token_ok}]
    need_password=True 的账号既没有设备信任档案、也没在本机存密码，
    必须临时给一次密码才能在浏览器里登录。
    """
    cfg = read_config()
    want = None
    if ids:
        want = set(str(x) for x in ids)
    out = []
    for a in (cfg.get('accounts') or []):
        if not a.get('enabled', True):
            continue
        if want is not None and str(a.get('id')) not in want:
            continue
        if only_invalid:
            tok = (a.get('token') or '').strip()
            if tok and a.get('token_ok') is True:
                continue
        if not (a.get('username') or '').strip():
            continue
        has_prof = _has_profile(a.get('id'))
        has_pwd = bool(acc.account_password(a))
        out.append({'id': a.get('id'), 'name': a.get('name') or '?',
                    'username': (a.get('username') or '').strip(),
                    'has_profile': has_prof,
                    'has_password': has_pwd,
                    'need_password': not has_prof and not has_pwd,
                    'token_ok': a.get('token_ok')})
    return out


def run_retoken(params):
    """逐个账号重新取一次 token（失效了就重取，不用再敲一遍学号）。

    - 有设备信任档案的账号：浏览器打开就自己跳过去，全自动，不需要密码；
    - 没有档案的账号：必须带密码（前端会让用户补），走一次完整登录。
    """
    ids = [str(x) for x in (params.get('ids') or [])]
    pwds = dict(params.get('passwords') or {})
    timeout = int(params.get('mfa_timeout') or 300)
    total = len(ids)
    done, fail = [], []

    for i, aid in enumerate(ids, 1):
        if _STOP.is_set():
            append_log('  已中止，剩下的账号没再继续。')
            break
        cfg = read_config()
        a = acc.get_account(cfg, aid)
        if not a:
            fail.append({'id': aid, 'name': aid, 'error': '账号不存在'})
            continue
        name = a.get('name') or '?'
        username = (a.get('username') or '').strip()
        old_token = (a.get('token') or '').strip()
        append_log('-' * 60)
        append_log(f'[{i}/{total}] {name}（{username}）…')
        if not username:
            append_log('  × 跳过：这个账号没填学号')
            fail.append({'id': aid, 'name': name, 'error': '没填学号'})
            continue
        pwd = (pwds.get(str(aid)) or '').strip()
        if not pwd:
            # 【V4.1 修复】前端这次没给密码时，回退用本机「记住密码」存下的
            # 那个 —— 之前只看请求里的密码，导致存了密码也每次都要手输。
            pwd = acc.account_password(a)
            if pwd:
                append_log('  · 使用本机已保存的密码自动登录')
        profile = acc.profile_dir(os.path.dirname(CONFIG_PATH), aid)
        if not pwd and not os.path.isdir(profile):
            append_log('  × 跳过：没有设备信任档案，也没存密码'
                       '（在账号行点 🔑 存一次密码，或先登录一次勾「信任此设备」）')
            fail.append({'id': aid, 'name': name, 'error': '需要密码（无设备信任档案）'})
            continue
        if not HAS_BL:
            append_log('  × 浏览器模块不可用，无法重新获取')
            fail.append({'id': aid, 'name': name, 'error': '浏览器模块不可用'})
            break
        try:
            tok, err = bl.browser_login(username, pwd, timeout=timeout,
                                        log=append_log,
                                        stop=lambda: _STOP.is_set(),
                                        profile_dir=profile,
                                        old_token=old_token)
        except Exception as e:
            tok, err = None, f'{type(e).__name__}: {e}'
        if not tok:
            append_log('  × ' + (err or '没拿到 token'))
            fail.append({'id': aid, 'name': name, 'error': err or '没拿到 token'})
            continue
        same = bool(old_token) and tok == old_token
        if same:
            append_log('  ! 拿回来的还是上一次那个 token —— CAS 免验证放行了，'
                       '会话没刷新，等于没重取。')
            append_log('    要强制换新的：先「🧹 清除设备信任」再用密码登录一次。')
        ok, why = b.verify_token(tok)
        uid, _ = b.api_profile(tok)
        cfg = read_config()
        a = acc.get_account(cfg, aid) or a
        a['token'] = tok
        a['token_ok'] = ok
        a['token_why'] = why
        a['token_time'] = datetime.now().strftime('%m-%d %H:%M')
        if uid:
            a['app_user_id'] = uid
        acc.save(CONFIG_PATH, cfg)
        append_log(('  ✓ 新的 token 已存好（有效）' if ok
                    else f'  ! 拿到 token 但校验没过：{why}'))
        done.append({'id': aid, 'name': name, 'valid': ok, 'why': why,
                     'same': same})

    append_log('=' * 60)
    append_log(f'重新获取结束：成功 {len(done)} 个，失败 {len(fail)} 个')
    return {'success': bool(done), 'retoken': True, 'done': done, 'fail': fail,
            'has_same': any(d.get('same') for d in done),
            'accounts': _view_accounts(read_config()),
            'active': read_config().get('active')}


def run_capture(params):
    """抓包任务：装证书 → 设代理 → 等 token → 复原 → 存进指定账号。"""
    if not HAS_TC:
        append_log(f'× 抓包模块不可用：{_TC_ERR}')
        return {'success': False, 'error': _TC_ERR}
    timeout = int(params.get('capture_timeout') or 300)
    port = int(params.get('capture_port') or tc.DEFAULT_PORT)
    with _CAP_LOCK:
        CAP.update(active=True, token=None, error=None, port=port)

    workdir = os.path.join(os.path.dirname(CONFIG_PATH), '.tokencap')
    try:
        cap = tc.TokenCapture(workdir, port=port, log=append_log)
        with _CAP_LOCK:
            CAP['cap_obj'] = cap

        append_log('[1/4] 准备本地证书…')
        if not cap.prepare():
            with _CAP_LOCK:
                CAP.update(active=False, error='证书未安装')
            return {'success': False, 'error': '证书未安装（需要点确认框的【是】）'}

        append_log('[2/4] 启动本地代理…')
        if not cap.start():
            with _CAP_LOCK:
                CAP.update(active=False, error='代理启动失败')
            return {'success': False, 'error': '代理启动失败'}

        append_log(f'[3/4] 等待小程序发请求（最多 {timeout} 秒）…')
        append_log('      → 现在去微信里打开海大场地小程序，随便点两下')
        tok = cap.wait(timeout=timeout, poll=0.3)

        if not tok:
            with _CAP_LOCK:
                CAP.update(active=False, error='超时未抓到')
            append_log('× 超时，没抓到。确认小程序里有实际的网络请求。')
            return {'success': False, 'error': '超时未抓到'}
        append_log(f'✓ 抓到 token（{len(tok)} 字符，前 24 位 {tok[:24]}…）')
        ok, why = b.verify_token(tok)
        uid, _ = b.api_profile(tok) if ok else ('', '')
        cfg = read_config()
        a = acc.get_account(cfg, params.get('acc_id') or cfg.get('active'))
        if a is None:
            a, _ = acc.upsert_login(cfg, '', tok, app_user_id=uid, ok=ok, why=why)
        else:
            a['token'] = tok
            a['token_ok'] = ok
            a['token_why'] = why
            a['token_time'] = datetime.now().strftime('%m-%d %H:%M')
            if uid and not a.get('app_user_id'):
                a['app_user_id'] = uid
            cfg['active'] = a['id']
        acc.save(CONFIG_PATH, cfg)
        append_log(f'✓ 已写入账号库：{a.get("name")}')
        append_log(f'  校验：{why}')
        with _CAP_LOCK:
            CAP.update(active=False, token=tok, error=None)
        return {'success': True, 'token': tok, 'valid': ok, 'why': why,
                'account_id': a.get('id'), 'account_name': a.get('name')}
    finally:
        append_log('[4/4] 复原系统代理…')
        try:
            if CAP.get('cap_obj'):
                CAP['cap_obj'].stop(restore=True)
        except Exception:
            pass
        with _CAP_LOCK:
            CAP.update(active=False, cap_obj=None)


def load_page():
    """优先读外部 index.html（改完刷新即可见），找不到再退回内嵌副本。"""
    candidates = []
    if getattr(sys, 'frozen', False):
        candidates.append(os.path.join(getattr(sys, '_MEIPASS', BASE_DIR), 'index.html'))
    candidates.append(os.path.join(BASE_DIR, 'index.html'))
    for p in candidates:
        try:
            with open(p, encoding='utf-8') as f:
                return f.read()
        except Exception:
            continue
    try:
        import index_data
        import base64
        return base64.b64decode(index_data.B64).decode('utf-8')
    except Exception:
        return '<h1>index.html 缺失</h1>'


def _save_login(cfg, username, tok, ok, why, mfa=False):
    """登录成功后的统一入库动作。返回 (account, is_new)。"""
    uid, uname = ('', '')
    if ok:
        try:
            uid, uname = b.api_profile(tok)
        except Exception:
            uid, uname = ('', '')
    a, is_new = acc.upsert_login(cfg, username, tok, app_user_id=uid,
                                 ok=ok, why=why, mfa=mfa)
    if username:
        a['username'] = username
    acc.save(CONFIG_PATH, cfg)
    return a, is_new


def _one_view(cfg, acc_id):
    for v in acc.view(cfg):
        if v.get('id') == acc_id:
            v['has_profile'] = _has_profile(v.get('id'))
            return v
    return None


def _browser_env():
    """给前端的浏览器环境体检报告。"""
    env = {'python': sys.executable, 'version': sys.version.split()[0],
           'frozen': bool(getattr(sys, 'frozen', False)),
           'playwright': False, 'error': _BL_ERR, 'browsers': {}}
    try:
        if bl is not None:
            env.update(bl.browser_env())
    except Exception as e:
        env['error'] = str(e)
    env['frozen'] = bool(getattr(sys, 'frozen', False))
    env.setdefault('python', sys.executable)
    return env


def _install_playwright(log):
    """源码模式下缺 Playwright 时，用当前这个 Python 装一个。

    只在源码模式有意义 —— 打包好的 exe 已经把 Playwright 打进去了。
    """
    global HAS_BL, _BL_ERR
    import importlib
    import subprocess
    py = sys.executable or 'python'
    log(f'  用 {py} 安装 playwright…')
    try:
        p = subprocess.run([py, '-m', 'pip', 'install', 'playwright'],
                           capture_output=True, text=True, timeout=900)
        out = (p.stdout or '') + (p.stderr or '')
        for ln in out.strip().splitlines()[-12:]:
            log('  | ' + ln[:110])
        if p.returncode != 0:
            return False, 'pip 退出码 %d' % p.returncode
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'
    # 装完让 Python 重新扫描 site-packages，再重新加载模块拿到新的 HAS_PLAYWRIGHT
    importlib.invalidate_caches()
    try:
        importlib.reload(bl)
        HAS_BL = bl.HAS_PLAYWRIGHT
        _BL_ERR = '' if HAS_BL else bl._PW_ERR
    except Exception as e:
        return False, f'重新加载失败：{e}'
    if not HAS_BL:
        return False, '装完了但仍无法导入：' + str(_BL_ERR)
    log('  ✓ Playwright 就绪（用的是系统已装的 Edge/Chrome，不用下载浏览器）')
    return True, ''


def _has_profile(acc_id):
    """这个账号有没有留下浏览器档案（＝ 勾过「信任此设备」）。"""
    try:
        return os.path.isdir(acc.profile_dir(os.path.dirname(CONFIG_PATH), acc_id))
    except Exception:
        return False


def _view_accounts(cfg):
    out = acc.view(cfg)
    for v in out:
        v['has_profile'] = _has_profile(v.get('id'))
    return out


def view_config():
    """给前端的整包配置。

    【V4.2 E4 验收发现】这里原来直接把 acc.load() 的原样配置丢出去，
    于是**混淆后的密码串也一起下发了** —— 而 accounts.view() 明明写的是
    「密码串绝不下发到前端」。界面从来不用这格（只用 has_password 打个徽章），
    纯粹是白送一个泄漏面。这里补上同样的处理。

    注意 token 是**故意**保留完整的：表格里的 👁 看全 / ⧉ 复制要靠它；
    打码那份在 /api/accounts（accounts.view）里，两边用途不同。

    前端回传时 password 这格必然是空的，merge_accounts() 会据此保留库里的原值，
    所以「普通保存」不会把挂机用的密码抹掉。
    """
    cfg = read_config()
    for a in cfg.get('accounts') or []:
        a['has_profile'] = _has_profile(a.get('id'))
        # 顺带补上界面要用的标记（原来缺 has_password，
        # 导致「🔑 密码已存」这个徽章在走 /api/config 渲染时永远不显示）
        a['has_password'] = bool((a.get('password') or '').strip())
        a['has_token'] = bool((a.get('token') or '').strip())
        a['password'] = ''
    return cfg


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _q(self):
        from urllib.parse import parse_qs
        try:
            return parse_qs(urlparse(self.path).query)
        except Exception:
            return {}

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text):
        body = text.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text, filename=None):
        """【V4.2 C3】纯文本响应；给了 filename 就当附件下载。"""
        body = text.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if filename:
            # RFC 5987：文件名里有中文也能被浏览器正确解码
            self.send_header('Content-Disposition',
                             "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                             % (filename, quote(filename)))
        self.end_headers()
        self.wfile.write(body)

    def _begin_browser_login(self, username, password, acc_id,
                             mfa_timeout=300, trusted=False, mfa_pending=False,
                             remember=False):
        """共用的「开真实浏览器走 CAS / MFA」入口。

        三种情形都走它：
          1) 已知账号有档案 + 信任过设备 → 直接免验证试；
          2) 服务端检测到账号开了 MFA → 开浏览器让本人过；
          3) 服务端拿到了 token 但校验不通过（疑似 MFA 仍挂起）→ 同样转浏览器补全。
        统一在这里占用任务槽、建好账号行、spawn 浏览器登录线程。

        remember=True 时，登录成功后把密码（混淆后）存进这个账号，
        以后「重新获取 token / 开抢前自动刷新」都不再需要人输密码。
        """
        if not HAS_BL:
            self._json({'ok': False, 'mfa': True,
                        'msg': '需要多因子认证，但浏览器模块不可用：' + _BL_ERR})
            return
        with _LOCK:
            if STATE['running']:
                self._json({'ok': False, 'mfa': True,
                            'msg': '已有任务在进行，请先停止'})
                return
            STATE['log'] = []
            STATE['result'] = None
            STATE['task'] = ('浏览器登录（试设备信任免验证）' if trusted
                            else '浏览器登录（等你过 MFA）')
        cfg = read_config()
        a = acc.get_account(cfg, acc_id) or acc.find_by_username(cfg, username)
        if a is None:
            a = acc.add_blank(cfg)
        a['username'] = username
        a['mfa_seen'] = True
        cfg['active'] = a['id']
        acc.save(CONFIG_PATH, cfg)
        append_log(('  该账号已有浏览器档案，直接走「信任此设备」免验证…'
                    if trusted else
                    ('  token 已拿到但校验未通过，疑似多因子认证仍挂起，改用浏览器补全…'
                     if mfa_pending else
                     '  密码正确，账号开了多因子认证（MFA）')))
        spawn('browser_login', {
            'username': username, 'password': password,
            'acc_id': a['id'],
            'mfa_timeout': int(mfa_timeout or 300),
            'remember_password': bool(remember),
            # V4.0：固定浏览器档案 —— 勾过「信任此设备」下次免验证
            'profile_dir': acc.profile_dir(os.path.dirname(CONFIG_PATH),
                                           a['id']),
        })
        self._json({'ok': True, 'browser': True, 'trusted': trusted,
                    'mfa_pending': mfa_pending,
                    'account_id': a['id'], 'account_name': a.get('name'),
                    'msg': ('正在用已信任的设备直接登录，若仍需验证请在浏览器里完成'
                            if trusted else
                            '已打开浏览器，请在浏览器里输入密码完成登录'
                            '（如需验证按页面上的提示操作即可）')})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/stop':
            # 双保险：前端已改成 POST，但万一还有旧页面/手输地址走 GET，
            # 这里也照停不误 —— 停止失效时用户只能干等，代价太大。
            _STOP.set()
            self._json({'ok': True})
        elif path in ('/', '/index.html'):
            self._html(load_page())
        elif path == '/api/status':
            with _LOCK:
                self._json({'running': STATE['running'], 'log': STATE['log'],
                            'result': STATE['result'], 'task': STATE['task']})
        elif path == '/api/config':
            self._json(view_config())
        elif path == '/api/accounts':
            cfg = read_config()
            self._json({'ok': True, 'active': cfg.get('active'),
                        'accounts': _view_accounts(cfg)})
        elif path == '/api/capture/status':
            st = {'has_module': HAS_TC}
            if HAS_TC:
                st['ca_installed'] = tc.ca_installed()
                en, sv = tc.get_proxy()
                st['proxy'] = sv if en else ''
            with _CAP_LOCK:
                st.update({'active': CAP['active'], 'port': CAP['port'],
                           'error': CAP['error'], 'has_token': bool(CAP['token'])})
            self._json(st)
        elif path == '/api/browser/status':
            ok, err = bl.browser_available() if HAS_BL or bl else (False, _BL_ERR)
            if not ok and err:
                err += '（若用源码启动：请改用 dist 里的 exe，或用装了 playwright 的 Python 运行）'
            self._json({'ok': ok, 'error': err, 'has_bl': HAS_BL, 'bl_err': _BL_ERR,
                        'env': _browser_env(),
                        'hint': '浏览器登录用于多因子认证：开一个真实浏览器，'
                                '自动填学号密码，MFA 那一步由你手动完成。'})
        elif path == '/api/fingerprint':
            q = self._q()
            rs = b.RandomSource(os.path.join(os.path.dirname(CONFIG_PATH),
                                             '.anti_collision_salt'),
                                passphrase=(q.get('passphrase') or [''])[0])
            self._json({'fingerprint': rs.fingerprint, 'salt': rs.salt[:12] + '…'})
        elif path == '/api/log/info':
            # 【V4.2 C1】告诉前端日志目录在哪、当前这次运行的文件叫什么
            # current 优先给「正在写的这个」；任务已结束就退到最近一次的文件名，
            # 保证抢完之后界面上还能看到「刚才那次写在 run_xxx.log」。
            self._json({'ok': True, 'log_dir': LOG_DIR,
                        'current': _LOG_NAME or _LAST_LOG_NAME or '',
                        'running': _LOG_FILE is not None})
        elif path == '/api/log/export':
            # 【V4.2 C3】把当前会话日志导成 txt 下载。
            # 内容逐行过一遍 redact()，保证导出的文件同样不含凭据 ——
            # 导出本身就是个潜在泄漏面，不能比落盘更松。
            with _LOCK:
                lines = list(STATE['log'])
            body = chr(10).join(redact(x) for x in lines)
            fn = 'tennis_log_%s.txt' % datetime.now().strftime('%Y%m%d_%H%M%S')
            self._text(body, filename=fn)
        elif path == '/api/log/file':
            # 导出【上一次/当前】落盘的运行日志文件本身（已脱敏后写入的）
            q = self._q()
            name = (q.get('name') or ['latest.log'])[0]
            # 只允许读 logs/ 目录里的 .log，挡掉 ../ 之类的路径穿越
            name = os.path.basename(name)
            if not name.endswith('.log'):
                self.send_error(400)
                return
            full = os.path.join(LOG_DIR, name)
            try:
                with open(full, encoding='utf-8') as f:
                    self._text(f.read(), filename=name)
            except Exception:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get('Content-Length', 0) or 0)
        raw = self.rfile.read(length) if length else b'{}'
        try:
            payload = json.loads(raw.decode('utf-8')) if raw else {}
        except Exception:
            payload = {}

        with _LOCK:
            busy = STATE['running']

        if path == '/api/config':
            cfg = read_config()
            if 'accounts' in payload:
                payload = dict(payload)
                payload['accounts'] = acc.merge_accounts(cfg.get('accounts'),
                                                         payload['accounts'])
            self._json(write_config(payload))
            return

        if path == '/api/salt':
            rs = b.RandomSource(os.path.join(os.path.dirname(CONFIG_PATH),
                                             '.anti_collision_salt'),
                                passphrase=payload.get('passphrase') or '')
            rs.reset_salt()
            self._json({'ok': True, 'fingerprint': rs.fingerprint})
            return

        # ---- 账号库 ----
        if path == '/api/accounts/add':
            cfg = read_config()
            name = (payload.get('name') or '').strip()
            a = acc.add_blank(cfg)
            if name:
                a['name'] = name
            acc.save(CONFIG_PATH, cfg)
            self._json({'ok': True, 'account': _one_view(cfg, a['id']),
                        'accounts': _view_accounts(cfg), 'active': cfg.get('active')})
            return

        if path == '/api/accounts/remove':
            cfg = read_config()
            acc.remove(cfg, payload.get('id'))
            acc.save(CONFIG_PATH, cfg)
            self._json({'ok': True, 'accounts': _view_accounts(cfg),
                        'active': cfg.get('active')})
            return

        if path == '/api/accounts/active':
            cfg = read_config()
            if acc.get_account(cfg, payload.get('id')):
                cfg['active'] = payload.get('id')
                acc.save(CONFIG_PATH, cfg)
            self._json({'ok': True, 'active': cfg.get('active')})
            return

        if path == '/api/accounts/verify':
            cfg = read_config()
            a = acc.get_account(cfg, payload.get('id'))
            if not a:
                self._json({'ok': False, 'msg': '找不到这个账号'})
                return
            if not (a.get('token') or '').strip():
                self._json({'ok': False, 'msg': '这个账号还没有 token，先去登录'})
                return
            ok, why = b.verify_token(a['token'].strip())
            a['token_ok'] = ok
            a['token_why'] = why
            acc.save(CONFIG_PATH, cfg)
            self._json({'ok': True, 'valid': ok, 'why': why,
                        'account': _one_view(cfg, a['id'])})
            return

        # ---- 【V4.1】账号密码（只在本机存，供挂机自动刷新用）----
        if path == '/api/accounts/password':
            cfg = read_config()
            a = acc.get_account(cfg, payload.get('id') or payload.get('account_id'))
            if not a:
                self._json({'ok': False, 'msg': '找不到这个账号'})
                return
            remember = bool(payload.get('remember', True))
            pwd = payload.get('password')
            if pwd is None:
                # 只切开关，不改密码内容
                a['remember_password'] = remember
                if not remember:
                    a['password'] = ''
            else:
                a['password'] = acc.encode_password(str(pwd or ''))
                a['remember_password'] = remember and bool(str(pwd or ''))
                if not a['remember_password']:
                    a['password'] = ''
            acc.save(CONFIG_PATH, cfg)
            self._json({'ok': True, 'account': _one_view(cfg, a['id']),
                        'accounts': _view_accounts(cfg)})
            return

        # ---- 【V4.1】立即刷新 token（手动触发，睡前点一次或排查用）----
        if path == '/api/refresh':
            with _LOCK:
                busy_now = STATE['running']
            if busy_now:
                self._json({'ok': False, 'msg': '有任务在跑，先按停止'})
                return
            cfg = read_config()
            scope = (payload.get('scope') or 'all')
            aid = payload.get('account_id') or cfg.get('active')
            todo = [x for x in (cfg.get('accounts') or [])
                    if x.get('enabled', True) and not acc.is_closed(x)
                    and (x.get('username') or '').strip()]
            if scope != 'all':
                todo = [x for x in todo if x.get('id') == aid]
            if not todo:
                self._json({'ok': False, 'msg': '没有可刷新的账号（需要有学号、且没关场）'})
                return
            STATE['task'] = '刷新 token'
            spawn_refresh(todo, int(payload.get('refresh_timeout')
                                    or cfg.get('refresh_timeout') or 900))
            self._json({'ok': True, 'accounts': len(todo)})
            return

        # ---- 重新获取 token（单个 / 批量）----
        if path == '/api/accounts/retoken_plan':
            # 先算一份「待处理清单」，让前端知道哪些账号需要补密码
            ids = payload.get('ids') or None
            only_invalid = bool(payload.get('only_invalid'))
            self._json({'ok': True, 'busy': busy,
                        'targets': _retoken_targets(ids, only_invalid)})
            return

        if path in ('/api/accounts/retoken', '/api/accounts/retoken_all'):
            one = path.endswith('retoken')
            cfg = read_config()
            if busy:
                self._json({'ok': False, 'msg': '已有任务在进行，请先停止'})
                return
            if not HAS_BL:
                self._json({'ok': False,
                            'msg': '浏览器模块不可用：' + _BL_ERR})
                return
            if one:
                ids = [payload.get('id')]
            else:
                ids = payload.get('ids') or None
                if not ids:
                    ids = [t['id'] for t in
                           _retoken_targets(None, bool(payload.get('only_invalid')))]
            targets = _retoken_targets(ids)
            if not targets:
                self._json({'ok': False, 'msg': '没有可处理的账号'
                                                '（账号要有学号，且没被停用）'})
                return
            pwds = dict(payload.get('passwords') or {})
            if one:
                pw = (payload.get('password') or '').strip()
                if pw:
                    pwds[str(targets[0]['id'])] = pw
            need = [t for t in targets if t['need_password']
                    and not (pwds.get(str(t['id'])) or '').strip()]
            if need:
                self._json({'ok': False, 'need_password': True,
                            'need': need,
                            'msg': '这些账号还没有设备信任档案，需要门户密码：'
                                   + '、'.join(t['name'] for t in need)})
                return
            with _LOCK:
                STATE['log'] = []
                STATE['result'] = None
                STATE['task'] = ('重新获取 token（%s）' % targets[0]['name']
                                 if len(targets) == 1
                                 else f'批量重新获取 token（{len(targets)} 个账号）')
            append_log(STATE['task'])
            append_log('  有设备信任档案的账号会自动跳过验证，'
                       '需要你操作的步骤会弹出浏览器。')
            spawn('retoken', {'ids': [t['id'] for t in targets],
                              'passwords': pwds,
                              'mfa_timeout': payload.get('mfa_timeout')
                              or cfg.get('mfa_timeout') or 300})
            self._json({'ok': True, 'started': True, 'count': len(targets),
                        'task': STATE['task'],
                        'msg': f'已开始，共 {len(targets)} 个账号。'
                               f'浏览器弹出时按提示操作即可。'})
            return

        if path == '/api/browser/install':
            if getattr(sys, 'frozen', False):
                self._json({'ok': False,
                            'msg': '这是打包好的 exe，Playwright 已经打进去了，不用再装。'})
                return
            ok, why = _install_playwright(append_log)
            self._json({'ok': ok, 'msg': why, 'env': _browser_env()})
            return

        if path == '/api/accounts/cleardevice':
            cfg = read_config()
            a = acc.get_account(cfg, payload.get('id'))
            if not a:
                self._json({'ok': False, 'msg': '找不到这个账号'})
                return
            d = acc.profile_dir(os.path.dirname(CONFIG_PATH), a['id'])
            existed = os.path.isdir(d)
            ok = bl.clear_profile(d, log=append_log) if HAS_BL or bl else False
            if not ok and existed:
                import shutil
                try:
                    shutil.rmtree(d)
                    ok = True
                except Exception as e:
                    append_log(f'  × 清除失败：{e}')
            a['mfa_seen'] = False
            acc.save(CONFIG_PATH, cfg)
            self._json({'ok': bool(ok), 'had': existed,
                        'account': _one_view(cfg, a['id'])})
            return

        # ---- 以下接口不占用「抢单任务」的互斥锁 ----
        if path == '/api/login':
            # 【V4.1】无 UI 入口的备用登录：直接打 H5 登录接口拿 token，
            # 不需要浏览器、不需要抓包、不需要证书，全程无人工介入。
            # 保留它是因为「定时刷新 token」等无人值守场景需要一条纯 HTTP 的登录路径。
            username = (payload.get('username') or '').strip()
            password = payload.get('password') or ''
            login_type = (payload.get('loginType') or '01').strip()
            if not username or not password:
                self._json({'ok': False, 'msg': '请填用户名和密码'})
                return
            tok, err = b.api_login(username, password, login_type)
            if not tok:
                self._json({'ok': False, 'msg': err or '登录失败'})
                return
            ok, why = b.verify_token(tok)
            cfg = read_config()
            a, is_new = _save_login(cfg, username, tok, ok, why)
            self._json({'ok': True, 'token': tok, 'valid': ok, 'why': why,
                        'preview': tok[:24] + '…', 'is_new': is_new,
                        'account': _one_view(cfg, a['id']),
                        'accounts': _view_accounts(cfg)})
            return

        # 方式一：统一身份认证（CAS）—— 学号 + 门户密码
        if path == '/api/caslogin':
            username = (payload.get('username') or '').strip()
            password = payload.get('password') or ''
            if not username or not password:
                self._json({'ok': False, 'msg': '请填学号和密码'})
                return
            # 【V4.0】方式一统一「用浏览器完成登录」—— 点完就弹浏览器，
            # 在浏览器里输密码 / 过 MFA，token 由浏览器内实时捕获。
            # 这样无论账号是否开了多因子认证，浏览器都会弹出来（与 V3 体验一致），
            # 不再先偷偷走服务端 CAS、把开了 MFA 的账号误判成「登录成功、不开浏览器」。
            cfg_pre = read_config()
            known = acc.find_by_username(cfg_pre, username)
            remember = bool(payload.get('remember'))
            if known and known.get('mfa_seen') and _has_profile(known['id']):
                # 已经信任过设备的账号：同一份浏览器档案，多半免验证直接拿 token，
                # 但浏览器照样弹出来，你确认一下即可。
                self._begin_browser_login(username, password, known['id'],
                                          mfa_timeout=payload.get('mfa_timeout'),
                                          trusted=True, remember=remember)
            elif HAS_BL:
                self._begin_browser_login(username, password, None,
                                          mfa_timeout=payload.get('mfa_timeout'),
                                          remember=remember)
            else:
                # 浏览器模块不可用（源码模式没装 Playwright）：退回服务端 CAS 兜底
                tok, err, extra = b.api_cas_login(username, password)
                if not tok:
                    self._json({'ok': False, 'msg': err or 'CAS 登录失败（且浏览器模块不可用，无法开浏览器）'})
                    return
                ok, why = b.verify_token(tok)
                cfg = read_config()
                a, is_new = _save_login(cfg, username, tok, ok, why)
                if remember:
                    a['password'] = acc.encode_password(password)
                    a['remember_password'] = True
                    acc.save(CONFIG_PATH, cfg)
                self._json({'ok': True, 'token': tok, 'valid': ok, 'why': why,
                            'preview': tok[:24] + '…', 'is_new': is_new,
                            'account': _one_view(cfg, a['id']),
                            'accounts': _view_accounts(cfg),
                            'active': cfg.get('active')})
            return

        if path == '/api/capture/stop':
            with _CAP_LOCK:
                cap = CAP.get('cap_obj')
            if cap:
                append_log('  收到停止信号，正在复原…')
                cap.stop(restore=True)
            _STOP.set()
            self._json({'ok': True})
            return

        if path == '/api/ca/remove':
            if not HAS_TC:
                self._json({'ok': False, 'msg': '模块不可用'})
                return
            ok = tc.uninstall_ca(log=append_log)
            self._json({'ok': ok})
            return

        if path == '/api/ca/install':
            if not HAS_TC:
                self._json({'ok': False, 'msg': '模块不可用：' + str(_TC_ERR)})
                return
            workdir = os.path.join(os.path.dirname(CONFIG_PATH), '.tokencap')
            ca = tc.CertAuthority(workdir)
            ca.ensure_ca()
            ok = tc.install_ca(ca.ca_cert_path, log=append_log)
            self._json({'ok': ok, 'fingerprint': ca.fingerprint})
            return

        if busy and path != '/api/stop':
            self._json({'ok': False, 'msg': '已有任务在进行，请先停止'})
            return

        if path == '/api/stop':
            _STOP.set()
            self._json({'ok': True})
            return

        with _LOCK:
            STATE['log'] = []
            STATE['result'] = None

        if path == '/api/preview':
            p = params_from(payload)
            p['submit'] = False
            scope = (payload.get('scope') or 'current')
            if scope == 'all':
                STATE['task'] = '侦察空场（全部账号）'
                spawn('preview_multi', p)
            else:
                STATE['task'] = '侦察空场'
                spawn('preview', p)
        elif path == '/api/start_now':
            p = params_from(payload)
            p['submit'] = True
            p['schedule'] = None
            if (payload.get('scope') or 'current') == 'all':
                STATE['task'] = '立即抢单（全部账号）'
                spawn('book_multi', p)
            else:
                STATE['task'] = '立即抢单'
                spawn('book', p)
        elif path == '/api/start_sched':
            p = params_from(payload)
            p['submit'] = True
            p['schedule'] = parse_schedule(payload.get('schedule') or
                                           read_config().get('schedule'))
            if not p['schedule']:
                self._json({'ok': False, 'msg': '定时时间格式不对'})
                return
            if (payload.get('scope') or 'current') == 'all':
                STATE['task'] = '定时抢单（全部账号）'
                spawn('book_multi', p)
            else:
                STATE['task'] = '定时抢单'
                spawn('book', p)
        elif path == '/api/bench':
            STATE['task'] = '接口测速'
            spawn('bench', params_from(payload))
        elif path == '/api/diagnose':
            STATE['task'] = '有效性检测'
            spawn('diagnose', params_from(payload))
        elif path == '/api/capture':
            cfg = read_config()
            p = params_from(payload)
            p['acc_id'] = payload.get('account_id') or cfg.get('active')
            STATE['task'] = '抓 token'
            spawn('capture', p)
        else:
            self.send_error(404)
            return

        self._json({'ok': True})


class Server(ThreadingHTTPServer):
    # Windows 下 SO_REUSEADDR 允许两个进程同时绑同一端口，
    # 会导致「改了代码却还是旧页面」。显式关掉，保证一个端口只有一个实例。
    allow_reuse_address = False
    daemon_threads = True


def boost_timer_resolution():
    """【V4.2 B4】把 Windows 的系统计时精度从默认 ~15.6ms 提到 1ms。

    倒计时最后一段是 sleep(0.005) / sleep(0) 忙等，粒度全靠系统时钟节拍。
    实测这台机器当前的 overshoot 只有 ~0.3ms —— 但那是**因为别的进程
    （浏览器/IDE）已经把全局精度顶上去了**，抢场那会儿要是只有我们一个进程在跑，
    sleep 就可能被拉到 15ms 粒度，等于第一枪晚十几毫秒。
    显式声明一次，不依赖别人的进程。

    返回一个 restore 函数；非 Windows 或 winmm 不可用时返回空操作。
    """
    try:
        import ctypes
        winmm = ctypes.windll.winmm
        if winmm.timeBeginPeriod(1) != 0:
            return lambda: None
        return lambda: winmm.timeEndPeriod(1)
    except Exception:
        return lambda: None


def main():
    restore_timer = boost_timer_resolution()
    try:
        server = Server((HOST, PORT), Handler)
    except OSError as e:
        print(f'× 端口 {PORT} 已被占用（{e}）')
        print('  多半是上一次的程序没关干净。排查：')
        print(f'      netstat -ano | findstr :{PORT}')
        print('      taskkill /PID <PID> /F')
        try:
            input('\n按回车键退出...')
        except EOFError:
            pass
        restore_timer()
        return

    url = f'http://{HOST}:{PORT}'
    print('=' * 58)
    print('  海南大学网球定场 V4.3 · Tennis Everyday 已启动')
    print(f'  {url}')
    print('  多账号账号库 · 每账号独立时间窗口与场地优先级')
    print('  关闭此窗口即停止服务')
    print('=' * 58)
    try:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止。')
    finally:
        server.server_close()
        try:
            restore_timer()
        except Exception:
            pass


if __name__ == '__main__':
    main()
