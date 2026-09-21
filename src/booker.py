# -*- coding: utf-8 -*-
"""
海南大学网球订场 V2 · 核心引擎
================================================================================
相对 V1(v5) 的六项改造，逐条对应你的需求：

【需求1】每人每天只能订 1 小时
  - SLOT_MINUTES 死锁 60，任何入口都无法改成别的时长；
  - 一天最多成交 1 单：成功即全局熔断（不再尝试任何其它候选）；
  - 开抢前先查「我的订单」，若目标日期当天已有 1 单 → 直接拒绝，绝不重复扣费。

【需求2】场地优先级可拖动
  - court_order 是一个有序列表（前端拖出来的），规划器严格按它排序；
  - 提供 4 种排序策略，默认「场地优先 + 时段随机」。

【需求3】时间窗口（如 06:00-22:00）
  - window_start / window_end 之间所有整点 1 小时都是候选；
  - 与场地开放表（周一~周日）求交，与 getDay 占用求差 → 真实可抢集合。

【需求4】速率
  - 实测结论见 README；引擎内置 benchmark() 可一键复测；
  - RateGovernor 统一控制最小间隔 + 随机抖动，防止自伤式高频。

【需求5】查询不消耗额度
  - getDay 是只读查询，不写库、不占额度 → 可高频并发侦察；
  - 采用「并发侦察 → 挑空场 → 串行精确打击」，每 N 轮刷新一次空场图。

【需求6】反脚本冲突随机化（不用时间种子）
  - RandomSource 用「持久随机盐文件 + os.urandom + 可选个人口令」做种，
    绝不使用 time.time()，因此同时启动的多份脚本序列互不相同；
  - 随机化对象是「候选尝试顺序」，让不同脚本分散到不同时段/场地，减少正面撞车。

================================================================================
接口与鉴权（2026-09 抓包 + 2026-09-15 复测）
  - POST /app/pro/place/list       拉场地      （不校验身份）
  - POST /app/order/detail/getDay  查占用      （校验身份，只读）
  - POST /app/order/master/submitOrder 下单    （校验身份，直接扣校园卡）
  - POST /app/order/detail/list    我的订单    （校验身份，用于「一天一单」保护）
  - token 失效时统一返回 HTTP 200 + body {"code":401,"msg":"...认证失败..."}
    ⇒ 不能只看 HTTP 状态码，必须看 body 的 code。
================================================================================
"""
import base64
import hashlib
import json
import os
import random
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date as date_cls, datetime, timedelta, timezone

import requests

try:
    requests.packages.urllib3.disable_warnings()
except Exception:
    pass

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BASE_URL = 'https://hdscs.hainanu.edu.cn'
PATH_PLACES = '/app/pro/place/list'
PATH_GETDAY = '/app/order/detail/getDay'
PATH_SUBMIT = '/app/order/master/submitOrder'
PATH_MY_ORDERS = '/app/order/detail/list'

# 2026-09-16 从 H5 前端扒出的登录入口（与小程序同一后端，无需微信、无需抓包）
#   Fk = e => Ze.post("/login", e)   → {username, password, loginType:"01"}
PATH_LOGIN = '/login'
PATH_GETINFO = '/getInfo'

PRODUCT_ID = '2721318797597070029'

# 【需求1】一次只能订 1 小时 —— 硬锁 60，不接受外部覆盖
SLOT_MINUTES = 60

UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI '
    'MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) '
    'UnifiedPCWindowsWechat(0xf2541b37) XWEB/20089 miniProgram/wx559b66d8c8ed12ec'
)

# CAS 必须用**普通浏览器** UA：带 MicroMessenger/miniProgram 的话，
# 统一身份认证会返回另一套页面（拿不到 pwdEncryptSalt）。
UA_BROWSER = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36'
)

WEEKDAY_CN = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

# 场地兜底表（2026-09-15 从 place/list 实测校准，共 7 片）
COURTS_FALLBACK = [
    {'name': '1号场', 'sku': '4963330595780290769'},
    {'name': '2号场', 'sku': '3996377011407863139'},
    {'name': '3号场', 'sku': '6246636039779204615'},
    {'name': '4号场', 'sku': '6103487680574368237'},
    {'name': '5号场', 'sku': '3559920284380324671'},
    {'name': '6号场', 'sku': '743241785440262681'},
    {'name': '7号场', 'sku': '3268579703001472977'},
]

# 兜底开放表（真实数据，place/list 拉不到时兜底）
_GAP = [['18:00', '19:00'], ['21:00', '22:00']]      # 周一~周四
_FULL = [['18:00', '22:00']]                          # 周五~周日
FALLBACK_WEEKLY = {
    '1号场': {wd: [['21:00', '22:00']] for wd in WEEKDAY_CN[:5]} | {wd: _FULL for wd in WEEKDAY_CN[5:]},
    **{f'{i}号场': ({wd: _GAP for wd in WEEKDAY_CN[:4]} | {wd: _FULL for wd in WEEKDAY_CN[4:]})
       for i in range(2, 8)},
}

# 排序策略
SORT_COURT = 'court'                    # 严格按拖动顺序（同场地内按时间）
SORT_TIME = 'time'                      # 先按时间，同时段再按拖动顺序
SORT_SHUFFLE = 'shuffle'                # 全随机（反冲突最强）
SORT_COURT_RANDOM_TIME = 'court_rand'   # 场地按拖动顺序，时段随机（默认·推荐）
SORT_LABELS = {
    SORT_COURT: '严格按拖动顺序',
    SORT_TIME: '时间优先（早的先抢）',
    SORT_SHUFFLE: '完全随机（反冲突最强）',
    SORT_COURT_RANDOM_TIME: '场地按拖动顺序 + 时段随机（推荐）',
}

VERDICT_CN = {
    'success': '下单成功',
    'auth_error': 'Token 失效',
    'no_permission': '无权限',
    'not_open': '未开放预约',
    'conflict': '时段已被占',
    'pay_error': '支付/余额问题',
    'limit_reached': '今日额度已满',
    'network_error': '网络异常',
    'rate_limited': '被限流',
    'unknown': '响应不明确',
}

NOT_OPEN_KEYWORDS = ['未开放预约', '未开放', '尚未开放', '不在开放时间', '未到预约时间',
                     '暂未开放', '不在可预约', '超出可提前', '超过可提前']
CONFLICT_KEYWORDS = ['冲突', '已被', '已满', '已预约', '已订', '不能', '存在',
                     'exist', 'occupied', 'not available', 'repeat']
PERM_KEYWORDS = ['无权限', '没有权限', '权限不足', '无权访问', '禁止访问', 'forbidden',
                 'no permission', '不合法', '非法请求']
PAY_KEYWORDS = ['余额不足', '请先充值', '请充值', '支付失败', 'insufficient', '扣款失败']
LIMIT_KEYWORDS = ['超出限制', '超过限制', '限购', '每人每天', '每日限', '最多只能',
                  '已达上限', '次数超限', 'limit']
RATE_KEYWORDS = ['请求过于频繁', '频繁', 'too many', 'rate limit', '429', '稍后再试',
                 '操作过于频繁', '请勿频繁']

DEFAULT_TIMEOUT = 10

# 开抢前提前多少秒把 HTTPS 连接捂热（三次握手 + TLS 大约要 60~140ms，
# 这段耗时绝不能算在「第一枪」上）。太早做连接会被服务端空闲断掉。
PREWARM_LEAD = 2.0

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

def default_config():
    return {
        'token': '',
        'target_date': (date_cls.today() + timedelta(days=2)).isoformat(),
        # 【需求3】时间窗口：窗口内所有整点 1 小时都是候选
        'window_start': '18:00',
        'window_end': '22:00',
        # 【需求2】场地优先级（前端拖动出来的顺序）
        'court_order': ['4号场', '5号场', '6号场', '7号场', '2号场', '3号场', '1号场'],
        'court_enabled': {c['name']: True for c in COURTS_FALLBACK},
        # 【需求6】反冲突
        'sort_strategy': SORT_COURT_RANDOM_TIME,
        'anti_collision': True,
        'passphrase': '',
        # 【需求4】速率
        # 这组是实测后的「最快且不撞限流」档：
        #   服务端 submit 的限流门槛就在 9 次/秒附近（连打 8 次被拒 6 次），
        #   所以 min_interval 取 0.15s ≈ 6.7 次/秒，留出安全余量；
        #   getDay 是只读接口，实测 14 并发能到 29 次/秒，侦察线程拉满。
        'min_interval': 0.15,
        'jitter_ms': 0,
        'burst': 2,
        'scout_workers': 14,
        # 抢购的头几秒最金贵：不在候选之间停下来重新侦察（那要多花 300ms）。
        # 候选试完了自然会侦察一次。
        'refresh_rounds': 0,
        # 并发首发：0=关（纯串行，最稳）。设成 2~3 则第一轮同时打前 N 个候选，
        # 覆盖面更广，代价是服务端排队让每个请求慢 ~100ms，且理论上多单窗口。
        'parallel_first': 0,
        'not_open_max_retries': 120,
        'unknown_max_retries': 3,
        # 定时
        'schedule': '08:00:00',
        # 【需求1】一天一单保护
        'one_per_day': True,
        'max_days_ahead': 2,
    }


# ---------------------------------------------------------------------------
# 【需求6】随机源 —— 不用时间做种子
# ---------------------------------------------------------------------------

class RandomSource:
    """非时间种子的随机源。

    为什么不能用 time.time()：分发给多人的脚本若在同一秒启动，
    random.seed(time.time()) 会产生**完全相同**的随机序列，
    于是所有人抢同一批时段 —— 正是要避免的「脚本打脚本」。

    这里的熵来自三处叠加：
      1. 持久化随机盐（首次运行用 os.urandom 生成，落到 .salt 文件，每个人/每台机不同）
      2. 每次进程启动再混入 os.urandom(24)（同一台机多次运行序列也不同）
      3. 可选的个人口令（用户自己填，进一步区分分发出去的副本）
    """

    def __init__(self, salt_path, passphrase='', enabled=True):
        self.enabled = bool(enabled)
        self.salt_path = salt_path
        self.salt = self._load_or_create_salt()
        self.passphrase = passphrase or ''
        seed_bytes = (
            bytes.fromhex(self.salt)
            + os.urandom(24)
            + hashlib.sha256(self.passphrase.encode('utf-8')).digest()
        )
        digest = hashlib.blake2b(seed_bytes, digest_size=32).digest()
        self._rng = random.Random(int.from_bytes(digest, 'big'))
        self.fingerprint = hashlib.blake2b(
            bytes.fromhex(self.salt) + self.passphrase.encode('utf-8'),
            digest_size=4).hexdigest().upper()

    def _load_or_create_salt(self):
        try:
            if os.path.exists(self.salt_path):
                with open(self.salt_path, encoding='utf-8') as f:
                    s = (f.read() or '').strip()
                if len(s) >= 32:
                    return s
        except Exception:
            pass
        s = secrets.token_hex(32)
        try:
            os.makedirs(os.path.dirname(self.salt_path) or '.', exist_ok=True)
            with open(self.salt_path, 'w', encoding='utf-8') as f:
                f.write(s)
        except Exception:
            pass
        return s

    def shuffle(self, seq):
        """打乱（原地）。关闭反冲突时保持原序。"""
        if not self.enabled:
            return list(seq)
        out = list(seq)
        self._rng.shuffle(out)
        return out

    def uniform(self, a, b):
        return self._rng.uniform(a, b) if self.enabled else (a + b) / 2

    def randint(self, a, b):
        return self._rng.randint(a, b) if self.enabled else (a + b) // 2

    def reset_salt(self):
        """换一个身份（想重新抽签时用）。"""
        s = secrets.token_hex(32)
        try:
            with open(self.salt_path, 'w', encoding='utf-8') as f:
                f.write(s)
        except Exception:
            pass
        self.salt = s


# ---------------------------------------------------------------------------
# 【需求4】速率治理
# ---------------------------------------------------------------------------

def measure_clock_offset(session, token, sku, d, samples=3,
                         timeout=DEFAULT_TIMEOUT):
    """量一下「本机时钟」和「学校服务器时钟」差多少秒。

    返回值 = 服务器时间 - 本机时间。正数说明服务器更快（我们得早点发），
    负数说明服务器慢（我们得晚点发）。

    做法：发一个请求，记下发出的本地时刻 t0 和收完的本地时刻 t1，
    用响应头里的 Date（服务器时间）和 (t0+t1)/2 比较。
    Date 只有秒精度，所以多测几次取中位数把网络抖动压下去。
    """
    from email.utils import parsedate_to_datetime
    offs = []
    for _ in range(max(1, samples)):
        try:
            t0 = datetime.utcnow()
            r = session.post(BASE_URL + PATH_GETDAY,
                             headers=build_headers(token),
                             json={'skuId': sku, 'serviceDate': d},
                             timeout=timeout)
            t1 = datetime.utcnow()
            raw = r.headers.get('Date')
            if not raw:
                continue
            srv = parsedate_to_datetime(raw)
            if srv.tzinfo is not None:
                srv = srv.astimezone(timezone.utc).replace(tzinfo=None)
            mid = t0 + (t1 - t0) / 2
            offs.append((srv - mid).total_seconds())
        except Exception:
            continue
    if not offs:
        return None
    offs.sort()
    # HTTP 的 Date 头只有「秒」精度而且是向下截断的，
    # 所以单次观测 = 真实偏差 + 一个 (-1, 0] 之间的负误差。
    # 取多次里的最大值最接近真值，再补上 1/(n+1) 秒把这个系统偏差抹平。
    # （实测本机比服务器慢约 1.1 秒，不校正的话等于白送别人一秒多。）
    return offs[-1] + 1.0 / (len(offs) + 1)


def build_session(pool=16):
    """建一个连接池够大的会话。

    默认 requests 只有 10 条连接，侦察并发一上去就会排队等着新建连接，
    白白多花一个 TLS 握手的时间（实测 ~100ms）。
    """
    from requests.adapters import HTTPAdapter
    s = requests.Session()
    s.verify = False
    try:
        ad = HTTPAdapter(pool_connections=max(8, pool),
                         pool_maxsize=max(8, pool), max_retries=0)
        s.mount('https://', ad)
        s.mount('http://', ad)
    except Exception:
        pass
    return s


class RateGovernor:
    """统一节流：保证相邻请求间隔 >= min_interval，并叠加随机抖动。

    抖动同样来自 RandomSource（非时间派生），避免多份脚本节奏完全对齐。
    """

    def __init__(self, min_interval=0.12, jitter_ms=120, rng=None):
        self.min_interval = max(0.0, float(min_interval or 0))
        self.jitter = max(0.0, float(jitter_ms or 0)) / 1000.0
        self._rng = rng
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.perf_counter()
            delta = self.min_interval - (now - self._last)
            if self._rng and self.jitter:
                delta += self._rng.uniform(0, self.jitter)
            if delta > 0:
                time.sleep(delta)
            self._last = time.perf_counter()

    @property
    def theoretical_qps(self):
        avg = self.min_interval + (self.jitter / 2)
        return (1.0 / avg) if avg > 0 else float('inf')

    def reset(self):
        """把节流时钟归零 —— 用在「定时到点」的那一刻。

        等待期间可能刚做过侦察，_last 是很新的时间戳；
        不归零的话，开抢第一枪会被上一次请求的节流余量拖住几十上百毫秒，
        而抢场赢的就是这点毫秒。
        """
        with self._lock:
            self._last = 0.0


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def to_min(hhmm):
    h, m = str(hhmm).split(':')
    return int(h) * 60 + int(m)


def fmt_min(m):
    return f'{m // 60:02d}:{m % 60:02d}'


def weekday_cn_of(d):
    return WEEKDAY_CN[date_cls.fromisoformat(d).weekday()]


def build_headers(token):
    return {
        'User-Agent': UA,
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'Origin': 'https://hdscw.hainanu.edu.cn',
        'Referer': 'https://hdscw.hainanu.edu.cn/',
        'Accept-Encoding': 'gzip, deflate',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Authorization': f'Bearer {token}',
    }


def brief(x, n=300):
    if x is None:
        return '(无响应)'
    if isinstance(x, str):
        return x[:n]
    try:
        return json.dumps(x, ensure_ascii=False)[:n]
    except Exception:
        return str(x)[:n]


def judge(result, status=None):
    """把服务端响应翻译成语义化结论。"""
    if status == 401:
        return 'auth_error'
    if status == 403:
        return 'no_permission'
    if status == 429:
        return 'rate_limited'
    if not isinstance(result, dict):
        text = str(result)
        low = text.lower()
        for ks, v in ((RATE_KEYWORDS, 'rate_limited'), (PERM_KEYWORDS, 'no_permission'),
                      (NOT_OPEN_KEYWORDS, 'not_open'), (LIMIT_KEYWORDS, 'limit_reached')):
            if any(k.lower() in low for k in ks):
                return v
        return 'unknown'

    code = result.get('code')
    msg = str(result.get('msg') or result.get('message') or '')
    if code in (401, '401') or '认证失败' in msg:
        return 'auth_error'
    if code in (200, '200', 0, '0') and (msg in ('操作成功', '下单成功', 'ok', 'success', '')
                                         or '成功' in msg):
        return 'success'
    text = json.dumps(result, ensure_ascii=False)
    low = text.lower()
    for ks, v in ((RATE_KEYWORDS, 'rate_limited'), (PERM_KEYWORDS, 'no_permission'),
                  (LIMIT_KEYWORDS, 'limit_reached'), (NOT_OPEN_KEYWORDS, 'not_open'),
                  (PAY_KEYWORDS, 'pay_error'), (CONFLICT_KEYWORDS, 'conflict')):
        if any(k.lower() in low for k in ks):
            return v
    return 'unknown'


# ---------------------------------------------------------------------------
# 接口层
# ---------------------------------------------------------------------------

def api_places(session, token, timeout=DEFAULT_TIMEOUT):
    payload = {
        'size': 999,
        'query': {'spuId': PRODUCT_ID, 'price': 0, 'saleStatus': '0', 'delFlag': 0},
        'queryConfigList': [
            {'fieldName': 'spu_id', 'propertyName': 'spuId', 'tableAlias': 'a', 'queryType': 'eq'},
            {'fieldName': 'price', 'propertyName': 'price', 'tableAlias': 'a', 'queryType': 'ne'},
            {'fieldName': 'sale_status', 'propertyName': 'saleStatus', 'tableAlias': 'a', 'queryType': 'eq'},
            {'fieldName': 'del_flag', 'propertyName': 'delFlag', 'tableAlias': 'a', 'queryType': 'eq'},
        ],
        'orderConfigList': [{'fieldName': 'create_time', 'tableAlias': 'a', 'orderType': 'asc'}],
    }
    r = session.post(BASE_URL + PATH_PLACES, headers=build_headers(token),
                     json=payload, timeout=timeout)
    rows = (r.json() or {}).get('rows') or []
    courts = []
    for row in rows:
        try:
            weekly = {x['date']: [list(t) for t in x['times']]
                      for x in json.loads(row['dateSettings'])}
        except Exception:
            weekly = FALLBACK_WEEKLY.get(row.get('name'), {})
        courts.append({'name': row.get('name'), 'sku': row.get('id'),
                       'price': row.get('price'), 'weekly': weekly})
    return courts


def api_getday(session, token, sku, d, timeout=DEFAULT_TIMEOUT):
    """查某片场地某天已被占用的时段。只读，不消耗额度。"""
    r = session.post(BASE_URL + PATH_GETDAY, headers=build_headers(token),
                     json={'skuId': sku, 'serviceDate': d}, timeout=timeout)
    try:
        data = r.json()
    except Exception:
        return None, r.status_code, r.text[:200]
    occ = set()
    for it in (data or {}).get('data') or []:
        if it.get('serviceDate') == d and it.get('serviceTime'):
            occ.add(it['serviceTime'])
    return occ, r.status_code, data


def build_anon_headers():
    """登录用：不带 Authorization。"""
    h = build_headers('')
    h.pop('Authorization', None)
    return h


def extract_token(data):
    """从登录响应里挖 token（不同后端放的位置不一样，逐个试）。"""
    if not isinstance(data, dict):
        return None
    d = data.get('data')

    def g(o, k):
        return o.get(k) if isinstance(o, dict) else None

    for v in (g(d, 'token'), d if isinstance(d, str) else None,
              g(data, 'token'), g(d, 'access_token'), g(data, 'access_token'),
              g(d, 'Authorization'), g(d, 'loginUserKey')):
        if isinstance(v, str) and len(v) >= 30:
            return v.strip()
    # 兜底：整个响应里找长得像 JWT 的串
    m = re.search(r'eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}',
                  json.dumps(data, ensure_ascii=False))
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# CAS 统一身份认证（authserver.hainanu.edu.cn）
# ---------------------------------------------------------------------------
# 协议（2026-09-16 从登录页 + encrypt.js + login.js 逆向）
#   1. GET  /authserver/login?service=<service>  拿 JSESSIONID + execution + pwdEncryptSalt
#   2. 密码加密：AES-CBC/PKCS7，key = pwdEncryptSalt，iv = 随机16字符，
#      明文 = 随机64字符 + 密码（encrypt.js: encryptAES）
#   3. POST /authserver/login (form-urlencoded)：
#      username / password(密文) / lt(空) / execution / cllt=userNameLogin /
#      dllt=generalLogin / _eventId=submit / rememberMe
#   4. 成功 → 302，Location 里带 szblTK=<token>（H5 前端直接拿它当 token）
#      若账号开了多因子认证 → 302 到 reAuthCheck/reAuthLoginView.do?isMultifactor=true
# ---------------------------------------------------------------------------

AUTH_BASE = 'https://authserver.hainanu.edu.cn'
CAS_LOGIN_PATH = '/authserver/login'

# 【V3.1 修正】service 必须带「回跳目标」后缀，否则 MFA 通过后后端无处可跳，
# 浏览器会停在 https://hdscs.hainanu.edu.cn/hdsc/app/ 的 404 页上，永远拿不到 token。
# 依据（2026-09-16 从 H5 登录页 index-DVL2DxLK.js 逆向）：
#   service = https://hdscs.hainanu.edu.cn/hdsc/app/<base64url(回跳路径)>
#   H5 默认回跳 /user → base64url → "L3VzZXI"
#   且实测 /hdsc/app/* 未登录 302 回 CAS（service 带子路径）、
#   /hdsc/app/L3VzZXI?ticket=假票 → HTTP 500（后端确实会验票换 szblTK）
def _b64url_path(path: str) -> str:
    return base64.urlsafe_b64encode(path.encode('utf-8')).decode('ascii').rstrip('=')

CAS_REDIRECT_DEFAULT = '/user'
CAS_APP_BASE = 'https://hdscs.hainanu.edu.cn/hdsc/app/'
CAS_SERVICE = CAS_APP_BASE + _b64url_path(CAS_REDIRECT_DEFAULT)   # …/hdsc/app/L3VzZXI

# 平台侧错误码（H5 源码：["1401","1402"] 被排除，不当 token 用）
SZBL_ERR_CODES = ('1401', '1402')
AES_CHARS = 'ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678'

MFA_REQUIRED = 'MFA_REQUIRED'

# MFA 标记：命中任意一个即认为账号需要多因子认证。
# 海大 CAS 的 MFA 页面不一定走 302 跳转——也可能把「验证方式选择页」
# 直接内嵌在 200 响应里，甚至「先发 token、再挂 MFA 待验证」。
# 所以不能只看跳转 URL，响应体也得扫一遍。
_MFA_URL_MARKERS = ('reauth', 'multifactor', 'ismultifactor', 'mfa', 'otp',
                    'factor', 'authn', 'step-up', 'duo', 'twofactor', '2fa')
_MFA_TXT_MARKERS = ('reauth', 'multifactor', 'ismultifactor', 'mfa', 'otp',
                    'factor', 'authn', 'step-up', 'duo', 'twofactor', '2fa',
                    '多因子', '多因素', '企业微信', '短信验证', '短信验证码',
                    '扫码', '请选择验证方式', '验证方式', '动态口令',
                    '二次验证', '二次认证', '身份验证')


def mfa_in_text(t):
    """响应体里是否出现多因子认证标记。"""
    if not t:
        return False
    low = (t or '').lower()
    return any(m in low for m in _MFA_TXT_MARKERS)


def mfa_in_url(u):
    """跳转 URL 里是否出现多因子认证标记。"""
    if not u:
        return False
    low = (u or '').lower()
    return any(m in low for m in _MFA_URL_MARKERS)


def _cas_rand(n):
    return ''.join(random.choice(AES_CHARS) for _ in range(n))


def _cas_encrypt_password(password: str, salt: str) -> str:
    """复现 encrypt.js 的 encryptPassword()。"""
    try:
        from cryptography.hazmat.primitives import padding as _pad
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except Exception:
        return password
    data = (_cas_rand(64) + password).encode('utf-8')
    p = _pad.PKCS7(128).padder()
    buf = p.update(data) + p.finalize()
    enc = Cipher(algorithms.AES(salt.encode('utf-8')),
                 modes.CBC(_cas_rand(16).encode('utf-8'))).encryptor()
    return base64.b64encode(enc.update(buf) + enc.finalize()).decode()


def api_cas_login(username, password, timeout=15):
    """学号 + 门户密码 → token。

    返回 (token, err, extra)
      token  成功时是 token 字符串
      err    None 成功；MFA_REQUIRED 表示需要多因子认证；其它为错误信息
      extra  附加信息（如 MFA 页面地址）
    """
    if not username or not password:
        return None, '请填学号和密码', None
    session = requests.Session()
    session.verify = False
    session.headers.update({'User-Agent': UA_BROWSER,
                            'Accept': 'text/html,application/xhtml+xml,*/*',
                            'Accept-Language': 'zh-CN,zh;q=0.9'})
    url = AUTH_BASE + CAS_LOGIN_PATH + '?service=' + CAS_SERVICE
    try:
        r = session.get(url, timeout=timeout)
        html = r.text
    except Exception as e:
        return None, f'打不开统一身份认证页：{e}', None

    m_salt = (re.search(r'id="pwdEncryptSalt"\s+value="([^"]*)"', html)
              or re.search(r"id='pwdEncryptSalt'\s+value='([^']*)'", html)
              or re.search(r'pwdEncryptSalt"\s+value="([^"]*)"', html))
    m_exec = (re.search(r'id="execution"\s+name="execution"\s+value="([^"]*)"', html)
              or re.search(r'name="execution"\s+value="([^"]*)"', html))
    if not m_salt:
        return None, '登录页拿不到加密盐（页面结构可能变了）', None
    salt = m_salt.group(1)
    execution = m_exec.group(1) if m_exec else 'e1s1'

    payload = {
        'username': str(username),
        'password': _cas_encrypt_password(str(password), salt),
        'lt': '',
        'execution': execution,
        'cllt': 'userNameLogin',
        'dllt': 'generalLogin',
        '_eventId': 'submit',
        'rememberMe': 'true',
    }
    try:
        resp = session.post(AUTH_BASE + CAS_LOGIN_PATH, data=payload, timeout=timeout,
                            allow_redirects=False,
                            headers={'Content-Type': 'application/x-www-form-urlencoded',
                                     'Referer': url})
    except Exception as e:
        return None, f'提交登录失败：{e}', None

    loc = resp.headers.get('Location', '') or ''

    # 【V3.1】CAS 登录成功后只发第一跳（service?ticket=ST-xxx），
    # szblTK 是 hdscs 后端验完票后在**后续重定向**里才出现的，
    # 所以必须手动跟完整条跳转链，逐跳扫 URL 里的 szblTK。
    def _scan(u):
        m = re.search(r'szblTK=([A-Za-z0-9._\-]+)', u or '')
        return m.group(1) if m else None

    def _mfa_required(where):
        # where: 触发 MFA 的那一跳 URL
        if not where.startswith('http'):
            where = AUTH_BASE + '/' + where.lstrip('/')
        return None, MFA_REQUIRED, {'url': where, 'session': session,
                                    'cookies': session.cookies.get_dict(),
                                    'hint': '账号开了多因子认证（MFA）。'}

    cur = AUTH_BASE + CAS_LOGIN_PATH          # 当前 URL（用于解析相对 Location）
    hop = loc
    first_body = resp.text                   # 密码提交这次的响应体（200 内嵌 MFA 页看这里）
    last_body = ''
    for _ in range(8):                        # 最多 8 跳，防环
        if not hop:
            break
        tok = _scan(hop)
        if tok:
            if tok in SZBL_ERR_CODES:
                return None, (f'平台返回错误码 {tok}（1401/1402）：学号可能尚未'
                              '绑定订场平台账号。请先在微信里正常进一次小程序'
                              '再回来重试。'), None
            # 拿到 token 但同时这跳/前后还带着 MFA 标记 →
            # 说明这是「CAS 已发 token、但 MFA 仍挂起」的账号，
            # 仍走浏览器让本人完成验证，拿回真正可用的 token。
            if mfa_in_text(first_body) or mfa_in_text(last_body):
                return _mfa_required(hop)
            return tok, None, None
        if mfa_in_url(hop):
            # 把 CAS 会话一并交出去：浏览器要靠这些 cookie 直接进入 MFA 那一步，
            # 否则用户得重新输一遍学号密码。
            return _mfa_required(hop)
        if not hop.startswith('http'):
            from urllib.parse import urljoin as _uj
            hop = _uj(cur, hop)
        cur = hop
        try:
            r2 = session.get(hop, timeout=timeout, allow_redirects=False)
        except Exception as e:
            return None, f'跟随登录跳转失败：{e}', None
        last_body = r2.text or ''
        # 某一跳是 MFA 选择页（200，不跳转）：响应体里会有标记
        if mfa_in_text(last_body):
            return _mfa_required(hop)
        hop = r2.headers.get('Location', '') or ''
    # 链走完：再看看密码提交那一次是不是直接返回了 MFA 页（HTTP 200 内嵌）
    if mfa_in_text(first_body):
        return _mfa_required(url)
    # 链走完仍没拿到 token
    return None, ('登录链走完仍没拿到 token（最后一跳：'
                  + (cur or '?')[:90] + '），请把这段发给开发者'), None

    # 没跳走 → 登录失败，页面里有原因
    try:
        txt = re.sub(r'<script.*?</script>', ' ', resp.text, flags=re.S | re.I)
        txt = re.sub(r'<style.*?</style>', ' ', txt, flags=re.S | re.I)
        txt = re.sub(r'<[^>]+>', ' ', txt)
        txt = re.sub(r'\s+', ' ', txt).strip()
    except Exception:
        txt = ''
    for kw in ('验证码', '密码错误', '用户名或密码错误', '不存在', '锁定', '激活', '过期'):
        if kw in txt:
            i = txt.find(kw)
            return None, txt[max(0, i - 30): i + 50].strip(), None
    return None, (txt[:120] or f'登录失败（HTTP {resp.status_code}）'), None


def api_login(username, password, login_type='01', timeout=12):
    """学号 + 密码 → token。不需要微信、不需要抓包、不需要证书。

    接口来自 H5 前端：`POST /login  {username, password, loginType:"01"}`
    （与小程序同一后端 hdscs.hainanu.edu.cn，token 通用）
    """
    if not username or not password:
        return None, '请填用户名和密码'
    session = requests.Session()
    session.verify = False
    try:
        r = session.post(BASE_URL + PATH_LOGIN, headers=build_anon_headers(),
                         json={'username': str(username), 'password': str(password),
                               'loginType': str(login_type or '01')},
                         timeout=timeout)
        data = r.json()
    except Exception as e:
        return None, f'请求失败：{e}'
    code = data.get('code') if isinstance(data, dict) else None
    msg = (data.get('msg') or '') if isinstance(data, dict) else ''
    if code not in (200, '200', 0, '0'):
        return None, msg or f'登录失败（code={code}）'
    tok = extract_token(data)
    if not tok:
        return None, '登录成功，但响应里没找到 token 字段：' + brief(data, 160)
    return tok, None


def api_getinfo(token, timeout=10):
    """拉用户信息，可用来确认 token 归属。"""
    session = requests.Session()
    session.verify = False
    try:
        r = session.get(BASE_URL + PATH_GETINFO, headers=build_headers(token),
                        timeout=timeout)
        return r.json(), None
    except Exception as e:
        return None, str(e)


def api_profile(token, timeout=10):
    """登录后拉一次个人信息，用来自动填「用户ID」。

    /getInfo 的返回形状在不同版本里略有差别（data / data.user / user 都见过），
    这里做多层兜底；取不到就返回空串，不影响登录本身。
    返回 (user_id, username)
    """
    data, err = api_getinfo(token, timeout=timeout)
    if err or not isinstance(data, dict):
        return '', ''
    roots = [data.get('data'), data.get('user'), data.get('userInfo'), data]
    uid_keys = ('userId', 'id', 'appUserId', 'app_user_id', 'uid', 'user_id')
    name_keys = ('userName', 'username', 'loginName', 'nickName', 'nickname',
                 'realName', 'name', 'studentNo', 'userNo')
    uid, uname = '', ''
    for root in roots:
        if not isinstance(root, dict):
            continue
        if not uid:
            for k in uid_keys:
                v = root.get(k)
                if v not in (None, '', 0):
                    uid = str(v)
                    break
        if not uname:
            for k in name_keys:
                v = root.get(k)
                if isinstance(v, str) and v.strip():
                    uname = v.strip()
                    break
        if uid and uname:
            break
    return uid, uname


def verify_token(token, d=None, timeout=10):
    """验证 token 是否有效：调 getDay（受保护接口，能读即有效）。"""
    session = requests.Session()
    session.verify = False
    d = d or (date_cls.today() + timedelta(days=2)).isoformat()
    try:
        occ, status, data = api_getday(session, token, COURTS_FALLBACK[0]['sku'],
                                       d, timeout=timeout)
    except Exception as e:
        return False, f'验证请求失败：{e}'
    if status == 401 or (isinstance(data, dict) and data.get('code') == 401):
        return False, 'token 无效（401 认证失败）'
    if status == 200:
        return True, '有效'
    return False, f'未预期响应：HTTP {status} {brief(data, 100)}'


def api_submit(session, token, sku, d, t, timeout=DEFAULT_TIMEOUT):
    payload = {
        'orderSource': '2', 'productType': '1011', 'orderType': '2',
        'productId': PRODUCT_ID, 'skuId': sku, 'payType': '11', 'isUseCard': '0',
        'serviceDate': d, 'serviceTime': t, 'bookPayType': '0',
        'quantity': 1, 'addedServicesIds': [],
    }
    try:
        r = session.post(BASE_URL + PATH_SUBMIT, headers=build_headers(token),
                         json=payload, timeout=timeout)
    except requests.RequestException as e:
        return 'network_error', None, str(e)
    try:
        data = r.json()
    except Exception:
        return 'unknown', r.status_code, r.text[:300]
    return judge(data, status=r.status_code), r.status_code, data


def api_my_orders(session, token, d=None, app_user_id='', timeout=DEFAULT_TIMEOUT):
    """我的订单列表（报文与抓包第 19 号一致）。

    用于「一天一单」保护：目标日期当天若已有单 → 拒绝再下，绝不重复扣费。
    app_user_id 可为空（服务端会按 token 归属过滤），填了更精确。
    """
    query = {'delFlag': 0}
    qcl = [{'fieldName': 'del_flag', 'propertyName': 'delFlag',
            'tableAlias': 'a', 'queryType': 'eq'}]
    if app_user_id:
        query['appUserId'] = str(app_user_id)
        qcl.insert(0, {'fieldName': 'app_user_id', 'propertyName': 'appUserId',
                       'tableAlias': 'a', 'queryType': 'eq'})
    payload = {
        'current': 1, 'size': 50, 'query': query, 'queryConfigList': qcl,
        'orderConfigList': [{'fieldName': 'create_time', 'tableAlias': 'a',
                             'orderType': 'desc'}],
    }
    try:
        r = session.post(BASE_URL + PATH_MY_ORDERS, headers=build_headers(token),
                         json=payload, timeout=timeout)
        data = r.json()
    except Exception as e:
        return None, str(e)
    if isinstance(data, dict) and data.get('code') not in (200, '200', 0, '0', None):
        return None, brief(data, 150)
    rows = (data or {}).get('rows')
    if rows is None:
        rows = ((data or {}).get('data') or {}).get('list') or []
    out = []
    for it in rows:
        out.append({
            'id': it.get('id'), 'orderNo': it.get('orderNo'),
            'date': it.get('serviceDate'), 'time': it.get('serviceTime'),
            'court': it.get('productName'), 'status': it.get('orderStatus'),
        })
    if d:
        out = [o for o in out if o.get('date') == d]
    return out, None


# ---------------------------------------------------------------------------
# 时段 / 候选规划
# ---------------------------------------------------------------------------

def enumerate_slots(win_start, win_end, step=SLOT_MINUTES):
    """时间窗口内所有整点 1 小时时段 → ['18:00-19:00', ...]"""
    s, e = to_min(win_start), to_min(win_end)
    step = SLOT_MINUTES  # 强制 1 小时
    out = []
    t = s
    while t + step <= e:
        out.append(f'{fmt_min(t)}-{fmt_min(t + step)}')
        t += step
    return out


def court_open_at(court, weekday, slot):
    """该场地在该星期几是否开放这个 1 小时时段。"""
    s, e = to_min(slot.split('-')[0]), to_min(slot.split('-')[1])
    for o_s, o_e in (court.get('weekly') or {}).get(weekday, []):
        if to_min(o_s) <= s and e <= to_min(o_e):
            return True
    return False


def build_matrix(courts, weekday, slots, occupied_map):
    """构造 场地×时段 三态矩阵：open(开放且空闲) / occupied(开放但被订) / closed(不开放)。"""
    matrix = {}
    for c in courts:
        row = {}
        occ = occupied_map.get(c['sku'], set()) or set()
        for slot in slots:
            if not court_open_at(c, weekday, slot):
                row[slot] = 'closed'
            elif slot in occ:
                row[slot] = 'occupied'
            else:
                row[slot] = 'open'
        matrix[c['name']] = row
    return matrix


def make_candidates(courts, weekday, slots, occupied_map, court_order,
                    enabled_map, rng, strategy):
    """生成候选并按策略排序。

    courts        场地列表（含 sku / weekly）
    court_order   前端拖动出来的场地优先级（字符串列表）
    enabled_map   每片场地是否参与
    rng           RandomSource
    strategy      排序策略
    """
    order_index = {name: i for i, name in enumerate(court_order)}
    slot_index = {s: i for i, s in enumerate(slots)}

    pool = []
    for c in courts:
        if not enabled_map.get(c['name'], True):
            continue
        occ = occupied_map.get(c['sku'], set()) or set()
        for slot in slots:
            if not court_open_at(c, weekday, slot):
                continue
            if slot in occ:
                continue
            pool.append({
                'court': c['name'], 'sku': c['sku'], 'slot': slot,
                'court_rank': order_index.get(c['name'], 999),
                'slot_rank': slot_index.get(slot, 999),
            })

    if strategy == SORT_COURT:
        pool.sort(key=lambda x: (x['court_rank'], x['slot_rank']))
    elif strategy == SORT_TIME:
        pool.sort(key=lambda x: (x['slot_rank'], x['court_rank']))
    elif strategy == SORT_SHUFFLE:
        pool = rng.shuffle(pool)
    else:  # SORT_COURT_RANDOM_TIME（默认）
        # 按拖动顺序分组，组内时段随机 → 场地优先级仍然被尊重，
        # 但不同脚本在同一个场地里会落在不同的小时上，减少正面撞车。
        by_court = {}
        for it in pool:
            by_court.setdefault(it['court_rank'], []).append(it)
        pool = []
        for rank in sorted(by_court):
            pool.extend(rng.shuffle(by_court[rank]))
    return pool


# ---------------------------------------------------------------------------
# 侦察：并发查空场（只读，不消耗额度）
# ---------------------------------------------------------------------------

def scout(session, token, courts, d, workers=7, log=print):
    """并发拉取所有场地的占用情况。"""
    occupied = {}
    auth_fail = 0

    def one(c):
        try:
            occ, status, data = api_getday(session, token, c['sku'], d)
            if status == 401 or (isinstance(data, dict) and data.get('code') == 401):
                return c['sku'], None, 'auth'
            return c['sku'], (occ or set()), None
        except Exception as e:
            return c['sku'], set(), str(e)[:80]

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(courts) or 1))) as ex:
        for sku, occ, err in ex.map(one, courts):
            if err == 'auth':
                auth_fail += 1
                occupied[sku] = set()
            elif err:
                log(f'  [侦察] {sku} 失败：{err}')
                occupied[sku] = set()
            else:
                occupied[sku] = occ
    return occupied, auth_fail


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_booking(params, log=print, stop=None):
    """抢单主流程。params 见 app_server / 前端字段。"""
    stop = stop or (lambda: False)
    token = (params.get('token') or '').strip()
    d = params.get('target_date') or ''
    win_start = params.get('window_start') or '18:00'
    win_end = params.get('window_end') or '22:00'
    court_order = list(params.get('court_order') or [c['name'] for c in COURTS_FALLBACK])
    enabled_map = params.get('court_enabled') or {c['name']: True for c in COURTS_FALLBACK}
    strategy = params.get('sort_strategy') or SORT_COURT_RANDOM_TIME
    do_submit = bool(params.get('submit', True))
    schedule = params.get('schedule')            # (h, m, s) 或 None
    one_per_day = bool(params.get('one_per_day', True))
    base_dir = params.get('base_dir') or '.'

    rng = RandomSource(os.path.join(base_dir, '.anti_collision_salt'),
                       passphrase=params.get('passphrase') or '',
                       enabled=bool(params.get('anti_collision', True)))
    gov = RateGovernor(params.get('min_interval', 0.12),
                       params.get('jitter_ms', 120), rng=rng)

    not_open_max = int(params.get('not_open_max_retries') or 120)
    unknown_max = int(params.get('unknown_max_retries') or 3)
    burst = max(1, int(params.get('burst') or 3))
    # 并发首发：抢单第一轮把前 N 个候选同时打出去（0 = 关，老老实实串行）。
    # 实测：并发 7 个不同场地不会触发限流，但服务端要排队，每个请求的往返
    # 会从 ~140ms 涨到 ~240ms。所以它的价值不在"更快"，而在"更广"——
    # 一次就覆盖多个场地，不用等前一个失败再试下一个。
    parallel_first = max(0, int(params.get('parallel_first') or 0))
    refresh_rounds = max(0, int(params.get('refresh_rounds') or 0))
    workers = max(1, int(params.get('scout_workers') or 7))

    slots = enumerate_slots(win_start, win_end)
    weekday = weekday_cn_of(d) if d else ''

    log('=' * 66)
    log(' 海大网球订场 V4.0 · 急速抢单（一天一单，固定 1 小时）')
    log(f' 目标日期：{d}（{weekday}）')
    log(f' 时间窗口：{win_start} - {win_end} ｜ 候选时段 {len(slots)} 个：'
        f'{", ".join(slots) if slots else "无"}')
    log(f' 场地顺序：{" > ".join(n for n in court_order if enabled_map.get(n, True))}')
    log(f' 排序策略：{SORT_LABELS.get(strategy, strategy)}')
    log(f' 反冲突随机：{"开" if rng.enabled else "关"}（签 {rng.fingerprint}）')
    log(f' 速率：最小间隔 {gov.min_interval}s + 抖动 ≤{int((params.get("jitter_ms") or 0))}ms '
        f'→ 理论上限约 {gov.theoretical_qps:.1f} 次/秒')
    log('=' * 66)

    result = {'success': False, 'slots': slots, 'weekday': weekday,
              'attempts': [], 'matrix': {}, 'candidates': [], 'plan': []}

    if not token:
        log('× 未填 token。')
        result['error'] = '未填 token'
        return result
    if not d:
        log('× 未选日期。')
        result['error'] = '未选日期'
        return result
    try:
        ahead = (date_cls.fromisoformat(d) - date_cls.today()).days
        max_ahead = int(params.get('max_days_ahead') or 2)
        if ahead > max_ahead:
            log(f'⚠ 目标日期距今 {ahead} 天，超出可预约窗口（约 {max_ahead} 天）。')
        elif ahead < 0:
            log('⚠ 目标日期已过去。')
    except Exception:
        pass
    if not slots:
        log('× 时间窗口内没有合法的整点时段（窗口至少要跨 1 小时）。')
        result['error'] = '时间窗口无效'
        return result

    session = build_session(max(8, workers + 4))

    # ---------- 1. 场地 ----------
    try:
        courts = api_places(session, token)
        if courts:
            log(f'[场地] place/list 实时拿到 {len(courts)} 片')
        else:
            raise RuntimeError('rows 为空')
    except Exception as e:
        log(f'[场地] place/list 失败（{e}），用内置兜底表')
        courts = [{'name': c['name'], 'sku': c['sku'], 'price': 20,
                   'weekly': FALLBACK_WEEKLY.get(c['name'], {})} for c in COURTS_FALLBACK]
    known = {c['sku'] for c in courts}
    for c in COURTS_FALLBACK:
        if c['sku'] not in known:
            courts.append({'name': c['name'], 'sku': c['sku'], 'price': 20,
                           'weekly': FALLBACK_WEEKLY.get(c['name'], {})})

    # ---------- 2. 【需求1】一天一单保护 ----------
    if one_per_day:
        orders, err = api_my_orders(session, token, d,
                                    app_user_id=params.get('app_user_id') or '')
        if err:
            log(f'[额度] 查我的订单失败（{err}），跳过当日额度校验')
        else:
            # 保守起见：当天存在任何订单（未明确已取消的）都视为已用额度
            live = [o for o in (orders or [])
                    if str(o.get('status') or '').upper() not in ('05', '99', 'CANCEL')]
            if live:
                log(f'⚠ 检测到 {d} 当天已有 {len(live)} 笔订单：')
                for o in live:
                    log(f'    - {o.get("court")} {o.get("time")}（{o.get("orderNo")}）')
                log('  按「每人每天 1 小时」规则，已停止，不会重复下单扣费。')
                result['error'] = '当天已有订单（一天一单保护）'
                result['existing_orders'] = live
                return result
            log(f'[额度] {d} 当天无订单，可以继续（每人每天 1 小时）')

    # ---------- 3. 侦察：并发查空场（只读，不消耗额度） ----------
    log(f'[侦察] 并发查询 {len(courts)} 片场地的占用（getDay，只读不消耗额度）...')
    gov.wait()
    occupied, auth_fail = scout(session, token, courts, d, workers=workers, log=log)
    if courts and auth_fail == len(courts):
        log('× 所有场地 getDay 均「认证失败」→ token 已失效，请重新抓 token。')
        log('  提醒：place/list 不需要 token，能看到场地 ≠ token 有效。')
        result['error'] = 'token 已失效'
        result['auth_failed'] = True
        return result

    matrix = build_matrix(courts, weekday, slots, occupied)
    result['matrix'] = matrix
    result['occupied'] = {c['name']: sorted(occupied.get(c['sku'], set())) for c in courts}
    log('[空场图] （○ 可抢 · × 已占 · － 不开放）')
    header = '           ' + ' '.join(s.split('-')[0][:2] + '点' for s in slots)
    log(header)
    for c in courts:
        if not enabled_map.get(c['name'], True):
            continue
        mark = {'open': '○', 'occupied': '×', 'closed': '－'}
        log(f"   {c['name']:<6} " + ' '.join(f'  {mark[matrix[c["name"]][s]]} ' for s in slots))

    # ---------- 4. 候选 ----------
    def refresh_plan():
        return make_candidates(courts, weekday, slots, occupied, court_order,
                               enabled_map, rng, strategy)

    plan = refresh_plan()
    result['plan'] = [{'court': p['court'], 'slot': p['slot']} for p in plan]
    result['candidates'] = result['plan']
    if plan:
        log(f'\n[规划] 共 {len(plan)} 个候选（按顺序逐个尝试，抢到 1 单立即停止）：')
        for i, p in enumerate(plan[:20], 1):
            log(f"   {i:>2}. {p['court']}  {p['slot']}")
        if len(plan) > 20:
            log(f'   ... 另有 {len(plan) - 20} 个')
    if not plan:
        # 关键是：设了定时的情况下绝不能在这里退出。
        # 典型的场景是「8 点放号」，7:59 侦察必然一片「不开放」，
        # 若在此时 return，就永远等不到放号的那一刻 —— 抢都抢不了。
        if not schedule:
            log('× 窗口内没有可抢的时段（要么不开放、要么已被订满）。')
            result['error'] = '无可用候选'
            return result
        log('× 此刻窗口内还没有可抢的时段（多半是还没到放号时间）。')
        log('  已设了定时，到点后会立刻重新侦察一次再抢 —— 先等着。')

    if not do_submit:
        log('\n[预览模式] 仅侦察与规划，未提交任何订单。')
        return result

    # ---------- 5. 定时 ----------
    if schedule:
        # 允许传 (h,m,s) 元组，也允许传 '07:59:58' 字符串
        if isinstance(schedule, str):
            try:
                parts = [int(x) for x in schedule.split(':')]
                while len(parts) < 3:
                    parts.append(0)
                schedule = tuple(parts[:3])
            except Exception:
                schedule = None
        if schedule and len(schedule) == 3:
            h, m, s = schedule
            target = datetime.now().replace(hour=h, minute=m, second=s,
                                            microsecond=0)
            # 今天的这个点已经过了（比如下午两点才点的「8:00 开抢」），
            # 那就等明天早上那一场，别一启动就立刻开打。
            if target <= datetime.now():
                target += timedelta(days=1)

            # 先量一下本机和服务器差多少时间，把开抢时刻校正过去。
            # 这一步很关键：系统时钟快/慢 1 秒，就早/晚 1 秒，
            # 而抢场赢的就是这几十毫秒。
            offset = measure_clock_offset(session, token, courts[0]['sku'], d,
                                          samples=5)
            if offset is None:
                log('  · 没能读到服务器时间，按本机时钟开抢（建议校准系统时间）')
                offset = 0.0
            elif abs(offset) > 10:
                # 差出 10 秒以上基本是某次网络卡顿测歪了，宁可不校正
                log(f'  · 测出的时钟差（{offset:.1f}s）不正常，忽略，按本机时钟开抢')
                offset = 0.0
            else:
                log(f'  · 服务器时钟比本机 {"快" if offset >= 0 else "慢"} '
                    f'{abs(offset)*1000:.0f} ms，开抢时刻已自动校正')
                target = target - timedelta(seconds=offset)

            log(f'\n等待到 {target.strftime("%H:%M:%S")} 开抢'
                f'（本机时刻，剩余 '
                f'{(target - datetime.now()).total_seconds():.1f}s）...')
            warmed = False
            while not stop():
                left = (target - datetime.now()).total_seconds()
                if left <= 0:
                    break
                # ① 临门一脚前把 TLS 连接捂热 —— 新建连接要一次完整握手，
                #    实测 60~140ms，开抢第一枪绝不能浪费在这上面。
                if (not warmed) and left <= PREWARM_LEAD:
                    warmed = True
                    try:
                        _t = time.perf_counter()
                        api_getday(session, token, courts[0]['sku'], d,
                                   timeout=DEFAULT_TIMEOUT)
                        log(f'  · 预热连接完成（{int((time.perf_counter()-_t)*1000)}ms）'
                            f'，连接保持热状态直接开抢')
                    except Exception:
                        pass
                    # 临门一脚再校一次时钟：等了几十分钟，两边可能又漂了
                    off2 = measure_clock_offset(session, token,
                                                courts[0]['sku'], d, samples=3)
                    if off2 is not None and abs(off2 - offset) > 0.3:
                        log(f'  · 时钟偏差修正：{offset*1000:.0f}ms → '
                            f'{off2*1000:.0f}ms，开抢时刻同步调整')
                        target = target + timedelta(seconds=(offset - off2))
                        offset = off2
                    continue
                # ② 越接近越睡得短：最后 1 秒 5ms 一跳，最后 80ms 纯忙等，
                #    把「到点」的判断误差从原来的 50ms 压到 1ms 以内。
                if left > 1.0:
                    time.sleep(0.05)
                elif left > 0.08:
                    time.sleep(0.005)
                else:
                    time.sleep(0)
            if stop():
                log('  已取消。')
                return result
            # ③ 节流时钟归零，别让等待期间的请求拖慢第一枪
            gov.reset()
            lag_ms = (datetime.now() - target).total_seconds() * 1000
            log(f'  到点（误差 {lag_ms:+.0f} ms），开始提交。')
            # ④ 放号就发生在这一刻：如果等之前压根没有候选（一片「不开放」），
            #    必须先抓一份最新空场图；本来就有候选的话直接开打，别浪费这半秒。
            if not plan:
                occupied, af = scout(session, token, courts, d,
                                     workers=workers, log=log)
                if af == len(courts):
                    log('  × token 失效，停止。')
                    result['error'] = 'token 已失效'
                    result['auth_failed'] = True
                    return result
                gov.reset()

    # ---------- 6. 抢单 ----------
    if parallel_first > 1:
        log(f'\n[抢单] 开始（第一轮并发首发 {parallel_first} 个候选，之后转串行）')
        log(f'   ⚠ 并发首发理论上存在「同时成交多单」的窗口，'
            f'抢完请到「我的订单」核对一下。')
    else:
        log('\n[抢单] 开始（串行提交：同一账号并发下单会导致重复扣费，故不并行提交）')
    tried = set()
    not_open_streak = 0
    refresh_counter = 0
    booked = None
    parallel_done = False
    # 防无限重试：如果候选一直「已被占用」而侦察结果又没变化
    # （服务端缓存、或者数据本身有抖动），原来的循环会一轮一轮无限试下去。
    rescans = 0
    max_rescans = 6

    while not stop():
        fresh = refresh_plan()
        fresh = [p for p in fresh if (p['court'], p['slot']) not in tried]
        if not fresh:
            rescans += 1
            if rescans > max_rescans:
                log(f'  已经全量重试 {max_rescans} 轮还是没抢到，停止'
                    f'（避免无限重试空耗）。')
                result['stop_reason'] = 'exhausted'
                break
            if refresh_counter == 0:
                log('  候选已全部试过，重新侦察一次...')
            gov.wait()
            occupied, af = scout(session, token, courts, d, workers=workers, log=log)
            if af == len(courts):
                log('  token 失效，停止。')
                break
            tried.clear()
            refresh_counter = 0
            fresh = refresh_plan()
            if not fresh:
                log('  重新侦察后仍无可用候选，结束。')
                break

        # ---- 并发首发（可开关）：第一轮把前 N 个候选同时打出去 ----
        if parallel_first > 1 and not parallel_done:
            parallel_done = True
            batch = fresh[:parallel_first]
            if len(batch) > 1:
                log('\n>>> 并发首发 ' + str(len(batch)) + ' 个候选：'
                    + '、'.join(f'{p["court"]} {p["slot"]}' for p in batch))
                gov.reset()
                with ThreadPoolExecutor(max_workers=len(batch)) as ex:
                    outs = list(ex.map(
                        lambda p: api_submit(session, token, p['sku'], d,
                                             p['slot']), batch))
                wins, hard = [], None
                for p, (v, st, da) in zip(batch, outs):
                    tried.add((p['court'], p['slot']))
                    result['attempts'].append({
                        'court': p['court'], 'slot': p['slot'],
                        'verdict': v, 'http': st, 'body': brief(da, 200),
                        'parallel': True})
                    log(f'    [并发] {p["court"]} {p["slot"]} → '
                        f'{VERDICT_CN.get(v, v)}'
                        + ('' if v == 'success' else f' | {brief(da, 100)}'))
                    if v == 'success':
                        wins.append(p)
                    elif v in ('auth_error', 'no_permission'):
                        hard = hard or (p, v)
                    elif v in ('pay_error', 'limit_reached'):
                        hard = hard or (p, v)
                refresh_counter += len(batch)

                if wins:
                    booked = wins[0]
                    log(f'\n✓✓ 抢到！{booked["court"]} {d} {booked["slot"]}（1 小时）')
                    if len(wins) > 1:
                        log(f'  ⚠ 并发首发里有 {len(wins)} 个都返回成功：'
                            + '、'.join(f'{p["court"]} {p["slot"]}' for p in wins))
                        log('    请到「我的订单」核对，多出来的那几单需要手动退掉。')
                    else:
                        log('   已熔断，不会再下第二单。')
                    break
                if hard:
                    p, v = hard
                    log(f'  × {VERDICT_CN.get(v, v)}（{p["court"]}），停止。')
                    result['stop_reason'] = v
                    result['success'] = False
                    return result
                # 并发全部没成 → 回到循环顶，剩下的候选按串行继续
                continue

        target = fresh[0]
        key = (target['court'], target['slot'])
        tried.add(key)
        refresh_counter += 1

        log(f'\n>>> 尝试 {target["court"]} {d} {target["slot"]}')

        # burst：对同一个候选连打若干次（间隔受速率器约束），
        # 用于抢「刚放号」的窗口；只要成功立刻熔断，不会重复下单。
        for b in range(burst):
            if stop():
                break
            gov.wait()
            verdict, status, data = api_submit(session, token, target['sku'], d, target['slot'])
            result['attempts'].append({'court': target['court'], 'slot': target['slot'],
                                       'verdict': verdict, 'http': status,
                                       'body': brief(data, 200)})
            log(f'    #{b + 1} HTTP={status} → {VERDICT_CN.get(verdict, verdict)}'
                + (f' | {brief(data, 120)}' if verdict not in ('success',) else ''))

            if verdict == 'success':
                log(f'\n✓✓ 抢到！{target["court"]} {d} {target["slot"]}（1 小时）')
                log('   已将下单熔断，不会再下第二单。')
                booked = target
                break
            if verdict in ('auth_error', 'no_permission'):
                log(f'   × {VERDICT_CN[verdict]}，停止（请检查 token）。')
                booked = None
                stop_reason = verdict
                result['stop_reason'] = stop_reason
                result['success'] = False
                return result
            if verdict == 'pay_error':
                log('   × 支付/余额问题，停止。')
                result['stop_reason'] = 'pay_error'
                return result
            if verdict == 'limit_reached':
                log('   × 触发每日额度限制（每人每天 1 小时），停止。')
                result['stop_reason'] = 'limit_reached'
                return result
            if verdict == 'conflict':
                log('     时段已被占，换下一个候选。')
                break
            if verdict == 'rate_limited':
                # 挨了限流就把节流拧慢一档，别硬顶着继续被打；
                # 同时恢复得比原来快一点（原来是死睡 1.0~1.8s）。
                gov.min_interval = min(0.6, gov.min_interval * 1.5 + 0.02)
                wait_s = 0.5 + rng.uniform(0, 0.4)
                log(f'     ⚠ 被限流，节流放宽到 {gov.min_interval:.2f}s，'
                    f'{wait_s:.1f}s 后重试')
                time.sleep(wait_s)
                continue
            if verdict == 'not_open':
                not_open_streak += 1
                if not_open_streak > not_open_max:
                    log(f'     连续 {not_open_max} 次「未开放预约」，停止。')
                    result['stop_reason'] = 'not_open'
                    return result
                # 「还没开放」时要贴着放号那一刻反复探，间隔交给节流器；
                # 原来这里硬睡 250ms，等于把精度浪费掉 2~3 倍。
                continue
            # unknown
            if b < burst - 1:
                time.sleep(0.08)
                continue
            log('     响应不明确，换下一个候选。')
            break

        if booked:
            break

        # 每 refresh_rounds 个候选后，重新侦察一次空场（查询不消耗额度）
        if refresh_rounds and refresh_counter >= refresh_rounds:
            refresh_counter = 0
            log('  -- 刷新空场图 --')
            gov.wait()
            occupied, af = scout(session, token, courts, d, workers=workers, log=log)
            if af == len(courts):
                log('  token 失效，停止。')
                break
            tried.clear()

    if booked:
        result['success'] = True
        result['booked'] = {'court': booked['court'], 'slot': booked['slot'], 'date': d}
    else:
        log('\n△ 未抢到，详见上方日志。')
    return result


# ---------------------------------------------------------------------------
# 【需求4】测速器
# ---------------------------------------------------------------------------

def benchmark(params, log=print, stop=None):
    """实测接口速率与限流情况。

    分三段：
      A. getDay 串行 N 次   —— 单次往返 + 串行 QPS
      B. getDay 并发 N 次   —— 并发侦察能达到的 QPS
      C. submitOrder K 次   —— 用「过去日期 + 不开放时段」的安全参数，
                                绝不会真的下单扣费，只测往返与限流
    """
    stop = stop or (lambda: False)
    token = (params.get('token') or '').strip()
    d = params.get('target_date') or (date_cls.today() + timedelta(days=2)).isoformat()
    n_serial = int(params.get('bench_n') or 30)
    n_conc = int(params.get('bench_conc') or 14)
    workers = int(params.get('scout_workers') or 7)
    n_submit = int(params.get('bench_submit') or 10)

    out = {'ok': False, 'token_ok': None, 'serial': {}, 'concurrent': {},
           'submit': {}, 'advice': ''}

    if not token:
        log('× 未填 token，无法测速。')
        out['error'] = '未填 token'
        return out

    session = requests.Session()
    session.verify = False

    log('=' * 66)
    log(' 接口测速（只测往返与限流，不会产生任何真实订单）')
    log('=' * 66)

    # ---------- 探活 ----------
    log('\n[0/3] 探活：getDay 单次')
    occ, status, data = api_getday(session, token, COURTS_FALLBACK[0]['sku'], d)
    if status == 401 or (isinstance(data, dict) and data.get('code') == 401):
        out['token_ok'] = False
        log('  × token 已失效（code=401 认证失败）。')
        log('    说明：401 是鉴权层直接短路返回，耗时偏乐观；')
        log('    要得到真实下单速率，请填一个有效 token 再测。')
    else:
        out['token_ok'] = True
        log(f'  ✓ token 有效，HTTP={status}')

    # ---------- A 串行 ----------
    log(f'\n[1/3] getDay 串行 {n_serial} 次（查空场，只读）')
    sku = COURTS_FALLBACK[0]['sku']
    times, codes = [], {}
    t0 = time.perf_counter()
    for i in range(n_serial):
        if stop():
            break
        s1 = time.perf_counter()
        try:
            _, st, dd = api_getday(session, token, sku, d)
            c = (dd or {}).get('code') if isinstance(dd, dict) else f'HTTP{st}'
        except Exception as e:
            c = f'ERR:{str(e)[:30]}'
        times.append(time.perf_counter() - s1)
        codes[str(c)] = codes.get(str(c), 0) + 1
    total = time.perf_counter() - t0
    if times:
        avg = sum(times) / len(times)
        out['serial'] = {'n': len(times), 'total_s': round(total, 3),
                         'avg_ms': round(avg * 1000), 'min_ms': round(min(times) * 1000),
                         'max_ms': round(max(times) * 1000),
                         'qps': round(len(times) / total, 2), 'codes': codes}
        log(f'  总耗时 {total:.2f}s ｜ 平均 {avg*1000:.0f}ms ｜ '
            f'最快 {min(times)*1000:.0f}ms ｜ 最慢 {max(times)*1000:.0f}ms')
        log(f'  ⇒ 串行上限约 {len(times)/total:.1f} 次/秒')
        log(f'  响应分布：{codes}')

    # ---------- B 并发 ----------
    log(f'\n[2/3] getDay 并发 {n_conc} 次 / {workers} 线程（模拟开抢前的侦察）')
    courts = COURTS_FALLBACK[:max(1, workers)]
    t0 = time.perf_counter()
    rounds = max(1, n_conc // len(courts))
    done = 0
    codes2 = {}
    for _ in range(rounds):
        if stop():
            break
        occupied, af = scout(session, token, courts, d, workers=workers, log=lambda *a: None)
        done += len(courts)
    total2 = time.perf_counter() - t0
    if total2 > 0:
        out['concurrent'] = {'n': done, 'total_s': round(total2, 3),
                             'qps': round(done / total2, 2), 'workers': workers}
        log(f'  {done} 次请求 / {total2:.2f}s ⇒ 并发约 {done/total2:.1f} 次/秒')
        log(f'  ⇒ 一次「7 片场地全量侦察」约 {total2/max(1,rounds)*1000:.0f}ms 完成')

    # ---------- C 下单 ----------
    log(f'\n[3/3] submitOrder {n_submit} 次（安全参数：过去日期 + 不开放时段，绝不成单）')
    safe_date = '2020-01-01'
    safe_time = '03:00-04:00'
    times3, res3 = [], {}
    for i in range(n_submit):
        if stop():
            break
        s1 = time.perf_counter()
        verdict, st, dd = api_submit(session, token, COURTS_FALLBACK[0]['sku'],
                                     safe_date, safe_time)
        times3.append(time.perf_counter() - s1)
        key = f'{VERDICT_CN.get(verdict, verdict)}｜{brief(dd, 80)}'
        res3[key] = res3.get(key, 0) + 1
        time.sleep(0.15)
    if times3:
        avg3 = sum(times3) / len(times3)
        out['submit'] = {'n': len(times3), 'avg_ms': round(avg3 * 1000),
                         'min_ms': round(min(times3) * 1000),
                         'max_ms': round(max(times3) * 1000),
                         'qps': round(1 / avg3, 2), 'responses': res3}
        log(f'  平均 {avg3*1000:.0f}ms ｜ 最快 {min(times3)*1000:.0f}ms ｜ '
            f'最慢 {max(times3)*1000:.0f}ms ⇒ 约 {1/avg3:.1f} 次/秒')
        for k, v in res3.items():
            log(f'    x{v}  {k}')

    # ---------- 结论 ----------
    limited = any(k in str(out['serial'].get('codes', {})) for k in ('429', '503', '限流'))
    lines = []
    if out['serial'].get('qps'):
        q = out['serial']['qps']
        lines.append(f'查询（getDay）：实测串行约 {q:.1f} 次/秒，'
                     f'并发约 {out["concurrent"].get("qps", 0):.1f} 次/秒；')
        lines.append('  它是只读接口，不写库、不消耗订场额度 → 可以放心高频侦察。')
    if out['submit'].get('qps'):
        lines.append(f'下单（submitOrder）：实测约 {out["submit"]["qps"]:.1f} 次/秒。')
        lines.append('  但你每天只有 1 小时额度，抢到即停，')
        lines.append('  真正的瓶颈是「第一发什么时候出去」，不是持续 QPS。')
    lines.append('建议参数：')
    lines.append(f'  · 最小间隔取 0.10~0.15s（≈7~10 次/秒），既能抢到又不触发风控；')
    lines.append('  · 侦察用 7 线程并发，一次全量空场图 <1 秒；')
    lines.append('  · 提交一律串行 —— 同一账号并发下单会重复扣费，且违反一天一单。')
    if limited:
        lines.append('  ⚠ 检测到限流响应，请把最小间隔调到 0.3s 以上。')
    out['advice'] = '\n'.join(lines)
    out['ok'] = True
    log('\n[结论]')
    for ln in lines:
        log('  ' + ln)
    return out


# ---------------------------------------------------------------------------
# 连通性 / 有效性诊断
# ---------------------------------------------------------------------------

def diagnose(params, log=print):
    token = (params.get('token') or '').strip()
    d = params.get('target_date') or ''
    win_start = params.get('window_start') or '18:00'
    win_end = params.get('window_end') or '22:00'
    out = {'ok': False, 'token_ok': False, 'courts': [], 'occupied': {}, 'error': None}

    log('=' * 66)
    log(' 下单有效性检测')
    log('=' * 66)
    if not token:
        log('× 未填 token。')
        out['error'] = '未填 token'
        return out
    if not d:
        log('× 未选日期。')
        out['error'] = '未选日期'
        return out

    session = requests.Session()
    session.verify = False

    log('\n[1/3] place/list（不校验身份，只证明网络通）')
    try:
        courts = api_places(session, token)
        log(f'  ✓ HTTP 200，拿到 {len(courts)} 片场地')
        for c in courts:
            log(f"    - {c['name']}  sku={c['sku']}")
        out['courts'] = [{'name': c['name'], 'sku': c['sku']} for c in courts]
    except Exception as e:
        log(f'  × 失败：{e}')
        out['error'] = f'place/list 失败：{e}'
        return out

    log('\n[2/3] getDay（校验身份 —— 只有它能证明 token 有效）')
    occ, status, data = api_getday(session, token, courts[0]['sku'], d) if courts else (None, None, None)
    log(f'  HTTP={status}  body={brief(data, 200)}')
    if status == 401 or (isinstance(data, dict) and data.get('code') == 401):
        log('  × token 已失效，请重新进小程序预订流程抓最新的 Bearer token。')
        out['error'] = 'token 已失效'
        return out
    out['token_ok'] = True
    log('  ✓ token 有效')

    log('\n[3/3] 拉取全部场地占用')
    occupied, af = scout(session, token, courts, d, workers=7, log=log)
    slots = enumerate_slots(win_start, win_end)
    for c in courts:
        o = sorted(occupied.get(c['sku'], set()))
        out['occupied'][c['name']] = o
        free = [s for s in slots if s not in o and court_open_at(c, weekday_cn_of(d), s)]
        log(f"    {c['name']}：占用于 {', '.join(o) if o else '无'} → "
            f"窗口内可抢 {', '.join(free) if free else '无'}")
    out['ok'] = True
    return out
