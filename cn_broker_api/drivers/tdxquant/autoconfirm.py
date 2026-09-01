r"""给通达信的「智赢交易信号」页面打自动确认补丁（装 / 卸 / 自检）。

## 这一整份是照着平安证券的实现写的

二次确认由**券商**决定，不是通达信的本地选项（证据见下）。所以这里一切具体的东西——页面
`webs/cfg/aireq.html`、选择器 `.ai-req`、宿主方法 `GetAutoJyReqList` / `sendautojy`、
行上的 `ZH` / `STATE` / `REQ_ID` / `TIME`——都是从平安下发的那份 HTML 上读出来的，换一家
没有任何理由仍然成立。三种可能，处置各不相同：那家不做二次确认（不需要补丁，`order_stock`
直接回 2）／做但页面字段不同（机制照抄，选择器与列名必须重新认）／做且不给自动化的口子
（只能人工点或换客户端）。不实现这个能力也能用：补丁不在时服务拒绝受理（503），
委托不会静默躺在队列里等人点。

## 为什么只能这么做

实盘账户上 `order_stock` 回三态里的 `1`「已发送信号至客户端，待用户确认！」，委托进到客户端
一个队列窗口（顶层 `#32770`、标题 `交易信号`，内容是 CEF 里的 `aireq.html`），要人点【发送】
才真报到柜台。2026-08-20 把「能不能关掉」这条路走死了，五条证据：量化模块没有这个开关
（TPyth.dll 中文串全列过，那三个是策略列表的筛选下拉）；信号队列没有设置页；菜单与配置里
没有入口；`GetAutoJyReqList` / `sendautojy` 在整个安装目录 4322 个文件里**只出现在那个 HTML
里**，宿主侧一个字符串都没有 ⇒ 这套是券商下发的；厂商原话是「实盘交易账户的自动下单请联系
你的开户券商」。⇒ 按账户类别 + 券商授权决定，本地无解，于是退一步让那个页面自己点自己的按钮。

## 补丁形态

不动那个 minified 的 Vue 包，只在 `</body>` 前插一段带标记的脚本，靠
`document.querySelector(".ai-req").__vue__` 拿到组件实例，调它自己的 `send()`（与人点按钮
同一个函数）。配置放在旁挂文件 `tq_autosend.js`，加载不到就什么都不做（fail closed）——
删掉它、或客户端升级把 HTML 冲掉，都是自动失效回到手工确认，不存在"以为关了其实还在发"。
页脚有可见徽标（开/关、演练/生产）。`mode="cancel"` 是演练档：命中时调 `cancel()`，
用来验"脚本加载→拿到实例→规则命中→宿主命令执行"整条链，且一分钱都不会报出去。

## 五道闸（必须同时成立才动手）+ 幂等

| 闸 | 挡什么 |
| --- | --- |
| `account` 匹配 `ZH` | 别的账户的信号 |
| `STATE==="0"` | 已发送/已取消的历史行 |
| 行龄 ≤ `maxAgeSec` | 早上没人确认的陈旧信号在下午被顺手发出去（最危险的一格） |
| 落在 `hours` 里 | 盘后的探针单、误触 |
| 限价 + 量/金额上限 | 我们只发限价单；出现市价单或超额就不是我们的 |

发过的 `REQ_ID` 按日记进 `localStorage`，页面重载也不会重发同一笔。**标记在调用
`send()` 之后才写**：写在排期时的话，中间任何一步落空都成了「记了账没干活」，那一笔从此
每轮被跳过、永久卡在队列里等人点（2026-09-01 一笔卖券还款就是这么漏的，融资负债差点过夜）。

## 认行只认 `REQ_ID`

页面的 `load()` 每 `pollMs` 把 `list` 整个换成**一批新对象**，所以任何跨过一次 `load` 的
行引用都是废的——排期与真正发送之间隔着 100ms 的错开，必然跨得过去。`remove(t, e)` 则是
按**索引**给 `list[e]` 打 `ACTED` 标记（不是删行，早先这里的注释写反了），索引错位会让
另一行的按钮消失、那一笔人也点不了 ⇒ 行与索引必须是同一次查出来的。

行龄闸一过，自动这侧就再也不碰那一笔了（这条闸不能为了重试放宽）。所以「该发而没发出去」
的笔数直接从当前 `list` 算出来显示在页脚徽标上，**当场**看得见、还来得及手工点。

用法（工作目录＝仓库根；客户端目录从配置的 `driver.tdxquant.tdx_home` 读）：
  .venv\Scripts\python -m cn_broker_api.drivers.tdxquant.autoconfirm --status
  ... --apply --mode cancel                     # 演练档（默认）：点【取消】，一笔单都不报
  ... --apply --mode send --account 资金账号     # 生产档
  ... --remove
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from cn_broker_api.drivers.tdxquant.health import tdx_install_root
from cn_broker_api.stdio import init_stdio

#: 客户端安装目录。 **不在这里第二次写死机器路径**：2026-08-21 客户端目录改了个名
#: （去掉中文），当时这份副本和后端那份各要改一次——漏掉这一份的表现是"自检说没打补丁"
#: 这种假警报。现在唯一的出处是 `health.tdx_install_root()`（由入口按配置注入一次）。
#: 路径**必须惰性求值**：import 期取值的话，配置还没注入就先炸了，而本模块在
#: 「没配安装目录」时应当只在真去改文件那一刻失败。


def _cfg_dir() -> Path:
    return tdx_install_root() / "webs" / "cfg"


def page_path() -> Path:
    return _cfg_dir() / "aireq.html"


def sidecar_path() -> Path:
    return _cfg_dir() / "tq_autosend.js"


def backup_path() -> Path:
    return _cfg_dir() / "aireq.html.orig"

MARK_BEGIN = "<!-- TQ-AUTOSEND-BEGIN v1 QuantTradeDemo -->"
MARK_END = "<!-- TQ-AUTOSEND-END -->"

#: 注入的脚本。**不做任何判断**，判断全在旁挂配置里；配置缺失就是关。
INJECT = MARK_BEGIN + """
<script src="tq_autosend.js"></script>
<script>
(function () {
  var C = window.__TQ_AUTOSEND;
  if (!C || !C.enabled) { return; }                 // 配置缺失/关闭 ⇒ 什么都不做
  var MODE = C.mode === "cancel" ? "cancel" : "send";

  function app() {                                   // Vue 2 把实例挂在根元素上
    var e = document.querySelector(".ai-req");
    return e && e.__vue__ ? e.__vue__ : null;
  }
  function nowSec() {
    var d = new Date();
    return d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds();
  }
  function hms2sec(s) {
    var m = /^(\\d{1,2}):(\\d{2}):(\\d{2})$/.exec(String(s || "").trim());
    return m ? (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]) : null;
  }
  function inHours() {
    var t = nowSec(), hs = C.hours || [];
    for (var i = 0; i < hs.length; i++) {
      var a = hms2sec(hs[i][0] + ":00"), b = hms2sec(hs[i][1] + ":00");
      if (a !== null && b !== null && t >= a && t <= b) { return true; }
    }
    return false;
  }
  var KEY = "tqAutoSent_" + new Date().toISOString().slice(0, 10);
  function done() {
    try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (e) { return []; }
  }
  function mark(id) {
    var a = done();
    if (a.indexOf(id) < 0) { a.push(id); localStorage.setItem(KEY, JSON.stringify(a)); }
  }

  var acted = 0, missed = 0, pending = {};

  //  一笔一笔**错开**发，不在同一轮里连着调 `send()`。
  //
  // `send()` 不是普通的函数调用，它靠 `location.href = ".../sendautojy" + REQ_ID` 发起一次
  // 导航来通知宿主。同步循环里连赋 N 次值，浏览器只处理最后一次、前面的全被取消 ⇒ 十笔
  // 只有一两笔真到柜台，其余留在队列里等人点【全部发送】。页面自己的 `sendAll()` 就是
  // 用 `setTimeout(..., e)`、`e += 100` 错开的，这里照搬它那个 100ms。
  //
  // 🔴 **不直接调 `sendAll()`**：它无差别发送所有 `STATE==="0"` 的行，五道闸一道都不过——
  // 别的账户的信号、市价单、超额单，尤其是行龄超限的陈旧信号（队列形态里最危险的一格）。
  // 借它的手法，不借它的语义。
  var SEND_GAP_MS = 100;

  function rowIndexById(v, id) {
    for (var i = 0; i < v.list.length; i++) {
      if (String(v.list[i].REQ_ID) === id) { return i; }
    }
    return -1;
  }

  function fire(id) {
    delete pending[id];
    var v = app();
    if (!v || !v.list) { return; }
    // ⭐ 按 REQ_ID 重新认行，**不能认排期时抓到的那个对象**：页面的 `load()` 每 `pollMs`
    // 把 list 整个换成一批新对象（源码是 `var o=[]; r.map(...o.push(新对象)); this.list=o`），
    // 100ms 之后那个旧引用多半已经不在 list 里了。2026-09-01 漏掉一笔卖券还款就是这么来的：
    // 一次 tick 排四笔跨 300ms，load 的回调落在中间，落在它之后的那笔 `indexOf` 返回 -1、
    // 静默不发，而幂等标记当时写在排期处 ⇒ 后面每一轮都跳过它，融资负债差点过夜。
    var i = rowIndexById(v, id);
    if (i < 0) { return; }              // 这一轮认不到就不发；没记幂等，下一轮 tick 会重排
    var row = v.list[i];
    if (String(row.STATE) !== "0") { return; }            // 已被别处发掉/撤掉就不动它
    // 索引与行必须是同一次查出来的：页面的 `remove(t, e)` 按索引给 `list[e]` 打 ACTED，
    // 错位会让另一行的【发送】【取消】按钮消失，那一笔既发不出去、人也点不了。
    if (MODE === "cancel") { v.cancel(row, i, 0); } else { v.send(row, i, 0); }
    // ⭐ 幂等标记写在**调用之后**。写在排期时的话，中间任何一步落空都成了「记了账没干活」，
    // 且永不重试。放在这里的代价是同一笔可能被下一轮 tick 重排一次——不会变成重复报单：
    // 真发出去的那笔柜台会把 STATE 改成 "1"，上面那行挡得住；而排期期间的空窗由 `pending`
    // 挡（它只活在本页面会话里，重载后靠 localStorage 那份接着挡）。
    mark(id);
    acted++;
  }

  function tick() {
    var v = app();
    if (!v || !v.list || !v.list.length) { return; }
    if (!inHours()) { return; }                       // 盘后一律不动手
    var sent = done(), delay = 1, stale = 0;
    v.list.forEach(function (r) {
      if (String(r.STATE) !== "0") { return; }                       // 只碰未发送
      if (C.account && String(r.ZH) !== String(C.account)) { return; }
      var vol = Number(r.VOL) || 0;
      if (!vol || (C.maxVol && vol > C.maxVol)) { return; }
      var px = Number(r.PRICE);                                       // 市价单这里是 NaN
      if (!(px > 0)) { return; }                                      // 我们只发限价单
      if (C.maxNotional && vol * px > C.maxNotional) { return; }
      var t = hms2sec(r.TIME), age = t === null ? -1 : nowSec() - t;
      if (age < 0) { return; }
      if (age > (C.maxAgeSec || 90)) {
        // 该发、没发出去、行龄闸又已经过了 ⇒ 自动这侧再也不会碰它（这条闸不能为了重试
        // 放宽，早上的陈旧信号在下午被顺手发出去是队列形态里最危险的一格）。只能让人看见。
        stale++;
        return;
      }
      var id = String(r.REQ_ID);
      if (sent.indexOf(id) >= 0 || pending[id]) { return; }           // 幂等：发过/排着不再排
      pending[id] = true;
      setTimeout((function (x) { return function () { fire(x); }; })(id), delay);
      delay += SEND_GAP_MS;
    });
    missed = stale;   // 从 list 现状重算，不累加——计数器扛不住页面重载，这个读数扛得住
  }

  function badge() {
    //  徽标必须挂在 body 上做固定层，**不能动页面自己的元素**：第一版给 .footer 设了
    // position:relative 再往里塞 div，跟它自己的 CSS 打架，页脚跑到顶上、表头整行消失
    // （2026-08-20 演练时实测）。观测件不许改被观测对象的布局。
    var el = document.getElementById("tq-autosend-badge");
    if (!el) {
      el = document.createElement("div");
      el.id = "tq-autosend-badge";
      el.style.cssText = "position:fixed;left:8px;bottom:1px;font-size:11px;"
        + "z-index:9999;pointer-events:none;background:transparent;";
      document.body.appendChild(el);
    }
    // 漏笔是这段代码唯一会静默造成损失的失败形态（2026-08-28、2026-09-01 各发生过一次，
    // 两次都是事后翻 localStorage 才发现的）⇒ 它必须在**当场**看得见，还来得及手工点。
    el.style.color = missed > 0 ? "#e05c5c"
      : (MODE === "cancel" ? "#e8a33d" : "#4bbf73");
    el.textContent = "自动" + (MODE === "cancel" ? "取消(演练)" : "确认")
      + " 开 · " + (inHours() ? "交易时段" : "非交易时段只观察")
      + " · 已发" + acted
      + (missed > 0 ? " · 漏 " + missed + " 笔，请手工点【发送】" : "");
  }

  var iv = C.pollMs || 2000;
  setInterval(function () { try { var v = app(); if (v) { v.load(); } } catch (e) {} }, iv);
  setInterval(function () { try { tick(); badge(); } catch (e) {} }, Math.max(500, iv / 2));
  setTimeout(function () { try { tick(); badge(); } catch (e) {} }, 400);
})();
</script>
""" + MARK_END


def sidecar_js(cfg: dict) -> str:
    return ("// 通达信「交易信号」自动确认的配置。由 tdx_autoconfirm_patch.py 生成。\n"
            "// 删掉本文件 ⇒ 自动确认立即失效（fail closed），回到人工点【发送】。\n"
            "window.__TQ_AUTOSEND = " + json.dumps(cfg, ensure_ascii=False, indent=2) + ";\n")


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def write(p: Path, s: str) -> None:
    p.write_text(s, encoding="utf-8", newline="")


def strip_patch(html: str) -> str:
    """把已有的补丁块去掉（幂等重装用）。"""
    return re.sub(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END), "", html,
                  flags=re.S)


def cmd_status() -> int:
    if not page_path().exists():
        print(f"[!] 找不到页面 {page_path()}")
        return 1
    html = read(page_path())
    on = MARK_BEGIN in html
    print(f"页面   {page_path()}")
    print(f"       补丁 {'[ok] 在' if on else '[!] 不在'}，"
          f"sha1={hashlib.sha1(html.encode('utf-8')).hexdigest()[:12]}，"
          f"{len(html)} 字节")
    print(f"备份   {backup_path()}  {'[ok]' if backup_path().exists() else '[!] 无'}")
    if sidecar_path().exists():
        txt = read(sidecar_path())
        m = re.search(r"window\.__TQ_AUTOSEND\s*=\s*(\{.*?\});", txt, re.S)
        cfg = json.loads(m.group(1)) if m else {}
        print(f"配置   {sidecar_path()}\n       {json.dumps(cfg, ensure_ascii=False)}")
        eff = on and bool(cfg.get("enabled"))
        print(f"\n生效状态：{'[ok] 开（' + str(cfg.get('mode')) + ' 档）' if eff else '关'}")
    else:
        print(f"配置   {sidecar_path()}  [!] 无 ⇒ 即使补丁在，也是关的（fail closed）")
        print("\n生效状态：关")
    return 0


def cmd_apply(args) -> int:
    if not page_path().exists():
        print(f"[!] 找不到页面 {page_path()}")
        return 1
    html = read(page_path())
    if not backup_path().exists():
        if MARK_BEGIN in html:
            print("[!] 页面已带补丁但没有备份，拒绝继续（先手工确认原文）")
            return 2
        write(backup_path(), html)
        print(f"已备份原文 → {backup_path()}")
    clean = strip_patch(html)
    if clean.count("</body>") != 1:
        print(f"[!] </body> 出现 {clean.count('</body>')} 次，拒绝改")
        return 2
    out = clean.replace("</body>", INJECT + "</body>")
    assert out.count(MARK_BEGIN) == 1, "补丁块必须恰好一份"
    write(page_path(), out)
    cfg = {
        "enabled": True,
        "mode": args.mode,
        "account": args.account,
        "maxAgeSec": args.max_age,
        "maxVol": args.max_vol,
        "maxNotional": args.max_notional,
        "hours": [["09:15", "11:35"], ["12:55", "15:05"]] if not args.anytime
                 else [["00:00", "23:59"]],
        "pollMs": 2000,
    }
    write(sidecar_path(), sidecar_js(cfg))
    print(f"[ok] 补丁已装：{page_path()}")
    print(f"[ok] 配置已写：{sidecar_path()}")
    print(f"   {json.dumps(cfg, ensure_ascii=False)}")
    if args.mode == "cancel":
        print("\n[!] 现在是演练档：命中规则会点【取消】，不会报单。验完记得改成 --mode send")
    if args.anytime:
        print("[!] 交易时段闸被放开成全天（只该在演练时用）")
    print("\n下一步：让「交易信号」窗口重新弹一次（发一笔委托），页脚出现徽标即为加载成功。")
    return 0


def cmd_remove() -> int:
    rc = 0
    if page_path().exists():
        html = read(page_path())
        if MARK_BEGIN in html:
            write(page_path(), strip_patch(html))
            print(f"[ok] 补丁已从 {page_path()} 移除")
        else:
            print("页面本来就没有补丁")
        if backup_path().exists():
            a, b = read(page_path()), read(backup_path())
            print("与备份逐字节一致" if a == b else "[!] 与备份不一致（客户端可能升级过页面）")
    else:
        print(f"[!] 找不到 {page_path()}")
        rc = 1
    if sidecar_path().exists():
        sidecar_path().unlink()
        print(f"[ok] 配置已删 {sidecar_path()}")
    return rc


def _load_tdx_home() -> bool:
    """独立跑这个命令时，客户端安装目录得自己从配置读一次。

     读的是**服务用的那一份配置**（`CN_BROKER_API_CONFIG` / 默认位置），不是再开一个参数：
    多一个 `--tdx-home` 就等于多一处可以和配置分叉的地方，而分叉的表现是"自检说没打补丁"
    这种假警报（2026-08-21 客户端目录改名时踩过一次，当时两份副本各要改一次）。
    """
    from cn_broker_api.config import ConfigError, load
    from cn_broker_api.drivers.tdxquant.health import set_tdx_home

    try:
        cfg = load()
    except ConfigError as e:
        print(f"读配置失败：{e}", file=sys.stderr)
        return False
    if not cfg.tdxquant.tdx_home:
        print("配置里没写 driver.tdxquant.tdx_home ⇒ 不知道客户端装在哪，"
              "无从找到要打补丁的页面", file=sys.stderr)
        return False
    set_tdx_home(cfg.tdxquant.tdx_home)
    return True


def main() -> int:
    init_stdio()
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true", help="看补丁与配置现在什么状态")
    g.add_argument("--apply", action="store_true", help="装/重装补丁（幂等）")
    g.add_argument("--remove", action="store_true", help="卸掉补丁并删配置")
    ap.add_argument("--mode", choices=("send", "cancel"), default="cancel",
                    help="send=生产（点发送）/ cancel=演练（点取消，不报单）。默认演练")
    # 不给默认值：原先写着一个真实资金账号，本仓库要公开就成了泄露。空串＝不限账号。
    ap.add_argument("--account", default="", help="只对这个资金账号的信号动手（空＝不限）")
    ap.add_argument("--max-age", type=int, default=90, help="行龄上限（秒）")
    ap.add_argument("--max-vol", type=int, default=100000, help="单笔股数上限")
    ap.add_argument("--max-notional", type=float, default=150000.0, help="单笔金额上限")
    ap.add_argument("--anytime", action="store_true",
                    help="放开交易时段闸（只给演练用，生产别加）")
    args = ap.parse_args()
    if not _load_tdx_home():
        return 2
    if args.status:
        return cmd_status()
    if args.apply:
        return cmd_apply(args)
    return cmd_remove()


if __name__ == "__main__":
    raise SystemExit(main())
