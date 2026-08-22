"""给通达信的「智赢交易信号」页面打自动确认补丁（装 / 卸 / 自检）。

## 为什么只能这么做

实盘账户上 `order_stock` 回的是三态里的 `1`「已发送信号至客户端，待用户确认！」，委托进到
客户端一个**队列窗口**（顶层 `#32770`、标题 `交易信号`，内容是 CEF 里的
`webs/cfg/aireq.html`，`<title>智赢交易信号</title>`），要人点【发送】才真报到柜台。

2026-08-20 把「能不能关掉」这条路走死了，五条证据：

  1. 量化模块没有这个开关——TPyth.dll 的中文字符串全列过，`实盘交易/模拟交易/交易信号`
     那三个是策略列表的**筛选下拉**（`rva 0xce40` 那个函数 `CB_ADDSTRING` 填进去的）；
  2. 信号队列**没有设置页**：`webs/cfg/` 下与它相关的只有 `aireq.html`；
  3. 菜单与配置里没有入口（`funcs/ funcs_jy/ *.xmb *.dat tdxmenu.html` 全扫过）；
  4. **`GetAutoJyReqList` / `sendautojy` 这些方法名在整个安装目录 4322 个文件里只出现在
     那个 HTML 里**——宿主侧一个字符串都没有 ⇒ 这套是券商下发/服务端派发的，不是本地选项；
  5. 厂商原话：「对于**实盘交易账户**是提示下单让用户确认」「实盘交易账户的自动下单请
     联系你的开户券商，开通并使用对应的支持 TQ 的版本」。

⇒ 按**账户类别 + 券商授权**决定，本地无解。于是退一步：让那个页面自己按规则点自己的按钮。

## 补丁形态

不动那个 minified 的 Vue 包，只在 `</body>` 前插一段带标记的脚本，靠
`document.querySelector(".ai-req").__vue__` 拿到组件实例，调**它自己的** `send()`
（与人点按钮同一个函数），并且：

  * 配置放在**旁挂文件** `tq_autosend.js`。加载不到 ⇒ 什么都不做（**fail closed**）：
    删掉它、或客户端升级把 HTML 冲掉，都是自动失效回到手工确认，不存在"以为关了其实还在发"；
  * 页脚显示可见徽标，当前是开还是关、是演练还是生产，看一眼就知道；
  * `mode="cancel"` 是**演练档**：规则命中时调 `cancel()` 而不是 `send()`，
    用来验"脚本加载→拿到实例→规则命中→宿主命令执行"整条链，且一分钱都不会报出去。

## 五道闸（必须同时成立才动手）

| 闸 | 挡什么 |
| --- | --- |
| `account` 匹配 `ZH` | 别的账户的信号 |
| `STATE==="0"` | 已发送/已取消的历史行 |
| 行龄 ≤ `maxAgeSec` | **早上没人确认的陈旧信号在下午被顺手一起发出去**（最危险的一格） |
| 落在 `hours` 里 | 盘后的探针单、误触 |
| 限价 + 量/金额上限 | 我们的路径只发限价单；出现市价单或超额就不是我们的 |

外加幂等：发过的 `REQ_ID` 按日记进 `localStorage`，页面重载也不会重发同一笔。

用法（工作目录＝项目根）：
  python -u .claude/skills/cn-data-ops/tdx_autoconfirm_patch.py --status
  python -u ... --apply --mode cancel      # 演练档
  python -u ... --apply --mode send        # 生产档
  python -u ... --remove
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from cn_broker_api.drivers.tdxquant.health import tdx_install_root

#: 客户端安装目录。⭐ **不在这里第二次写死机器路径**：2026-08-21 客户端目录改了个名
#: （去掉中文），当时这份副本和后端那份各要改一次——漏掉这一份的表现是"自检说没打补丁"
#: 这种假警报。现在唯一的出处是 `health.tdx_install_root()`（由入口按配置注入一次）。
#: ⚠️ 路径**必须惰性求值**：import 期取值的话，配置还没注入就先炸了，而本模块在
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

  var acted = 0, skipped = 0;
  function tick() {
    var v = app();
    if (!v || !v.list || !v.list.length) { return; }
    if (!inHours()) { return; }                       // 盘后一律不动手
    var sent = done();
    v.list.forEach(function (r, i) {
      if (String(r.STATE) !== "0") { return; }                       // 只碰未发送
      if (C.account && String(r.ZH) !== String(C.account)) { skipped++; return; }
      var age = nowSec() - (hms2sec(r.TIME) === null ? -1e9 : hms2sec(r.TIME));
      if (!(age >= 0 && age <= (C.maxAgeSec || 90))) { skipped++; return; }
      var vol = Number(r.VOL) || 0;
      if (!vol || (C.maxVol && vol > C.maxVol)) { skipped++; return; }
      var px = Number(r.PRICE);                                       // 市价单这里是 NaN
      if (!(px > 0)) { skipped++; return; }                           // 我们只发限价单
      if (C.maxNotional && vol * px > C.maxNotional) { skipped++; return; }
      var id = String(r.REQ_ID);
      if (sent.indexOf(id) >= 0) { return; }                          // 幂等：发过不再发
      mark(id);
      acted++;
      if (MODE === "cancel") { v.cancel(r, i, 0); } else { v.send(r, i, 0); }
    });
  }

  function badge() {
    // ⚠️ 徽标必须挂在 body 上做固定层，**不能动页面自己的元素**：第一版给 .footer 设了
    // position:relative 再往里塞 div，跟它自己的 CSS 打架，页脚跑到顶上、表头整行消失
    // （2026-08-20 演练时实测）。观测件不许改被观测对象的布局。
    var el = document.getElementById("tq-autosend-badge");
    if (!el) {
      el = document.createElement("div");
      el.id = "tq-autosend-badge";
      el.style.cssText = "position:fixed;left:8px;bottom:1px;font-size:11px;"
        + "z-index:9999;pointer-events:none;background:transparent;color:"
        + (MODE === "cancel" ? "#e8a33d" : "#4bbf73");
      document.body.appendChild(el);
    }
    el.textContent = "自动" + (MODE === "cancel" ? "取消(演练)" : "确认")
      + " 开 · " + (inHours() ? "交易时段" : "非交易时段只观察")
      + " · 已处理" + acted;
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
        print(f"✗ 找不到页面 {page_path()}")
        return 1
    html = read(page_path())
    on = MARK_BEGIN in html
    print(f"页面   {page_path()}")
    print(f"       补丁 {'✅ 在' if on else '✗ 不在'}，"
          f"sha1={hashlib.sha1(html.encode('utf-8')).hexdigest()[:12]}，"
          f"{len(html)} 字节")
    print(f"备份   {backup_path()}  {'✅' if backup_path().exists() else '✗ 无'}")
    if sidecar_path().exists():
        txt = read(sidecar_path())
        m = re.search(r"window\.__TQ_AUTOSEND\s*=\s*(\{.*?\});", txt, re.S)
        cfg = json.loads(m.group(1)) if m else {}
        print(f"配置   {sidecar_path()}\n       {json.dumps(cfg, ensure_ascii=False)}")
        eff = on and bool(cfg.get("enabled"))
        print(f"\n生效状态：{'✅ 开（' + str(cfg.get('mode')) + ' 档）' if eff else '关'}")
    else:
        print(f"配置   {sidecar_path()}  ✗ 无 ⇒ 即使补丁在，也是关的（fail closed）")
        print("\n生效状态：关")
    return 0


def cmd_apply(args) -> int:
    if not page_path().exists():
        print(f"✗ 找不到页面 {page_path()}")
        return 1
    html = read(page_path())
    if not backup_path().exists():
        if MARK_BEGIN in html:
            print("✗ 页面已带补丁但没有备份，拒绝继续（先手工确认原文）")
            return 2
        write(backup_path(), html)
        print(f"已备份原文 → {backup_path()}")
    clean = strip_patch(html)
    if clean.count("</body>") != 1:
        print(f"✗ </body> 出现 {clean.count('</body>')} 次，拒绝改")
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
    print(f"✅ 补丁已装：{page_path()}")
    print(f"✅ 配置已写：{sidecar_path()}")
    print(f"   {json.dumps(cfg, ensure_ascii=False)}")
    if args.mode == "cancel":
        print("\n⚠️ 现在是**演练档**：命中规则会点【取消】，不会报单。验完记得改成 --mode send")
    if args.anytime:
        print("⚠️ 交易时段闸被放开成全天（只该在演练时用）")
    print("\n下一步：让「交易信号」窗口重新弹一次（发一笔委托），页脚出现徽标即为加载成功。")
    return 0


def cmd_remove() -> int:
    rc = 0
    if page_path().exists():
        html = read(page_path())
        if MARK_BEGIN in html:
            write(page_path(), strip_patch(html))
            print(f"✅ 补丁已从 {page_path()} 移除")
        else:
            print("页面本来就没有补丁")
        if backup_path().exists():
            a, b = read(page_path()), read(backup_path())
            print("与备份逐字节一致 ✅" if a == b else "⚠️ 与备份不一致（客户端可能升级过页面）")
    else:
        print(f"✗ 找不到 {page_path()}")
        rc = 1
    if sidecar_path().exists():
        sidecar_path().unlink()
        print(f"✅ 配置已删 {sidecar_path()}")
    return rc


def _load_tdx_home() -> bool:
    """独立跑这个命令时，客户端安装目录得自己从配置读一次。

    ⭐ 读的是**服务用的那一份配置**（`CN_BROKER_API_CONFIG` / 默认位置），不是再开一个参数：
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
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true", help="看补丁与配置现在什么状态")
    g.add_argument("--apply", action="store_true", help="装/重装补丁（幂等）")
    g.add_argument("--remove", action="store_true", help="卸掉补丁并删配置")
    ap.add_argument("--mode", choices=("send", "cancel"), default="cancel",
                    help="send=生产（点发送）/ cancel=演练（点取消，不报单）。默认演练")
    # 🔴 **不给默认值**：这里原先写着一个真实资金账号（母项目里是自用脚本，无所谓；
    # 本仓库要公开，就成了泄露）。空串＝不限账号，与配置里 `account` 缺省的语义一致。
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
