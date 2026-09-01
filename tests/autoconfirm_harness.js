// 在 node 里跑真正要注入客户端的那段自动确认脚本，验它在页面的真实语义下不漏笔。
//
// 被测代码不是副本：Python 侧从 `autoconfirm.INJECT` 里剥出 <script> 正文写进临时文件，
// 这里 require 进来跑 ⇒ 改了注入脚本而没改这里，测试立刻说话。
//
// 🔴 台架的价值全在**假件够不够像**这一点上。这三条是从 aireq.html.orig 的 minified 源码
// 里读出来的真实语义，缺任何一条，坏代码和好代码都会满分：
//   1. `load()` 把 list 整个换成**一批新对象**（源码：var o=[]; r.map(...o.push(新对象)); this.list=o）
//      ⇒ 排期时抓到的行引用，100ms 后极可能已经不在 list 里。2026-09-01 漏掉一笔卖券还款
//      就是这么来的。
//   2. `send(t, e)` 认的是 `t.REQ_ID`（location.href 拼的是它），而 `remove(t, e)` 按**索引 e**
//      给 `list[e]` 打 ACTED ⇒ 索引与行必须对得上，错了会让另一行的按钮消失、人也点不了。
//   3. 发出去之后柜台把那行的 STATE 由 "0" 改成 "1"，下一次 load 才看得到。这中间的空窗
//      正是幂等标记要挡的东西。
'use strict';
const fs = require('fs');
const vm = require('vm');

const scriptPath = process.argv[2];
const scenario = process.argv[3];

function hms(offsetSec) {
  const d = new Date(Date.now() - offsetSec * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 一行队列信号。`ageSec` ＝ 这笔是多久以前进队列的。 */
function row(reqId, extra) {
  return Object.assign({
    ID: reqId, ZH: '327299901850', CODE: '301004', NAME: '嘉益股份',
    BS: '1', VOL: '500', REQ_ID: String(reqId), STATE: '0',
    DAY: '20260901', TIME: hms(0), PRICE: '35.74',
  }, extra || {});
}

/** 假的 Vue 实例，照 aireq.html 的真实语义办事。 */
function makeVm(rows) {
  const backend = {};                       // REQ_ID -> STATE，柜台那侧的真相
  rows.forEach((r) => { backend[r.REQ_ID] = r.STATE; });
  const seen = [];                          // 每次 send 的取证记录
  const canceled = [];
  const misindexed = [];                    // send 收到的索引没指向那一行的次数

  const build = () => rows.map((r) => Object.assign({}, r, { STATE: backend[r.REQ_ID] }));

  const self = {
    list: build(),
    send(t, e, n) {
      if (self.list[e] !== t) { misindexed.push({ reqId: String(t.REQ_ID), i: e }); }
      seen.push(String(t.REQ_ID));
      backend[String(t.REQ_ID)] = '1';      // 柜台受理，下一次 load 才看得到
      self.remove(t, e);
    },
    cancel(t, e, n) {
      if (self.list[e] !== t) { misindexed.push({ reqId: String(t.REQ_ID), i: e }); }
      canceled.push(String(t.REQ_ID));
      backend[String(t.REQ_ID)] = '2';
      self.remove(t, e);
    },
    remove(t, e) { if (self.list[e]) { self.list[e].ACTED = true; } },
    load() { self.list = build(); },        // ⬅️ 每次都是全新对象，见文件头第 1 条
    _seen: seen, _canceled: canceled, _misindexed: misindexed,
  };
  return self;
}

const SCENARIOS = {
  // 四笔在同一秒进队列（14:57 那批卖券还款的形状），load 一直在中间重建 list。
  batch_with_reloads: {
    rows: [row(5, { CODE: '300882', VOL: '1400', PRICE: '14.28' }),
           row(6, { CODE: '301004', VOL: '500', PRICE: '35.74' }),
           row(7, { CODE: '600826', VOL: '2700', PRICE: '7.69' }),
           row(8, { CODE: '601158', VOL: '4800', PRICE: '4.09' })],
    runMs: 1200,
    expectSent: ['5', '6', '7', '8'],
  },
  // 五道闸：只有第一行合规。
  gates: {
    rows: [row(1),
           row(2, { ZH: '999999999999' }),                     // 别的账户
           row(3, { PRICE: '' }),                              // 市价单
           row(4, { VOL: '200000' }),                          // 超股数上限
           row(5, { VOL: '9000', PRICE: '35.74' }),            // 超金额上限
           row(6, { TIME: hms(600) }),                         // 陈旧信号
           row(7, { STATE: '1' })],                            // 已发送的历史行
    runMs: 800,
    expectSent: ['1'],
  },
};

const conf = SCENARIOS[scenario];
if (!conf) { throw new Error(`没有这个场景：${scenario}`); }

const app = makeVm(conf.rows);
const store = {};
const badge = { id: '', style: {}, textContent: '' };
const sandbox = {
  window: {
    __TQ_AUTOSEND: {
      enabled: true, mode: 'send', account: '327299901850',
      maxAgeSec: 90, maxVol: 100000, maxNotional: 150000.0,
      hours: [['00:00', '23:59']], pollMs: 40,     // 让 load 密集地插进排期中间
    },
  },
  document: {
    querySelector: () => ({ __vue__: app }),
    getElementById: (id) => (badge.id === id ? badge : null),
    createElement: () => badge,
    body: { appendChild: (el) => { badge.id = 'tq-autosend-badge'; return el; } },
  },
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
  },
  setInterval, setTimeout, clearInterval, Date, JSON, String, Number, Math, console,
};
sandbox.window.localStorage = sandbox.localStorage;

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(scriptPath, 'utf8'), sandbox);

setTimeout(() => {
  console.log(JSON.stringify({
    sent: app._seen,
    canceled: app._canceled,
    misindexed: app._misindexed,
    expectSent: conf.expectSent,
    badge: badge.textContent,
    marked: JSON.parse(store[Object.keys(store).find((k) => k.startsWith('tqAutoSent_')) || ''] || '[]'),
  }));
  process.exit(0);
}, conf.runMs);
