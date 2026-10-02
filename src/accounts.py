# -*- coding: utf-8 -*-
"""
账号库（V4.3）
================================================================================
V3.1 只有一个「全局 token」，换个人就得重新登录、改一次时间窗口和场地顺序。
V4.0 把它升级成**账号库**：

  · 一个账号 = 一行（名字 / 学号 / 用户ID / Bearer token / 自己的时间窗口 / 自己的场地优先级）
  · 方式一（学号 + 门户密码）反复登录，就能往库里追加账号二、账号三……
  · 每个账号在界面上是一个「浏览器标签页」，切过去看到的就是它自己的
    时间窗口 + 场地优先顺序
  · 抢单时可以「只用当前账号」或「按库里顺序逐个尝试，抢到即停」

只保存 token，不保存密码（密码只在登录那一次请求里用到）。
================================================================================
"""
import json
import os
import time
from datetime import date as date_cls, datetime, timedelta

CN_NUM = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十']

# 与 booker.COURTS_FALLBACK 保持一致（不 import booker，避免循环依赖）
ALL_COURTS = ['1号场', '2号场', '3号场', '4号场', '5号场', '6号场', '7号场']
DEFAULT_ORDER = ['4号场', '5号场', '6号场', '7号场', '2号场', '3号场', '1号场']


def _cn(n):
    return CN_NUM[n - 1] if 1 <= n <= 10 else str(n)


def new_id(cfg):
    used = {a.get('id') for a in (cfg.get('accounts') or [])}
    i = 1
    while ('acc%d' % i) in used:
        i += 1
    return 'acc%d' % i


def new_name(cfg):
    used = {a.get('name') for a in (cfg.get('accounts') or [])}
    n = len(cfg.get('accounts') or []) + 1
    while ('账号' + _cn(n)) in used:
        n += 1
    return '账号' + _cn(n)


def default_account(cfg=None, name=None):
    """新建一个空白账号（未登录状态）。"""
    a = {
        'id': new_id(cfg or {}),
        'name': name or new_name(cfg or {}),
        'username': '',
        # 【V4.1】挂机自动刷新用：只有用户在本机勾选「记住密码」才会落盘，
        # 且存的是混淆后的串（见 encode_password），不是明文。
        'password': '',
        'remember_password': False,
        'app_user_id': '',
        'token': '',
        'token_ok': None,       # True/False/None(未验证)
        'token_why': '',
        'token_time': '',       # 最近一次拿到 token 的时间
        'mfa_seen': False,      # 该号是否遇到过 MFA
        'enabled': True,        # 是否参与抢单
        # 以下是「每个账号自己的」定场参数
        'window_start': '18:00',
        'window_end': '22:00',
        'court_order': DEFAULT_ORDER[:],
        'court_enabled': {n: True for n in ALL_COURTS},
        'sort_strategy': 'court_rand',
    }
    return a


def default_config():
    return {
        'version': '5.0',
        'accounts': [],
        'active': '',
        # 以下为所有账号共用的全局参数
        'target_date': (date_cls.today() + timedelta(days=2)).isoformat(),
        'anti_collision': True,
        'passphrase': '',
        'jitter_ms': 120,
        'min_interval': 0.12,
        'burst': 3,
        'scout_workers': 7,
        'refresh_rounds': 4,
        # 【V4.2 B3】并发首发：0=纯串行（默认，零多单风险）。
        # V4.1 这两处白名单都漏了它 → save() 经 migrate() 重建时被丢弃，
        # 界面设了也不生效，booker 永远拿到 0。这正是 MEMORY.md 记的
        # 「新增全局参数必须 default_config 和 migrate 两处都加」那个坑。
        'parallel_first': 0,
        'not_open_max_retries': 120,
        'unknown_max_retries': 3,
        'schedule': '07:59:58',
        'one_per_day': True,
        # 多账号并行时：抢到一个是否立刻叫停其他账号。
        # True = 不占多个场；False = 各抢各的（账号各抢不同时间段时用）
        'stop_on_first': True,
        'max_days_ahead': 2,
        # 以下三项无 UI 入口，只在代码里被读取、用作默认值兜底，勿删。
        # 供「Token 自动获取」面板回填
        'capture_timeout': 300,
        'capture_port': 8899,
        'mfa_timeout': 300,
        # 【V4.1】挂机自动刷新 token
        'auto_refresh_token': True,   # 定时抢时，开抢前自动重登拿新 token
        'refresh_lead_min': 15,       # 提前多少分钟刷新（默认开抢前 15 分钟）
        'refresh_timeout': 900,       # 单次刷新最多等多少秒（默认 15 分钟，够等到开抢）
        # 【V4.4】auto_roll_date（目标日期自动跟随）已拆除：
        # 它只做算术（今天 + N 天）不核对现实，会把早已抢光的日期当成"正确日期"，
        # 给人错误的安全感。现在日期完全手动，程序只报「当天还剩几片空场」。
        # 【V4.3】开火提前量的下限（毫秒）。
        # 校时只能解决「本机钟和服务器钟不一样快」，解决不了**服务端放号任务
        # 自己晚触发** —— 2026-09-25 实测：按校时结果卡着 08:00:00 发过去，
        # 服务端照样回「当前时间未开放预约」。
        # 早发的代价是几发「未开放」（本来也会原地继续锤），晚发的代价是
        # 眼睁睁看着别人抢走，两边不对称 → 宁可早。
        # 实际提前量 = max(实测时钟偏差, 这个值)。
        'fire_lead_ms': 200,
        # 【V5 · 场地管理】真实退订开关。**默认 False = 干运行**。
        # False 时点「退订」只做校验 + 预演 + 打日志，**绝不发退款请求**
        # —— 退图和下单一样是不可逆的写操作，没经过真人确认不许落地。
        'refund_live_enabled': False,
    }


# ---------------------------------------------------------------------------
# 【V4.1】密码的本地存储
# ---------------------------------------------------------------------------
# 说明：这是**混淆**，不是加密 —— 目的是让 config.json 里不出现一眼可读的
# 明文密码（顺手打开、截图、误传时不会直接泄露），挡不住真想解的人。
# 真正的防线是：config.json 已在 .gitignore 里，永远不会进仓库。
_OBF_SALT = b'hainanu-tennis-v41'


def encode_password(raw):
    """明文密码 → 可落盘的混淆串。空密码返回空串。"""
    raw = (raw or '')
    if not raw:
        return ''
    import base64
    data = raw.encode('utf-8')
    out = bytearray()
    for i, ch in enumerate(data):
        out.append(ch ^ _OBF_SALT[i % len(_OBF_SALT)])
    return 'v1:' + base64.urlsafe_b64encode(bytes(out)).decode('ascii')


def decode_password(stored):
    """混淆串 → 明文密码。识别不了（空 / 老格式）就原样返回。"""
    stored = (stored or '')
    if not stored:
        return ''
    if not stored.startswith('v1:'):
        return stored          # 兼容：早期万一存过明文
    import base64
    try:
        data = base64.urlsafe_b64decode(stored[3:].encode('ascii'))
    except Exception:
        return ''
    out = bytearray()
    for i, ch in enumerate(data):
        out.append(ch ^ _OBF_SALT[i % len(_OBF_SALT)])
    try:
        return out.decode('utf-8')
    except Exception:
        return ''


def account_password(a):
    """取一个账号可用于自动登录的明文密码；没记住密码则返回空串。"""
    if not a:
        return ''
    if not a.get('remember_password'):
        return ''
    return decode_password(a.get('password') or '')


# ---------------------------------------------------------------------------
# 读 / 写 / 迁移
# ---------------------------------------------------------------------------

def _normalize_account(a, idx=1, cfg=None):
    """补齐缺字段，保证前端拿到的每一行结构一致。

    只认 default_account() 里定义过的键 —— 前端回传的临时字段
    （has_profile / preview / masked / index / active）不会被写进 config.json。
    """
    d = default_account(cfg)
    src = a or {}
    for k in list(d.keys()):
        if k in src:
            d[k] = src[k]
    if not d.get('id'):
        d['id'] = new_id(cfg or {})
    if not d.get('name'):
        d['name'] = d.get('username') or ('账号' + _cn(idx))
    order = [n for n in (d.get('court_order') or []) if n in ALL_COURTS]
    for n in ALL_COURTS:
        if n not in order:
            order.append(n)
    d['court_order'] = order
    en = dict(d.get('court_enabled') or {})
    d['court_enabled'] = {n: bool(en.get(n, True)) for n in ALL_COURTS}
    d['enabled'] = bool(d.get('enabled', True))
    d['token'] = (d.get('token') or '').strip()
    return d


def migrate(raw):
    """把 V3.1 的「单 token」旧配置升级成 V4.0 的账号库。

    旧结构：{token, app_user_id, window_*, court_order, court_enabled, ...}
    新结构：{accounts:[{...}], active, 全局参数...}
    """
    cfg = default_config()
    raw = dict(raw or {})

    if raw.get('accounts'):
        # 逐个 append，不能写成列表推导式 —— 那样 new_id() 在同一轮里
        # 看到的仍是旧的 accounts，会给多个无 id 的账号发同一个 acc1。
        cfg['accounts'] = []
        for i, a in enumerate(raw['accounts']):
            cfg['accounts'].append(_normalize_account(a, i + 1, cfg))
    elif raw.get('token'):
        # 旧版只有一个 token → 收编成「账号一」
        a = default_account(cfg, '账号一')
        a['username'] = raw.get('username') or ''
        a['app_user_id'] = str(raw.get('app_user_id') or '')
        a['token'] = raw.get('token') or ''
        a['window_start'] = raw.get('window_start') or '18:00'
        a['window_end'] = raw.get('window_end') or '22:00'
        if raw.get('court_order'):
            a['court_order'] = list(raw['court_order'])
        if raw.get('court_enabled'):
            a['court_enabled'] = dict(raw['court_enabled'])
        a['sort_strategy'] = raw.get('sort_strategy') or 'court_rand'
        cfg['accounts'] = [_normalize_account(a, 1, cfg)]

    # 全局参数照搬（账号专属的那几个不搬，它们已经在各账号里了）
    for k in ('target_date', 'anti_collision', 'passphrase', 'jitter_ms',
              'min_interval', 'burst', 'scout_workers', 'refresh_rounds',
              'parallel_first',
              'not_open_max_retries', 'unknown_max_retries', 'schedule',
              'one_per_day', 'stop_on_first', 'max_days_ahead', 'capture_timeout',
              'capture_port', 'mfa_timeout',
              # 【V4.1】挂机自动刷新
              'auto_refresh_token', 'refresh_lead_min', 'refresh_timeout',
              # 【V4.4】auto_roll_date 已拆除，旧配置里残留的这个键直接丢弃
              # 【V4.3】开火提前量下限
              'fire_lead_ms',
              # 【V5 · 场地管理】真实退订开关（默认 False，见 default_config）
              'refund_live_enabled'):
        if k in raw:
            cfg[k] = raw[k]
    # 兼容旧键名
    if not cfg.get('target_date') and raw.get('date'):
        cfg['target_date'] = raw['date']

    if not cfg['accounts']:
        a = _normalize_account(default_account(cfg, '账号一'), 1, cfg)
        cfg['accounts'] = [a]
    _dedupe_ids(cfg)
    if not cfg.get('active') or not get_account(cfg, cfg['active']):
        cfg['active'] = cfg['accounts'][0]['id']
    return cfg


def _dedupe_ids(cfg):
    """保证 accounts 里 id 唯一。

    重复 id 会让「按 id 定位账号」串号：切换标签页、删号、验 token
    全都可能作用到错误的账号上。任何进来的配置都在 migrate 里过一遍。
    """
    accs = cfg.get('accounts') or []
    seen = set()
    for a in accs:
        aid = a.get('id')
        if not aid or aid in seen:
            aid = new_id(cfg)
            a['id'] = aid
        seen.add(aid)
    return cfg


def load(path):
    cfg = default_config()
    try:
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                cfg = migrate(json.load(f) or {})
    except Exception:
        cfg = migrate({})
    return cfg


def save(path, cfg):
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    except Exception:
        pass
    cfg = migrate(cfg)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


# ---------------------------------------------------------------------------
# 查询 / 增删改
# ---------------------------------------------------------------------------

def get_account(cfg, acc_id):
    for a in (cfg.get('accounts') or []):
        if a.get('id') == acc_id:
            return a
    return None


def find_by_username(cfg, username):
    u = (username or '').strip()
    if not u:
        return None
    for a in (cfg.get('accounts') or []):
        if (a.get('username') or '').strip() == u:
            return a
    return None


def find_blank(cfg):
    """找一行「还没登录过」的空账号，用来接住登录结果。

    优先用当前选中的那一行（用户刚点 ＋ 建出来的就会是 active），
    否则用最后一行 —— 这样「点 ＋ 再登录」落到新行上，符合直觉。
    """
    blanks = [a for a in (cfg.get('accounts') or [])
              if not (a.get('token') or '').strip()
              and not (a.get('username') or '').strip()]
    if not blanks:
        return None
    act = cfg.get('active')
    for a in blanks:
        if a.get('id') == act:
            return a
    return blanks[-1]


def upsert_login(cfg, username, token, app_user_id='', ok=None, why='', mfa=False):
    """登录成功后写库：同学号更新原账号，没有就新建一行。返回 (account, is_new)。"""
    username = (username or '').strip()
    a = find_by_username(cfg, username) if username else None
    is_new = False
    if a is None:
        # 有空行就先用空行（用户点过 ＋ 的情况），否则追加一行新的
        a = find_blank(cfg)
        if a is None:
            a = default_account(cfg)
            cfg.setdefault('accounts', []).append(a)
        is_new = True
    a['username'] = username or a.get('username') or ''
    a['token'] = (token or '').strip()
    if app_user_id:
        a['app_user_id'] = str(app_user_id)
    a['token_ok'] = ok
    a['token_why'] = why or ''
    a['token_time'] = datetime.now().strftime('%m-%d %H:%M')
    if mfa:
        a['mfa_seen'] = True
    cfg['active'] = a['id']
    return a, is_new


def add_blank(cfg):
    a = default_account(cfg)
    cfg.setdefault('accounts', []).append(a)
    cfg['active'] = a['id']
    return a


def remove(cfg, acc_id):
    accs = cfg.get('accounts') or []
    cfg['accounts'] = [a for a in accs if a.get('id') != acc_id]
    if cfg.get('active') == acc_id:
        cfg['active'] = cfg['accounts'][0]['id'] if cfg['accounts'] else ''
    return cfg


def merge_accounts(stored, incoming):
    """前端整包保存时用：按 id 合并。

    两条保护：
      1. 前端没带 token（或带了空串）→ 保留库里原有的 token，绝不被抹掉；
      2. 前端漏传的账号 → 原样保留在末尾。
    删除账号走专门的 /api/accounts/remove，不会因为某次保存少传一个就丢号。
    """
    by_id = {a.get('id'): a for a in (stored or [])}
    out = []
    seen = set()
    used = set()
    for a in (incoming or []):
        old = by_id.get(a.get('id'))
        if old is None:
            # 前端漏了 id（或 id 对不上）：退回按学号、再按名字匹配，
            # 免得同一个号被当成新号追加成重名两行。
            old = _match_fallback(stored, a, used)
        if old is not None:
            a['id'] = old.get('id')          # 认祖归宗，id 保持稳定
            if not (a.get('token') or '').strip():
                a['token'] = old.get('token', '')
            # 【V4.1】密码同理：前端从来不回传密码串（view() 里已清空），
            # 所以空 = 「没改」，必须沿用库里的，绝不能被一次普通保存抹掉。
            if not (a.get('password') or '').strip():
                a['password'] = old.get('password', '')
                a['remember_password'] = bool(old.get('remember_password'))
        out.append(a)
        if a.get('id'):
            seen.add(a['id'])
            used.add(a['id'])
    for a in (stored or []):
        if a.get('id') not in seen:
            out.append(a)
    return out


def _match_fallback(stored, a, used):
    """id 没撞上时，按学号 → 名字依次兜底匹配一个还没被认领的旧账号。"""
    for key in ('username', 'name'):
        v = (a.get(key) or '').strip()
        if not v:
            continue
        for s in (stored or []):
            if s.get('id') in used:
                continue
            if (s.get(key) or '').strip() == v:
                return s
    return None


def preview(token, n=18):
    t = (token or '').strip()
    if not t:
        return ''
    return t[:n] + ('…' if len(t) > n else '')


def mask(token, n=18):
    """打码显示：前 18 位 + 圆点，中间正文不泄露。"""
    t = (token or '').strip()
    if not t:
        return ''
    return t[:n] + '…' + '•' * 8 + f'（共 {len(t)} 位）'


def view(cfg):
    """给前端的账号列表：附带 preview / masked，方便直接渲染。"""
    out = []
    for i, a in enumerate(cfg.get('accounts') or [], 1):
        d = dict(a)
        d['index'] = i
        d['preview'] = preview(a.get('token'))
        d['masked'] = mask(a.get('token'))
        d['has_token'] = bool((a.get('token') or '').strip())
        d['active'] = (a.get('id') == cfg.get('active'))
        d['has_password'] = bool((a.get('password') or '').strip())
        # 密码串（哪怕是混淆过的）绝不下发到前端 —— 只给一个「存了没有」的标记。
        # 前端回传时这格必然是空的，merge_accounts() 会据此保留库里的原值。
        d['password'] = ''
        out.append(d)
    return out


def enabled_court_count(a):
    """该账号勾选（开启）的场地数量。"""
    ce = a.get('court_enabled') or {}
    return sum(1 for v in ce.values() if v)


def is_closed(a):
    """【V4.3 一键关场】所有场地都关了 → 今天这个账号不抢、不发任何请求。

    与账号级的 `enabled`（彻底停用）是两套开关：
      · enabled=False      = 这个账号整个不用了（旧逻辑，仍生效）；
      · 场地全关（关场）   = 今天这一场不想用这个号，但账号/密码/信任都还在，
                            明天把场地勾回来就能恢复，不用重新登录。
    """
    return enabled_court_count(a) == 0


def runnable(cfg):
    """参与抢单的账号：启用 + 至少勾了一片场地 + 有 token。

    关场（所有场地都关）的账号不算 runnable —— 既不会进抢单列表，
    也不会在开抢前被刷新 token（见 app_server.refresh_all_accounts）。
    """
    return [a for a in (cfg.get('accounts') or [])
            if a.get('enabled', True) and not is_closed(a)
            and (a.get('token') or '').strip()]


def profile_dir(base_dir, acc_id):
    """该账号的浏览器档案目录（V4.0：用来留住 CAS 的「信任此设备」）。

    放在 config.json 同级的 .profiles/<账号id> 下 —— exe 放哪它就跟着在哪，
    换电脑时把整个目录拷走即可，不想要了删掉这个子目录就行。
    """
    return os.path.join(base_dir or '.', '.profiles', str(acc_id or 'default'))
