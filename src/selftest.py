# -*- coding: utf-8 -*-
"""离线自检（不联网）：规划 / 随机 / 判定 / 速率 / 配置。
运行： python selftest.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import booker as b
import accounts as A

FAIL = []


def check(cond, msg):
    if cond:
        print(f'  ✓ {msg}')
    else:
        print(f'  ✗ {msg}')
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
check(b.judge({'code': 500, 'msg': '日期超过可提前天数'}) == 'not_open', '超出提前天数 → not_open')
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

print('\n' + '=' * 50)
if FAIL:
    print(f'✗ {len(FAIL)} 项未通过：')
    for f in FAIL:
        print('   -', f)
    sys.exit(1)
print('✓ 全部通过')
