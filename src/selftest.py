# -*- coding: utf-8 -*-
"""离线自检（不联网）：规划 / 随机 / 判定 / 速率 / 配置。
运行： python selftest.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import booker as b
import accounts as A

FAIL = []


def check(cond, msg, extra=''):
    """extra 是可选的补充说明（失败原因、实际值等），只在有内容时追加。"""
    if cond:
        print(f'  ✓ {msg}')
    else:
        print(f'  ✗ {msg}' + (f'  -- {extra}' if extra else ''))
        FAIL.append(msg)


print('[1] 时段枚举（只订 1 小时）')
slots = b.enumerate_slots('06:00', '22:00')
check(len(slots) == 16, f'06:00-22:00 → 16 个整点时段（实际 {len(slots)}）')
check(slots[0] == '06:00-07:00' and slots[-1] == '21:00-22:00',
      f'首尾正确：{slots[0]} ... {slots[-1]}')
check(all(int(s.split('-')[1][:2]) - int(s.split('-')[0][:2]) == 1 for s in slots),
      '每个时段都是整 1 小时')
check(b.enumerate_slots('18:00', '22:00') == ['18:00-19:00', '19:00-20:00',
                                              '20:00-21:00', '21:00-22:00'],
      '18:00-22:00 → 4 段')
check(b.enumerate_slots('18:00', '18:30') == [], '不足 1 小时 → 空')

print('\n[2] 开放表（周一~周四 19-21 是专业课，不开放）')
c4 = {'name': '4号场', 'sku': 'x',
      'weekly': b.FALLBACK_WEEKLY['4号场']}
c1 = {'name': '1号场', 'sku': 'y',
      'weekly': b.FALLBACK_WEEKLY['1号场']}
check(b.court_open_at(c4, '周一', '18:00-19:00'), '周一 4号场 18-19 开放')
check(not b.court_open_at(c4, '周一', '19:00-20:00'), '周一 4号场 19-20 不开放（专业课）')
check(not b.court_open_at(c4, '周一', '20:00-21:00'), '周一 4号场 20-21 不开放')
check(b.court_open_at(c4, '周一', '21:00-22:00'), '周一 4号场 21-22 开放')
check(b.court_open_at(c4, '周五', '20:00-21:00'), '周五 4号场 20-21 开放')
check(not b.court_open_at(c1, '周三', '18:00-19:00'), '周三 1号场 18-19 不开放')
check(b.court_open_at(c1, '周三', '21:00-22:00'), '周三 1号场 21-22 开放')

print('\n[3] 候选生成与排序')
courts = [{'name': f'{i}号场', 'sku': f'sku{i}',
           'weekly': b.FALLBACK_WEEKLY[f'{i}号场']} for i in range(1, 8)]
slots = b.enumerate_slots('18:00', '22:00')
order = ['4号场', '5号场', '6号场', '7号场', '2号场', '3号场', '1号场']
enabled = {c['name']: True for c in courts}
occ = {'sku4': {'18:00-19:00'}, 'sku5': set(), 'sku6': set(), 'sku7': set(),
       'sku2': set(), 'sku3': set(), 'sku1': set()}
tmp = tempfile.mkdtemp()
rng = b.RandomSource(os.path.join(tmp, '.salt'), enabled=True)

plan = b.make_candidates(courts, '周六', slots, occ, order, enabled, rng, b.SORT_COURT)
check(plan[0]['court'] == '4号场', f'严格顺序：首个候选是 4号场（实际 {plan[0]["court"]}）')
check(plan[0]['slot'] == '19:00-20:00', f'4号场 18-19 被占 → 顺延到 19-20（实际 {plan[0]["slot"]}）')
check(all(p['slot'] != '18:00-19:00' or p['court'] != '4号场' for p in plan),
      '已占用时段不出现在候选里')

plan_t = b.make_candidates(courts, '周六', slots, occ, order, enabled, rng, b.SORT_TIME)
check(plan_t[0]['slot'] == '18:00-19:00', f'时间优先：首个是 18-19（实际 {plan_t[0]["slot"]}）')

plan_r = b.make_candidates(courts, '周六', slots, occ, order, enabled, rng, b.SORT_SHUFFLE)
check(len(plan_r) == len(plan), '完全随机：候选数量不变')

enabled2 = dict(enabled); enabled2['4号场'] = False
plan_e = b.make_candidates(courts, '周六', slots, occ, order, enabled2, rng, b.SORT_COURT)
check(all(p['court'] != '4号场' for p in plan_e), '停用场地不进候选')

plan_wd = b.make_candidates(courts, '周一', slots, occ, order, enabled, rng, b.SORT_COURT)
check(all(p['slot'] in ('18:00-19:00', '21:00-22:00') for p in plan_wd),
      '周一候选只剩 18-19 与 21-22')

print('\n[4] 反冲突随机（非时间种子）')
salt_a = os.path.join(tmp, 'sa'); salt_b = os.path.join(tmp, 'sb')
r1 = b.RandomSource(salt_a, enabled=True)
r2 = b.RandomSource(salt_b, enabled=True)
check(r1.salt != r2.salt, '不同盐文件 → 不同盐值')
check(r1.fingerprint != r2.fingerprint, f'不同签名：{r1.fingerprint} vs {r2.fingerprint}')
r3 = b.RandomSource(salt_a, enabled=True)
check(r3.fingerprint == r1.fingerprint, '同一盐 + 同口令 → 签名稳定（可复现）')
r4 = b.RandomSource(salt_a, passphrase='abc', enabled=True)
check(r4.fingerprint != r1.fingerprint, '加口令后签名改变')
seq1 = [r1.randint(0, 10**6) for _ in range(20)]
seq1b = [b.RandomSource(salt_a, enabled=True).randint(0, 10**6) for _ in range(20)]
check(seq1 != seq1b, '同一台机两次运行序列也不同（混入了 os.urandom）')
src = list(range(50))
sh1 = b.RandomSource(salt_a, enabled=True).shuffle(src)
sh2 = b.RandomSource(salt_b, enabled=True).shuffle(src)
check(sh1 != sh2, '不同脚本打乱结果不同')
check(sorted(sh1) == src, '打乱不丢元素')
off = b.RandomSource(salt_a, enabled=False)
check(off.shuffle(src) == src, '关闭反冲突时保持原序')

print('\n[5] 判定')
check(b.judge({'code': 200, 'msg': '下单成功'}) == 'success', '下单成功 → success')
check(b.judge({'code': 401, 'msg': '请求访问：x，认证失败，无法访问系统资源'}) == 'auth_error',
      'code=401 → auth_error')
# 【V4.2】「未开放」拆成暂时 / 永久两类：永久必须立即停，暂时才原地重试。
# 归类保守 —— 措辞含糊的一律算暂时（误判成永久的代价是 T+0 直接中止，整场抢不到）。
check(b.judge({'code': 500, 'msg': '日期超过可提前天数'}) == 'out_of_window',
      '超出提前天数 → out_of_window（永久，立即停）')
check(b.judge({'code': 500, 'msg': '超出可提前预约天数'}) == 'out_of_window',
      '超出可提前 → out_of_window（永久）')
check(b.judge({'code': 500, 'msg': '未开放预约'}) == 'not_open_yet',
      '未开放预约 → not_open_yet（暂时，原地重试）')
check(b.judge({'code': 500, 'msg': '尚未开放'}) == 'not_open_yet',
      '尚未开放 → not_open_yet（暂时）')
check(b.judge({'code': 500, 'msg': '不在可预约时间'}) == 'not_open_yet',
      '不在可预约时间（歧义）→ not_open_yet，不算永久')
check('not_open' not in b.VERDICT_CN and 'not_open_yet' in b.VERDICT_CN
      and 'out_of_window' in b.VERDICT_CN, '旧 not_open 已退役，两个新 verdict 都有中文')
check(b.judge({'code': 500, 'msg': '该时段已被预约'}) == 'conflict', '已被预约 → conflict')
check(b.judge({'code': 500, 'msg': '余额不足'}) == 'pay_error', '余额不足 → pay_error')
check(b.judge({'code': 500, 'msg': '无权限访问'}) == 'no_permission', '无权限 → no_permission')
check(b.judge({'code': 500, 'msg': '请求过于频繁'}) == 'rate_limited', '频繁 → rate_limited')
check(b.judge({'code': 500, 'msg': '每人每天只能预订1小时'}) == 'limit_reached',
      '每人每天1小时 → limit_reached')
check(b.judge('boom', status=429) == 'rate_limited', 'HTTP 429 → rate_limited')

print('\n[6] 速率治理')
g = b.RateGovernor(min_interval=0.10, jitter_ms=0)
t0 = time.perf_counter()
for _ in range(4):
    g.wait()
el = time.perf_counter() - t0
check(el >= 0.28, f'4 次请求至少间隔 0.30s（实际 {el:.3f}s）')
check(abs(g.theoretical_qps - 10) < 0.01, f'0.10s 间隔 → 10 次/秒（实际 {g.theoretical_qps:.1f}）')
g2 = b.RateGovernor(min_interval=0.05, jitter_ms=100)
check(8 < g2.theoretical_qps < 11, f'含抖动后理论值合理（{g2.theoretical_qps:.1f}）')

print('\n[7] 配置与矩阵')
cfg = b.default_config()
check(cfg['court_order'][0] == '4号场', f'默认首选 4号场（{cfg["court_order"][0]}）')
check(len(cfg['court_order']) == 7, '7 片场地齐全')
check(all(cfg['court_enabled'][c['name']] for c in b.COURTS_FALLBACK), '默认全部参与')
m = b.build_matrix(courts, '周六', slots, occ)
check(m['4号场']['18:00-19:00'] == 'occupied', '矩阵：4号场 18-19 标为已占')
check(m['5号场']['18:00-19:00'] == 'open', '矩阵：5号场 18-19 可抢')
m2 = b.build_matrix(courts, '周一', slots, occ)
check(m2['4号场']['19:00-20:00'] == 'closed', '矩阵：周一 19-20 不开放')
check('occupied' not in (b.build_matrix(courts, '周一', slots, {})['1号场'].values()),
      '周一 1号场不出现「已占」误判')

print('\n[8] 账号库（V4.0）')
c0 = A.migrate({})
check(len(c0['accounts']) == 1, '空配置 → 自动生成「账号一」')
check(c0['accounts'][0]['name'] == '账号一', '首个账号名为 账号一')
check(c0['active'] == c0['accounts'][0]['id'], 'active 指向首个账号')

# V3.1 旧配置迁移
old = b.default_config()
old['token'] = 'eyJhbGciOiJIUzUxMiJ9.OLDTOKEN'
old['app_user_id'] = '70505'
old['window_start'] = '06:00'
old['court_order'] = ['1号场', '2号场']
c1 = A.migrate(old)
check(len(c1['accounts']) == 1, '旧版单 token → 收编成 1 个账号')
check(c1['accounts'][0]['token'].endswith('OLDTOKEN'), 'token 迁进账号')
check(c1['accounts'][0]['app_user_id'] == '70505', '用户ID 迁进账号')
check(c1['accounts'][0]['window_start'] == '06:00', '时间窗口迁进账号')
check(c1['accounts'][0]['court_order'] == ['1号场', '2号场', '3号场', '4号场',
                                           '5号场', '6号场', '7号场'],
      '场地顺序迁移时补齐缺失的场地')

# 反复登录 → 追加账号
c2 = A.migrate({})
a1, n1 = A.upsert_login(c2, '20210001', 'TOKEN-A', app_user_id='111')
check(n1 is True, '第一次登录 → 新建账号')
check(a1['name'] == '账号一', f'接住空白行，仍叫 账号一（实际 {a1["name"]}）')
a2, n2 = A.upsert_login(c2, '20210002', 'TOKEN-B', app_user_id='222')
check(n2 is True and a2['name'] == '账号二', f'第二个学号 → 账号二（实际 {a2["name"]}）')
a3, n3 = A.upsert_login(c2, '20210003', 'TOKEN-C')
check(a3['name'] == '账号三', f'第三个学号 → 账号三（实际 {a3["name"]}）')
check(len(c2['accounts']) == 3, '库里共 3 个账号')

# 同学号再登录 → 原地更新，不新增行
a1b, n1b = A.upsert_login(c2, '20210001', 'TOKEN-A2', ok=True, why='有效')
check(n1b is False, '同学号再次登录 → 不新增行')
check(a1b is a1 and a1b['token'] == 'TOKEN-A2', '原地更新 token')
check(len(c2['accounts']) == 3, '账号数量仍是 3')
check(c2['active'] == a1b['id'], '登录后 active 切到刚登录的账号')
check(A.find_by_username(c2, '20210002') is a2, '按学号能查回账号')

# 空行优先复用（用户先点了 ＋）
c3 = A.migrate({})
A.add_blank(c3)
check(len(c3['accounts']) == 2, '＋ 新增空白账号 → 库里 2 行')
a4, _ = A.upsert_login(c3, '20210009', 'TOKEN-D')
check(len(c3['accounts']) == 2, '登录时优先填空白行，不无限增长')
check(a4['name'] == '账号二' and a4['token'] == 'TOKEN-D', '空白行被填上')

# 删除
c4 = A.migrate({})
A.upsert_login(c4, 'u1', 'T1')
A.upsert_login(c4, 'u2', 'T2')
A.remove(c4, c4['accounts'][0]['id'])
check(len(c4['accounts']) == 1, '删除后剩 1 个账号')
A.remove(c4, c4['accounts'][0]['id'])
check(len(c4['accounts']) == 0, '删空后 accounts 为空')
check(A.migrate(c4)['accounts'] == [] or True, '删空后再次迁移不炸')

# 前端整包保存：前端没带 token 时不能把库里的 token 抹掉
stored = A.migrate({})
A.upsert_login(stored, 'u1', 'SECRET')
incoming = [dict(stored['accounts'][0], token='')]
merged = A.merge_accounts(stored['accounts'], incoming)
check(merged[0]['token'] == 'SECRET', '整包保存时保留原有 token')
c6 = A.migrate({})
A.upsert_login(c6, 'u1', 'T1')
A.upsert_login(c6, 'u2', 'T2')
m6 = A.merge_accounts(c6['accounts'], [c6['accounts'][0]])
check(len(m6) == 2, '前端少传的账号不会被这次保存抹掉')

# 打码 / 预览
tk = 'eyJhbGciOiJIUzUxMiJ9' + 'x' * 200
check(A.preview(tk) == tk[:18] + '…', 'preview 保留前 18 位并加省略号')
check(A.preview(tk).startswith(tk[:18]), 'preview 前缀与原文一致')
check('x' * 50 not in A.mask(tk), '打码后不出现大段原文')
check(len(A.runnable(A.migrate({}))) == 0, '没 token 的账号不参与抢单')
c5 = A.migrate({})
A.upsert_login(c5, 'u1', 'T1')
A.upsert_login(c5, 'u2', 'T2')
c5['accounts'][1]['enabled'] = False
check(len(A.runnable(c5)) == 1, '停用的账号不参与抢单')
check(A.view(c5)[0]['has_token'] is True, 'view() 带出 has_token')
check(A.view(c5)[0]['masked'] != '', 'view() 带出打码后的 token')

# 落盘 / 读回
p = os.path.join(tempfile.mkdtemp(), 'config.json')
A.save(p, c5)
back = A.load(p)
check(len(back['accounts']) == 2, '保存再读回，账号数一致')
check(back['accounts'][0]['token'] == c5['accounts'][0]['token'], 'token 落盘无损')
check(back['accounts'][0]['court_order'] == c5['accounts'][0]['court_order'],
      '场地顺序落盘无损')

# id 唯一（重复 id 会让按 id 定位账号串号）
c6 = A.migrate({'accounts': [{'name': '甲'}, {'name': '乙'}, {'name': '丙'}]})
ids6 = [a['id'] for a in c6['accounts']]
check(len(set(ids6)) == 3, '多个无 id 的账号各自拿到唯一 id')
check(len(ids6) == 3, '账号数量不受去重影响')

c7 = A.migrate({'accounts': [{'id': 'acc1', 'name': '甲'},
                             {'id': 'acc1', 'name': '乙'}]})
ids7 = [a['id'] for a in c7['accounts']]
check(len(set(ids7)) == 2, '前端传来的重复 id 会被纠正')
check(A.get_account(c7, ids7[1])['name'] == '乙', '改 id 后仍能按 id 查到正确的号')
check(A.get_account(c7, c7['active']) is not None, 'active 指向一个真实存在的账号')

# 整包保存时前端漏传 id → 不能被追加成重名两行
s = [{'id': 'acc1', 'name': '甲', 'username': '001', 'token': 'tk1'},
     {'id': 'acc2', 'name': '乙', 'username': '002', 'token': 'tk2'}]
m = A.merge_accounts(s, [{'name': '甲', 'username': '001'},
                         {'name': '乙', 'username': '002'}])
check(len(m) == 2, '漏传 id 时按学号回退匹配，不会变成 4 行')
check(m[0].get('id') == 'acc1', '回退匹配后 id 仍是原来的 acc1')
check(m[0].get('token') == 'tk1', '回退匹配后 token 也没丢')
m2 = A.merge_accounts(s, [{'username': '009', 'name': '丙'}])
check(len(m2) == 3, '真正的新号照常追加')

print('\n[9] V4.1 挂机自动刷新（密码存储 / 日期跟随）')
# ---- 密码混淆：能还原、不落明文 ----
raw = 'Tennis@7148'
enc = A.encode_password(raw)
check(enc.startswith('v1:'), '密码存成 v1: 前缀的混淆串')
check(raw not in enc, '混淆串里不出现明文')
check(A.decode_password(enc) == raw, '能还原成原密码')
check(A.encode_password('') == '' and A.decode_password('') == '',
      '空密码往返安全')
check(A.decode_password('not-a-v1-string') == 'not-a-v1-string',
      '老格式（万一存过明文）原样返回')
check(A.decode_password('v1:@@@bad@@@') == '', '坏数据不炸，返回空')

# ---- account_password：只有勾了「记住」才给密码 ----
a1 = A.default_account({})
a1['password'] = A.encode_password(raw)
check(A.account_password(a1) == '', '没勾「记住密码」时拿不到密码')
a1['remember_password'] = True
check(A.account_password(a1) == raw, '勾了「记住密码」才拿到明文')

# ---- 整包保存不会把密码抹掉 ----
stored = [dict(a1, id='acc1', username='u1')]
incoming = [{'id': 'acc1', 'username': 'u1', 'password': ''}]
merged = A.merge_accounts(stored, incoming)
check(merged[0]['password'] == a1['password'], '前端回传空密码 → 保留库里的')
check(merged[0]['remember_password'] is True, '「记住」开关也一起保留')
incoming2 = [{'id': 'acc1', 'username': 'u1', 'password': A.encode_password('new')}]
check(A.merge_accounts(stored, incoming2)[0]['password'] == A.encode_password('new'),
      '传了新密码 → 正常覆盖')

# ---- view() 不外泄 ----
v = A.view({'accounts': [dict(a1, id='acc1')]})[0]
check(v.get('has_password') is True, 'view() 带出 has_password 标记')

# ---- 新全局参数进了默认配置、也进了迁移白名单（否则「UI 能设、存不住」）----
dc = A.default_config()
for k in ('auto_refresh_token', 'refresh_lead_min', 'refresh_timeout',
          'auto_roll_date'):
    check(k in dc, f'默认配置里有 {k}')
check(dc['refresh_lead_min'] == 15, '默认提前 15 分钟刷新')
check(dc['auto_refresh_token'] is True, '默认开启自动刷新')
mig = A.migrate({'auto_refresh_token': False, 'refresh_lead_min': 30,
                 'refresh_timeout': 600, 'auto_roll_date': False})
check(mig['auto_refresh_token'] is False, '迁移：auto_refresh_token 存得住')
check(mig['refresh_lead_min'] == 30, '迁移：refresh_lead_min 存得住')
check(mig['refresh_timeout'] == 600, '迁移：refresh_timeout 存得住')
check(mig['auto_roll_date'] is False, '迁移：auto_roll_date 存得住')

print('\n[10] V4.2 安全护栏（会扣钱的坑，全部锁死）')
from datetime import date, datetime, timedelta
_today = date(2026, 9, 23)
_yesterday = (_today - timedelta(days=1)).isoformat()
_tomorrow = (_today + timedelta(days=1)).isoformat()

# 时段过滤：过去的时段绝不进候选（过去时段服务端照样成交扣钱）
keep, drop = b.filter_future_slots(['06:00-07:00', '08:00-09:00', '20:00-21:00'],
                                   _today.isoformat(),
                                   now=datetime(2026, 9, 23, 19, 30))
check(keep == ['20:00-21:00'], f'今天 19:30 时只剩 20-21（实际 {keep}）')
check(len(drop) == 2, f'两个已过时段被滤掉（实际 {len(drop)}）')
keep2, drop2 = b.filter_future_slots(['06:00-07:00'], _tomorrow,
                                     now=datetime(2026, 9, 23, 23, 59))
check(keep2 == ['06:00-07:00'] and not drop2, '明天的时段全部保留')
keep3, drop3 = b.filter_future_slots(['06:00-07:00'], _yesterday,
                                     now=datetime(2026, 9, 23, 8, 0))
check(keep3 == [] and len(drop3) == 1, '昨天的时段全部滤掉')

# judge 归类（与 [5] 互补：这里专锁「永久 vs 暂时」的边界）
check(b.judge({'code': 500, 'msg': '超出可提前预约天数，最多提前3天'}) == 'out_of_window',
      '带具体数字的「超出可提前」仍判永久')
check(b.judge({'code': 500, 'msg': '该日期暂未开放预约'}) == 'not_open_yet',
      '「暂未开放预约」判暂时')

# benchmark() 源码里绝不再出现 api_submit 调用（AST 级断言）
import ast as _ast, inspect as _inspect
import inspect
_bsrc = _inspect.getsource(b.benchmark)
_tree = _ast.parse(_bsrc)
_calls = [n.func.id for n in _ast.walk(_tree)
          if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)]
check('api_submit' not in _calls, 'benchmark() 不再调用 api_submit（测速不碰下单）')

print('\n[11] 配置白名单往返（UI 能设就必须存得住）')
# parallel_first 是 V4.1 的漏项：界面有、保存时被 migrate 白名单丢弃
c = A.default_config()
check('parallel_first' in c, '默认配置里有 parallel_first')
c['parallel_first'] = 3
_p11 = os.path.join(tempfile.mkdtemp(), 'config.json')
A.save(_p11, c)
_back11 = A.load(_p11)
check(_back11.get('parallel_first') == 3,
      f'parallel_first 保存→读回不丢（实际 {_back11.get("parallel_first")}）')
_mig11 = A.migrate({'parallel_first': 5})
check(_mig11.get('parallel_first') == 5, 'migrate 白名单含 parallel_first')
_ui_keys = ('min_interval', 'jitter_ms', 'burst', 'parallel_first',
            'scout_workers', 'refresh_rounds', 'not_open_max_retries',
            'schedule', 'stop_on_first', 'one_per_day', 'anti_collision',
            'passphrase', 'auto_refresh_token', 'refresh_lead_min',
            'refresh_timeout', 'auto_roll_date', 'target_date')
for _k in _ui_keys:
    check(_k in A.default_config(), f'默认配置含界面可设键 {_k}')

print('\n[12] 日志脱敏（落盘/导出前必须过 redact）')
import app_server as S
_dirty = ('Bearer eyJhbGciOiJIUzUxMiJ9.abc123def456.ghi789jklm 登录成功，'
          '密码 v1:GBZEHg0123456 已保存，szblTK=eyJhbGciOiJIUzUxMiJ9.token999888 '
          'ticket=ST-12345-abcdefg 学号 20253007148')
_clean = S.redact(_dirty)
check('eyJ' not in _clean, 'JWT 被脱敏')
check('v1:GBZEHg' not in _clean, '混淆密码被脱敏')
check('szblTK=' not in _clean, 'szblTK 参数被脱敏')
check('ST-12345' not in _clean, 'CAS ticket 被脱敏')
check('20253007148' not in _clean, '11 位学号被脱敏')
check('2025' in _clean, '学号前 4 位保留（便于排查是哪一年入学）')
check(S.redact('普通日志一行') == '普通日志一行', '正常内容原样通过')

print('\n[13] E4 真机验收发现的两处洞（已修，别再退回去）')
# —— 洞一：/api/config 把混淆密码串一起下发给页面 ——
# accounts.view() 立过规矩「密码串绝不下发」，但 view_config() 绕过它直接吐原样配置。
# 界面只用一个 has_password 标记，那个串纯粹是白送的泄漏面。
_tdir = tempfile.mkdtemp(prefix='v42_view_')
_tcfg = A.default_config()
_ta = A.default_account(_tcfg, name='有密码的号')
_ta['token'] = 'eyJhbGciOiJIUzUxMiJ9.eyJhIjoiYiJ9.sig'
_ta['password'] = A.encode_password('somepassword')
_ta['remember_password'] = True
_tcfg['accounts'] = [_ta]
_tcfg['active'] = _ta['id']
_tpath = os.path.join(_tdir, 'config.json')
A.save(_tpath, _tcfg)
S.CONFIG_PATH = _tpath                      # 让 view_config 读这份临时配置
_v = S.view_config()
_va = (_v.get('accounts') or [])[0]
check('v1:' not in json.dumps(_v, ensure_ascii=False),
      '/api/config 不下发混淆密码串')
check(_va.get('password', None) == '', '密码格被清空')
check(_va.get('has_password') is True, '但 has_password 标记还在（界面徽章靠它）')
check(_va.get('remember_password') is True, 'remember_password 保留')
# token 是故意保留完整的：👁 看全 / ⧉ 复制要用
check(_va.get('token') == _ta['token'], 'token 仍完整下发（👁/⧉ 要用）')
# 被打码的那份只走 /api/accounts
_vm = A.view(A.load(_tpath))[0]
check('v1:' not in json.dumps(_vm, ensure_ascii=False),
      'accounts.view() 同样不含密码串')
# 保存回合：界面回传空密码时，库里的密码不能被抹掉
_merged = A.merge_accounts(A.load(_tpath).get('accounts'),
                           [{'id': _ta['id'], 'name': '有密码的号',
                             'token': _ta['token'], 'password': '',
                             'remember_password': True}])
check((_merged[0].get('password') or '') == _ta['password'],
      '界面回传空密码后，库里的密码仍在（普通保存不会抹掉挂机密码）')
# —— 洞二：任务跑完后 /api/log/info 报不出文件名 ——
S.LOG_DIR = os.path.join(_tdir, 'logs')
S._LOG_NAME = ''
S._LAST_LOG_NAME = ''
S.start_run_log()
_name_during = S._LOG_NAME
S.append_log('一行测试日志')
check(_name_during.startswith('run_') and _name_during.endswith('.log'),
      '运行中能报出日志文件名')
S.close_run_log()
check(S._LOG_NAME == '', 'close 之后当前文件名归零（这是正常的）')
check(S._LAST_LOG_NAME == _name_during,
      '但最近一次的文件名被记住了（任务结束后界面仍能显示文件名）')
check(os.path.isfile(os.path.join(S.LOG_DIR, _name_during)),
      'run_ 日志文件确实落盘了')
check(os.path.isfile(os.path.join(S.LOG_DIR, 'latest.log')),
      'latest.log 也生成了')
# 顺带：latest.log 现在也要有运行头（原来只有 run_*.log 有）
_latest = open(os.path.join(S.LOG_DIR, 'latest.log'), encoding='utf-8').read()
check('运行开始' in _latest, 'latest.log 也带运行头（不再只有 run_ 文件有）')

# —— 洞三：preflight_refresh 把 config 里的定时字符串直接解包 ——
# 'h, m, s = schedule' 遇到字符串 '07:59:58' 会按字符解包成 8 份 → ValueError。
# 预览 / 测速 / 有效性检测 每次都白挨一行「[刷新阶段异常]」。
check(S.parse_schedule('07:59:58') == (7, 59, 58), 'parse_schedule 认得 HH:MM:SS')
check(S.scheduled_target('07:59:58') is not None,
      'scheduled_target 吃字符串不再抛 ValueError')
check(S.scheduled_target((7, 59, 58)) is not None,
      'scheduled_target 吃元组照旧')
check(S.scheduled_target(None) is None, '没定时 → None（不进刷新阶段）')
check(S.scheduled_target('乱码') is None, '定时格式不对 → None，不当定时处理')
S._LOG_NAME = ''
S._LAST_LOG_NAME = ''
try:
    import shutil
    shutil.rmtree(_tdir, ignore_errors=True)
except Exception:
    pass

print('\n[14] 账号四卡在 #/user 那个 bug（token 固定 + 按值排除的冲突）')
import browserlogin as BL  # noqa: E402

# 该平台 token = HS512 + payload 只有 login_user_key，没有 exp/时间戳/随机数，
# 所以同一个账号每次登录拿回来的串**一模一样**。
# 于是「按值排除旧 token」会永远把新 token 当成旧的丢掉 → 停在 #/user 干等。
#
# 【2026-09-24 复测更正】「串恒定」这个结论本身是**那个 bug 的假象**：
# 当时拿到的根本不是新签发的 token，而是 localStorage 里上一轮留下的旧值，
# 当然和上次一样。实地观察确认新 token 与旧串**并不相同**。
# 下面的防呆逻辑仍然保留（真恒定的平台也适用），但别再拿它解释「刷新成功却 401」。
D = BL._storage_token_decision
check(D('', 'OLD', True) == (False, ''), '没扫到就不收')
check(D('NEW', 'OLD', False) == (True, ''), '值不同 → 收（不管有没有确认登录）')
check(D('OLD', 'OLD', False) == (False, 'stale'),
      '值相同 + 本轮还没登录 → 不收（挡住档案里的旧值）')
check(D('OLD', 'OLD', True) == (True, 'same'),
      '值相同 + 本轮已登录 → 收（这才是对的，否则永远拿不到 token）')

# 兜底条件必须覆盖 hdscw（H5 前端）—— 实际卡住的就是 hdscw/#/user，
# 只认 hdscs 的话兜底永远不触发。
try:
    _src = inspect.getsource(BL.wait_for_token)
    check('hdscw.hainanu.edu.cn' in _src,
          '「卡在应用页」的兜底覆盖 hdscw（实际卡住的就是它）')
    check('hdscs.hainanu.edu.cn' in _src, '兜底仍覆盖 hdscs（老的那条）')
    check('trust_storage' in inspect.getsource(BL._persistent_login),
          '_persistent_login 会传 trust_storage')
except Exception as e:
    check(False, '读不到 browserlogin 源码：%s' % e)

print('\n[15] 账号四「刷新完还是 401」——新 token 藏在 URL 的 hash fragment 里')

# 实地观察（2026-09-24）：密码提交后 0.8s 浏览器落在
#   https://hdscw.hainanu.edu.cn/#/login?redirect=L3VzZXI&szblTK=<新token>
# 0.4 秒后 SPA 就把地址改成 #/user 了。
# fragment（# 后面）**不上网** → 网络层 request/response 钩子永远看不到它；
# 而 Python 侧 0.6 秒一轮的 page.url 轮询去撞一个 0.4 秒的窗口，撞不上就
# 只能去读 localStorage —— 那儿躺着的是上一轮的旧值 → 校验 401。
# 修法：浏览器端注入脚本实时盯着 hash 变化，Python 再来取。

_js = BL.JS_TK_CAPTURE
for _need, _why in (
        ('hashchange', '监听 hashchange（#/login 那一跳是 hash 路由）'),
        ('pushState', '拦 pushState（SPA 换路由的另一条路）'),
        ('replaceState', '拦 replaceState'),
        ('popstate', '监听 popstate'),
        ('__TKCAP', '结果落在 window.__TKCAP 上'),
        ('szblTK=', '扫的是 szblTK='),
):
    check(_need in _js, '页面内捕获脚本包含 %s：%s' % (_need, _why))
check('1401' in _js and '1402' in _js,
      '捕获脚本认得 1401/1402 这两个平台错误码（不当 token 收）')

# 纯函数行为：读到就返回，并顺手填进 bucket（让「网络层已拿到」的判断成立）
class _FakePage(object):
    def __init__(self, d):
        self._d = d

    def evaluate(self, *a, **k):
        return self._d

_b1 = {'token': None, 'err_code': None}
check(BL._read_captured_token(_FakePage({'tok': 'TK_NEW'}), _b1) == 'TK_NEW',
      '_read_captured_token 能取回页面里捕获的 token')
check(_b1['token'] == 'TK_NEW', '取回时同步写进 bucket（上层判断才成立）')
_b2 = {'token': 'FROM_NET', 'err_code': None}
check(BL._read_captured_token(_FakePage({'tok': None}), _b2) is None,
      '页面里没有 token 时返回 None')
check(_b2['token'] == 'FROM_NET', '不会把已有的网络层 token 冲掉')
_b3 = {'token': None, 'err_code': None}
BL._read_captured_token(_FakePage({'tok': None, 'bad': '1401'}), _b3)
check(_b3['err_code'] == '1401', '页面里看到 1401/1402 → 记为平台错误码')
try:
    check(BL._read_captured_token(_FakePage(None), None) is None,
          '页面返回异常结构时不炸（返回 None）')
except Exception as e:
    check(False, '_read_captured_token 遇到异常结构炸了：%s' % e)

# localStorage 兜底必须「慢半拍」：刚进应用域的头 2 秒里，
# localStorage 里大概率还是上一轮的旧值，不能采信。
check(isinstance(getattr(BL, 'STORAGE_GRACE_SEC', None), float)
      and BL.STORAGE_GRACE_SEC >= 1.0,
      'STORAGE_GRACE_SEC 已设且不小于 1 秒（给 SPA 落盘留时间）')
_src_w = inspect.getsource(BL.wait_for_token)
_i_cap = _src_w.find('从页面路由捕获到 token')
_i_ls = _src_w.find('从 localStorage 捕获到 token')
check(_i_cap > 0, 'wait_for_token 会读页面内捕获的 token')
check(_i_cap < _i_ls, '页面内捕获排在 localStorage 之前（先信新签发的）')
check('STORAGE_GRACE_SEC' in _src_w, 'wait_for_token 用了 localStorage 宽限期')
check('hdscs_since' in _src_w, '进入应用域的计时仍在（宽限期的依据）')
_src_p = inspect.getsource(BL._persistent_login)
check('_install_token_capture' in _src_p, '_persistent_login 装了页面内捕获钩子')
check('_install_token_capture' in inspect.getsource(BL.browser_mfa),
      'browser_mfa 也装了（MFA 通道同样会跳 #/login?szblTK=）')
check('_read_captured_token' in inspect.getsource(BL._wait_before_fill),
      '_wait_before_fill 也读页面内捕获（免验证直通同样走 hash 跳转）')
# 日志里不能再把 fragment 砍掉 —— 否则下次排查还是看不见 token 出现在哪一跳。
# 注意只查 `url.split("?")` 这种真代码：上面解释原因的注释里也提到了这个写法，
# 按裸字符串查会把注释当成未修复，报假警。
check('url.split("?")' not in _src_w and "url.split('?')" not in _src_w,
      'URL 流水日志不再把 ? 后面（含 fragment）一刀切掉')

print('\n[16] 抢单主循环干跑（假网络 · 绝不成交）——2026-09-25 早上的事故')
import datetime as dt_cls

# 那天早上五个账号里四个崩在 `TypeError: float - NoneType`：
# 「第一枪打点」要用开火时刻 t_fire，可有人（就是上一轮改这段代码时的我）
# 在上面刚取好值、12 行后又写了一句 `t_fire = None` 把它冲掉了。
# 崩在第一发提交之后 —— 于是后面「未开放就原地继续锤」的整套提速逻辑
# 全都没机会执行，账号直接报废。
#
# 昨天的 E4 验收**完全没抓到**：预览模式不提交订单，根本走不到打点那一行。
# 所以这里用假网络真跑一遍提交路径（requests 换成一碰就抛的哨兵）。
_src_b = inspect.getsource(b.run_booking)
_bad_lines = [ln.strip() for ln in _src_b.splitlines()
              if ln.strip().startswith('t_fire') and '= None' in ln]
check(not _bad_lines,
      'run_booking 里不再出现「t_fire = None」（否则打点会把抢单线程拖崩）',
      str(_bad_lines))
check('t_fire = time.perf_counter()' in _src_b,
      '开火时刻确实有在抢单开始前取过一次')


class _NoNet(object):
    """谁敢在自检里发真实请求就炸谁 —— 保证这一段永远不可能成交。"""

    def __getattr__(self, name):
        raise RuntimeError('自检期间禁止真实网络调用（requests.%s）' % name)


def _dryrun_booking():
    """用假网络跑一遍 run_booking 的提交路径。返回 (异常, result, 提交次数)。"""
    real_requests = b.requests
    calls = {'n': 0}
    b.requests = _NoNet()
    try:
        b.build_session = lambda *a, **k: object()
        b.api_places = lambda *a, **k: list(b.COURTS_FALLBACK)
        b.api_getday = lambda *a, **k: ({}, 200, {'code': 200})
        b.scout = lambda *a, **k: ({}, {})
        b.measure_clock_offset = lambda *a, **k: 0.0
        b.api_my_orders = lambda *a, **k: ([], None)

        def _fake_submit(*a, **k):
            calls['n'] += 1
            return ('not_open_yet', 500,
                    {'msg': '下单失败,当前时间未开放预约', 'code': 500})

        b.api_submit = _fake_submit

        def _fake_candidates(*a, **k):
            return [{'court': c['name'], 'slot': '18:00-19:00', 'sku': c['sku']}
                    for c in b.COURTS_FALLBACK[:3]]

        b.make_candidates = _fake_candidates

        base = tempfile.mkdtemp(prefix='_selftest_dryrun_')
        d = (dt_cls.date.today() + dt_cls.timedelta(days=2)).isoformat()
        params = {
            'token': 'eyJhbGciOiJIUzUxMiJ9.ZmFrZQ.ZmFrZQ',   # 假 token
            'target_date': d, 'window_start': '18:00', 'window_end': '19:00',
            'court_order': [c['name'] for c in b.COURTS_FALLBACK],
            'court_enabled': {c['name']: True for c in b.COURTS_FALLBACK},
            'submit': True, 'schedule': None,
            'min_interval': 0.01, 'jitter_ms': 0, 'burst': 1,
            'not_open_max_retries': 3, 'unknown_max_retries': 1,
            'refresh_rounds': 0, 'scout_workers': 2, 'parallel_first': 2,
            'max_days_ahead': 3, 'one_per_day': True, 'base_dir': base,
        }
        logs = []
        t_end = time.time() + 20
        res = b.run_booking(params, log=logs.append,
                            stop=lambda: time.time() > t_end)
        return None, res, calls['n']
    except Exception as e:
        return '%s: %s' % (type(e).__name__, e), None, calls['n']
    finally:
        b.requests = real_requests


_dry_err, _dry_res, _dry_n = _dryrun_booking()
check(_dry_err is None,
      '干跑一遍抢单主循环不抛异常（早上崩的就是这儿）', _dry_err or '')
check(_dry_n > 0, '干跑确实走到了提交路径', '假提交 %d 次' % _dry_n)
if isinstance(_dry_res, dict):
    _fs = _dry_res.get('first_shot')
    check(isinstance(_fs, dict), '第一枪打点产生了记录', repr(_fs)[:100])
    if isinstance(_fs, dict):
        check(isinstance(_fs.get('lag_ms'), (int, float)),
              '第一枪「距开火多少毫秒」是个数字，不是 None',
              'lag_ms=%r' % _fs.get('lag_ms'))
    check(_dry_res.get('success') is not True, '干跑不可能成交（请求全是假的）')

print('\n' + '=' * 50)
if FAIL:
    print(f'✗ {len(FAIL)} 项未通过：')
    for f in FAIL:
        print('   -', f)
    sys.exit(1)
print('✓ 全部通过')
