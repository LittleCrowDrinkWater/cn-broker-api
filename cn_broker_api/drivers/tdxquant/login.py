"""桌面客户端的自动登录：把客户端起起来、登进交易，直到交易通道可用。

分工：本模块只负责把状态从"没登"推到"登上了"；"登上了没有"一律借 `health` 的第②项判，
判据只有一份。登录框的实测形态与四道安全闸见下面各函数。

一句话：风险不是"登不上"，而是**交易密码连续输错会被券商锁一天** ⇒ 认不准就中止、
填完先核对再提交、一次调用只提交一次、永不重试。

Windows 专用（`ctypes.windll`）。非 Windows 上 `u32/g32` 是 `None`，真去调才报错——
在能跑的地方跑、在不能跑的地方明确失败，而不是 import 期就把服务拖挂。
"""
import ctypes
import ctypes.wintypes as w
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_WIN = sys.platform == "win32"

#: 进度是否同时打到屏幕上。CLI 会把它打开；服务里只进日志。
VERBOSE = False

#: 客户端 MCP 地址，由入口注入（与 health 里的安装根目录同理：唯一出处）。
_MCP_URL = "http://127.0.0.1:17709"


def set_mcp_url(url: str) -> None:
    global _MCP_URL
    _MCP_URL = (url or _MCP_URL).rstrip("/")


def mcp_url() -> str:
    return _MCP_URL


def _say(msg: str) -> None:
    """进度输出。**日志一定有**——出问题时那几行就是全部线索。"""
    logger.info("[desktop_login] %s", msg)
    if VERBOSE:
        print(msg)


#: **stdout 与 stderr 都要归一**：后端 logger 写的是 stderr，只归一 stdout 的话
#: 真跑那次的日志全是乱码——而这脚本存在的意义就是出问题时给人看诊断信息。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
#: **刻意不声明 DPI 感知**：客户端是 DPI 不感知进程。保持不感知 ⇒ `GetWindowRect`、
#: `PrintWindow` 给的位图、我们 Post 出去的客户区坐标全在同一套（虚拟化后的）坐标里。
#: 上次声明了感知去点 CEF 页面，四种点击方式"全失败"，差点误判成"Chromium 不接受合成输入"。
u32 = ctypes.windll.user32 if _WIN else None
g32 = ctypes.windll.gdi32 if _WIN else None
ENUM_CB = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM) if _WIN else None
GWL_STYLE = -16
ES_PASSWORD, ES_READONLY = 0x0020, 0x0800
WS_DISABLED, WS_VISIBLE = 0x08000000, 0x10000000
WM_SETTEXT, WM_GETTEXT, WM_GETTEXTLENGTH = 0x000C, 0x000D, 0x000E
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
MK_LBUTTON = 0x0001
BM_CLICK = 0x00F5
VK_BACK, VK_END, VK_RETURN = 0x08, 0x23, 0x0D
SW_MINIMIZE = 6
#: 客户端主窗口的类名（`行情报价` 那个大窗；交易 UI 是它的 MDI 子窗口）。
MAIN_FRAME_CLASS = "TdxW_MainFrame_Class"
#: 凭据文件路径。**由入口按配置注入**——本仓库计划公开，写死机器路径既泄露布局、
#: 又会在别人机器上静默指向不存在的文件。`cred_source = "request"` 时压根不用它。
CRED_PATH: Optional[Path] = None


def set_cred_path(path: Optional[Path]) -> None:
    global CRED_PATH
    CRED_PATH = Path(path) if path else None
PROCS = ("TC.exe", "Tdxw.exe")
#: **客户端(行情)登录窗**的判别标志：它有这几个按钮，交易登录框没有。
#: 两个窗口都含 `SafeEdit`，光看"有没有密码框"会把它们混成一个 ⇒ 必须先分类再动手
#: （2026-08-21 实测：`--start` 起来先弹的是这一个，它有**两个**可填 SafeEdit，
#: 直接套交易登录那套规则会被"认不准"闸挡下来——挡对了，但也说明分类不能省）。
CLIENT_LOGIN_MARKERS = ("游客登录", "扫码登录", "短信登录")
#: 密码框的类名（厂商的安全输入控件）。标准 Edit 带 `ES_PASSWORD` 也算，两条判据并列。
PW_CLASSES = ("safeedit", "safepasswordedit", "tdxpassedit")
#: 一个星号大约多宽（px）。是量出来的：5 个字符 27~28px ⇒ 5.5。核对时给 ±0.6 个字符的余量。
GLYPH_W = 5.5
GLYPH_TOL = 0.6
#: 【登录】那块红的判据。右侧面板是白底，只有它和小字是红的 ⇒ 按"够宽的连续红色横条"找。
RED = dict(r_min=170, g_max=95, b_max=95, min_w=100, min_h=12)
def _txt(h: int) -> str:
    n = int(u32.SendMessageW(h, WM_GETTEXTLENGTH, 0, 0))
    b = ctypes.create_unicode_buffer(n + 2)
    u32.SendMessageW(h, WM_GETTEXT, n + 2, ctypes.byref(b))
    return b.value or ""
def _cls(h: int) -> str:
    b = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(h, b, 256)
    return b.value
def _style(h: int) -> int:
    return u32.GetWindowLongW(h, GWL_STYLE) & 0xFFFFFFFF
def _wrect(h: int) -> Tuple[int, int, int, int]:
    r = w.RECT()
    u32.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top
def _pid(h: int) -> int:
    p = w.DWORD()
    u32.GetWindowThreadProcessId(h, ctypes.byref(p))
    return p.value
def _children(h: int) -> List[int]:
    out: List[int] = []

    def cb(k, _l):
        out.append(k)
        return True

    u32.EnumChildWindows(h, ENUM_CB(cb), 0)
    return out
def _target_pids(names: Sequence[str] = PROCS) -> Dict[int, str]:
    """枚举目标进程。 **比对不分大小写**：同一台机器上装两份客户端时，
    可执行文件名的大小写可以不一样（实测 `Tdxw.exe` 与 `TdxW.exe` 两种拼法都有），
    而 Windows 文件名本身不区分大小写。区分大小写地比对的表现是「进程明明在跑却判成没跑」
    ⇒ 看门狗会一遍遍去拉起一个已经在跑的东西。
    """
    k32 = ctypes.windll.kernel32

    class ENTRY(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                    ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", w.DWORD), ("szExeFile", ctypes.c_char * 260)]

    wanted = {str(n).lower() for n in names}
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
    e = ENTRY()
    e.dwSize = ctypes.sizeof(ENTRY)
    out: Dict[int, str] = {}
    if k32.Process32First(snap, ctypes.byref(e)):
        while True:
            name = e.szExeFile.decode("mbcs", "replace")
            if name.lower() in wanted:
                out[e.th32ProcessID] = name
            if not k32.Process32Next(snap, ctypes.byref(e)):
                break
    k32.CloseHandle(snap)
    return out
def running_processes(names: Sequence[str] = PROCS) -> Dict[int, str]:
    """哪些目标进程在跑：`{pid: 进程名}`。**只读、零成本**——看门狗每分钟醒一次靠的就是它，
    所以这一步不许连客户端、不许抢任何锁。
    """
    return _target_pids(tuple(names))


@dataclass
class Ctl:
    """控件的**只读快照**。识别规则只吃这些字段 ⇒ 可以脱离 Windows 自测。"""

    hwnd: int
    cls: str
    text: str
    style: int
    left: int
    top: int
    width: int
    height: int

    @property
    def visible(self) -> bool:
        return bool(self.style & WS_VISIBLE)

    @property
    def is_edit(self) -> bool:
        return self.cls.lower().startswith("edit")

    @property
    def is_password(self) -> bool:
        return (self.cls.lower() in PW_CLASSES
                or (self.is_edit and bool(self.style & ES_PASSWORD)))

    @property
    def fillable(self) -> bool:
        return (self.visible and not (self.style & WS_DISABLED)
                and not (self.style & ES_READONLY))
class Ambiguous(Exception):
    """认不准。**中止**而不是猜——猜错会变成一次密码错误，连着几次就锁一天。"""
def pick_password(ctls: List[Ctl]) -> Ctl:
    """认出密码框：可见、可填、且类名/样式命中密码判据的**唯一**那个。"""
    pw = [c for c in ctls if c.is_password and c.fillable]
    if len(pw) != 1:
        raise Ambiguous(
            f"可填的密码框应当恰好 1 个，认出 {len(pw)} 个"
            f"（隐藏的那个不算——通讯密码/证书密码平时是隐藏的）")
    return pw[0]
def pick_account(ctls: List[Ctl], want: str) -> Ctl:
    """认出**装着资金账号的那个框**并要求它等于配置里的账号。

     这里做的是**核对而不是填写**：账号由客户端"记住账号"带出来，界面上那个是 DUI 画的，
    真值在一个隐藏 `Edit` 里。核对的价值在于——「默认账户」解析成谁随登录状态变，
    而这是**交易**，绝不能"以为登的是 A、其实登的是 B"。
    """
    hits = [c for c in ctls if c.is_edit and not c.is_password and c.text.strip() == want]
    if len(hits) != 1:
        holding = [c.text.strip() for c in ctls
                   if c.is_edit and not c.is_password and c.text.strip()]
        raise Ambiguous(
            f"没在登录框里找到装着账号 {want} 的输入框（找到 {len(hits)} 个；"
            f"框里现有的值：{holding or '全空'}）⇒ 可能是客户端没勾『记住账号』、"
            f"或者记的是另一个账号。先去客户端里把账号填对再来")
    return hits[0]
def classify(ctls: List[Ctl]) -> str:
    """这是哪道门：`client`（客户端/行情登录）/ `trade`（交易登录）/ `unknown`。"""
    texts = {c.text.strip() for c in ctls}
    if texts & set(CLIENT_LOGIN_MARKERS):
        return "client"
    return "trade" if any(c.is_password for c in ctls) else "unknown"
def visible_tops(pids: Dict[int, str]) -> Dict[int, str]:
    """当前可见顶层窗口 {hwnd: "类名|标题"}。用来做"点之前/点之后"的差集。"""
    out: Dict[int, str] = {}

    def cb(h, _l):
        if _pid(h) in pids and u32.IsWindowVisible(h):
            out[h] = f"{_cls(h)}|{_txt(h)}"
        return True

    u32.EnumWindows(ENUM_CB(cb), 0)
    return out
def close_new_popups(pids: Dict[int, str], known: Dict[int, str], keep: int) -> int:
    """把点击之后**新冒出来的**窗口关掉（二维码那种中间窗），返回关了几个。

     判据是**差集**而不是"标题里有二维码"：这是别人的魔改版，中间窗是什么、叫什么都不好说，
    但"点之前没有、点之后有"这件事是稳的。
     两样绝不碰：主窗口（`MAIN_FRAME_CLASS`）与登录窗自己（`keep`）——把主窗口关了等于
    把客户端杀了，而这一步的全部意义是让客户端活着登进去。
    """
    n = 0
    for h, desc in visible_tops(pids).items():
        if h in known or h == keep or _cls(h) == MAIN_FRAME_CLASS:
            continue
        _say(f"    关掉中间冒出来的窗口 0x{h:x} {desc[:40]!r}")
        u32.PostMessageW(h, 0x0010, 0, 0)          # WM_CLOSE
        n += 1
    if n:
        time.sleep(1.0)
    return n
def pass_client_login(dlg: int, ctls: List[Ctl], *, guest: bool = False,
                      seconds: int = 40, tries: int = 3) -> bool:
    """过第一道门：**点它自己的按钮**，一个字都不填。

     那些按钮是**真 Button**（`BM_CLICK` 打得动），只是被皮肤盖住、样式里标着不可见
    ——所以这里**不能**按"可见"过滤按钮。
     【游客登录】实测要点**两次**：第一次先弹一个二维码窗，叉掉之后再点才真进去
    （用户实测；这是"开心果整合版"的行为）。所以做成 `点 → 关掉新冒出来的窗 → 再点` 的循环，
    而不是"点一次然后等"。【登录】（记住密码那条路）点一次就过，同一段代码兼容。
    """
    want = "游客登录" if guest else "登录"
    btns = [c for c in ctls if c.cls.lower() == "button" and c.text.strip() == want]
    if len(btns) != 1:
        _say(f"[!] 客户端登录窗里找不到唯一的【{want}】按钮（找到 {len(btns)} 个）")
        return False
    btn = btns[0]
    _say(f"  点【{want}】（0x{btn.hwnd:x}，真 Button ⇒ BM_CLICK，不用坐标）")
    for attempt in range(1, tries + 1):
        before = visible_tops(pids_of(dlg))
        u32.SendMessageW(btn.hwnd, BM_CLICK, 0, 0)
        t0 = time.time()
        while time.time() - t0 < seconds / tries + 5:
            if not (u32.IsWindow(dlg) and u32.IsWindowVisible(dlg)):
                _say(f"  第一道门过了（第 {attempt} 次点击，{time.time() - t0:.0f}s）")
                return True
            if close_new_popups(pids_of(dlg), before, dlg):
                break                              # 关掉了中间窗 ⇒ 立刻再点一次
            time.sleep(0.5)
    _say(f"[!] 点了 {tries} 次、{seconds} 秒内还没过第一道门"
          + ("（游客那条路要连点两次，中间窗可能没关掉）" if guest else
             "（这条路要客户端记着行情密码；没记住就改用 --guest）"))
    return False
def pids_of(h: int) -> Dict[int, str]:
    """这个窗口所属进程（够用了：中间窗一定跟登录窗同进程）。"""
    return {_pid(h): "?"}
def snapshot(dlg: int) -> List[Ctl]:
    out = []
    for k in _children(dlg):
        l, t, wd, ht = _wrect(k)
        out.append(Ctl(k, _cls(k), _txt(k), _style(k), l, t, wd, ht))
    return out
def find_login_dialog(pids: Dict[int, str]) -> Optional[int]:
    """当前可见的、含密码框的顶层窗口（**两道门都长这样**，谁是谁交给 `classify`）。
    标题不参与判定（券商会改标题，控件类名不会）。"""
    hits: List[int] = []

    def cb(h, _l):
        if _pid(h) in pids and u32.IsWindowVisible(h):
            for k in _children(h):
                c, st = _cls(k), _style(k)
                if ((c.lower() in PW_CLASSES
                     or (c.lower().startswith("edit") and (st & ES_PASSWORD)))
                        and (st & WS_VISIBLE)):
                    hits.append(h)
                    break
        return True

    u32.EnumWindows(ENUM_CB(cb), 0)
    return hits[0] if hits else None
def wait_dialog(pids: Dict[int, str], seconds: int) -> Optional[int]:
    t0 = time.time()
    while time.time() - t0 < seconds:
        h = find_login_dialog(pids)
        if h is not None:
            return h
        time.sleep(0.4)
    return None
def grab(h: int):
    """`PrintWindow` 抓窗口位图（`PW_RENDERFULLCONTENT`）。返回 PIL Image。"""
    from PIL import Image

    class BI(ctypes.Structure):
        _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
                    ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                    ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG),
                    ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD),
                    ("biClrImportant", w.DWORD)]

    class BMI(ctypes.Structure):
        _fields_ = [("bmiHeader", BI), ("bmiColors", w.DWORD * 3)]

    _l, _t, wd, ht = _wrect(h)
    hdc = u32.GetWindowDC(h)
    mem = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, wd, ht)
    g32.SelectObject(mem, bmp)
    u32.PrintWindow(h, mem, 2)
    buf = ctypes.create_string_buffer(wd * ht * 4)
    bmi = BMI()
    bmi.bmiHeader.biSize = ctypes.sizeof(BI)
    bmi.bmiHeader.biWidth = wd
    bmi.bmiHeader.biHeight = -ht
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    g32.GetDIBits(mem, bmp, 0, ht, buf, ctypes.byref(bmi), 0)
    g32.DeleteObject(bmp)
    g32.DeleteDC(mem)
    u32.ReleaseDC(h, hdc)
    return Image.frombuffer("RGBA", (wd, ht), buf, "raw", "BGRA", 0, 1).convert("RGB")
def find_red_button(img) -> Optional[Tuple[int, int, int, int]]:
    """按颜色找【登录】那块红，返回 (x0, y0, x1, y1)（窗口坐标）。

     按颜色而不是写死坐标：这个框是定尺寸的，但写死坐标是"下次换版就静默点到别处"，
    而点错地方在登录框上可能就是一次错误尝试。只扫右侧白底面板（左边整块是橙色插画）。
    """
    px = img.load()
    W, H = img.size
    x_from = int(W * 0.4)
    runs: Dict[Tuple[int, int], List[int]] = {}
    for y in range(H):
        x, start = x_from, None
        while x < W:
            r, g, b = px[x, y]
            red = r > RED["r_min"] and g < RED["g_max"] and b < RED["b_max"]
            if red and start is None:
                start = x
            elif not red and start is not None:
                if x - start >= RED["min_w"]:
                    runs.setdefault((start, x), []).append(y)
                start = None
            x += 1
        if start is not None and W - start >= RED["min_w"]:
            runs.setdefault((start, W), []).append(y)
    if not runs:
        return None
    (x0, x1), ys = max(runs.items(), key=lambda kv: len(kv[1]))
    if len(ys) < RED["min_h"]:
        return None
    return x0, min(ys), x1, max(ys)
def glyph_width(img, c: Ctl, dlg_origin: Tuple[int, int]) -> int:
    """密码框里那串星号的**总宽度**（px）。

    密码框读不出内容也读不出长度（`SafeEdit` 对 `WM_GETTEXTLENGTH`/`EM_LINELENGTH` 恒回 0，
    实测）⇒ 只能量像素。挡的是"按键掉了几个"——那会直接变成一次密码错误。
    """
    ox, oy = dlg_origin
    box = (c.left - ox + 3, c.top - oy + 3,
           c.left - ox + c.width - 3, c.top - oy + c.height - 3)
    im = img.convert("L").crop(box)
    px = im.load()
    W, H = im.size
    cols = [x for x in range(W) if any(px[x, y] < 128 for y in range(H))]
    if not cols:
        return 0
    # 末尾那根光标是 1~2px 的竖线，从右边界往回削掉它（星号本身约 5.5px 宽）
    span = cols[-1] - cols[0] + 1
    return span if span > 3 else 0
def click(hwnd: int, cx: int, cy: int) -> None:
    lp = (cy << 16) | (cx & 0xFFFF)
    u32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
    u32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    time.sleep(0.06)
    u32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)
    time.sleep(0.2)
def focus_field(c: Ctl) -> None:
    """给密码框焦点：跨进程 `SetFocus` 不管用，改成往它自己身上点一下
    （控件的处理函数会自己 `SetFocus`）。"""
    click(c.hwnd, c.width // 2, c.height // 2)
def type_chars(c: Ctl, s: str) -> None:
    """逐字符发按键。**不用 `WM_SETTEXT`**：它会让星号显示出来，但那是"看起来填上了"，
    安全输入控件真正提交的是自己那条按键管道里的内容 ⇒ 只走按键这条路。"""
    for ch in s:
        vk = ord(ch.upper())
        u32.PostMessageW(c.hwnd, WM_KEYDOWN, vk, 0)
        u32.PostMessageW(c.hwnd, WM_CHAR, ord(ch), 0)
        u32.PostMessageW(c.hwnd, WM_KEYUP, vk, 0)
        time.sleep(0.03)
    time.sleep(0.3)
def clear_field(c: Ctl, n: int = 32) -> None:
    u32.PostMessageW(c.hwnd, WM_KEYDOWN, VK_END, 0)
    u32.PostMessageW(c.hwnd, WM_KEYUP, VK_END, 0)
    for _ in range(n):
        u32.PostMessageW(c.hwnd, WM_KEYDOWN, VK_BACK, 0)
        u32.PostMessageW(c.hwnd, WM_CHAR, VK_BACK, 0)
        u32.PostMessageW(c.hwnd, WM_KEYUP, VK_BACK, 0)
        time.sleep(0.01)
    time.sleep(0.2)
def minimize_client(pids) -> int:
    """把客户端主窗口最小化。返回最小化了几个窗口。

     只动**主窗口**（`TdxW_MainFrame_Class`），不碰「交易信号」那个队列窗口——它是独立顶层，
    而且**隐藏着也照常工作**（2026-08-20 实测：窗口关掉只是隐藏，自动确认脚本仍在跑）。
    最小化不会影响 MCP 端口（进程内的 socket）与行情连接，这一点跑完用四项自检复核。
    """
    n = 0

    def cb(h, _l):
        nonlocal n
        if _pid(h) in pids and u32.IsWindowVisible(h) and _cls(h) == MAIN_FRAME_CLASS:
            u32.ShowWindow(h, SW_MINIMIZE)
            n += 1
        return True

    u32.EnumWindows(ENUM_CB(cb), 0)
    return n
def channel_ok(cred: dict) -> Tuple[bool, str]:
    """现在交易通道通不通（一次连接 + 查资产）。返回 (通不通, 一句话)。

     判据是**账户句柄 + 资产字段**，与 09:15 那步通道自检共用同一段代码 ⇒ 判据只有一份。
    连不上/没登录都只是"还没好"，不是异常——本函数从不抛。
    """
    from cn_broker_api.drivers.tdxquant.health import check_account
    from cn_broker_api.drivers.tdxquant.mcp import McpClient

    try:
        r = check_account(McpClient(mcp_url()),
                          account=str(cred.get("account") or ""),
                          account_type=str(cred.get("account_type") or "STOCK"))
        return r.ok, r.detail
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:110]}"
def verify_logged_in(cred: dict, *, seconds: int = 45) -> bool:
    """判据是**账户句柄 + 资产字段**，不是"窗口不见了"（窗口关掉只说明它被关了）。
    直接复用交易通道自检的第②项 ⇒ 判据只有一份。"""
    from cn_broker_api.drivers.tdxquant.health import check_account
    from cn_broker_api.drivers.tdxquant.mcp import McpClient

    client = McpClient(mcp_url())
    t0, last = time.time(), ""
    while time.time() - t0 < seconds:
        try:
            r = check_account(client,
                              account=str(cred.get("account") or ""),
                              account_type=str(cred.get("account_type") or "STOCK"))
            last = r.detail
            if r.ok:
                _say(f"[ok] 登录已生效：{r.detail}")
                return True
        except Exception as e:  # noqa: BLE001 — 登录过程中连接失败是常态，等下一轮
            last = f"{type(e).__name__}: {str(e)[:110]}"
        time.sleep(2.5)
    _say(f"[!] {seconds} 秒内没等到登录生效。最后一次：{last}")
    return False
class CredMissing(Exception):
    """凭据文件不在/不全。**可捕获的异常**而不是 `SystemExit`：定时任务要把它翻译成"跳过"
    （没配凭据就是没打算用这条自动登录），命令行要翻译成退出码——处置由调用方定。"""


def load_cred() -> dict:
    if CRED_PATH is None:
        raise CredMissing(
            "没配凭据文件路径（config.toml 里的 driver.tdxquant.cred_file）；"
            '若走 cred_source = "request"，密码应当随请求下发而不是读文件')
    if not CRED_PATH.exists():
        raise CredMissing(
            f"凭据文件不在：{CRED_PATH}"
            f'（内容形如 {{"account": "…", "password": "…", "account_type": "CREDIT"}}，'
            f"**放在仓库之外**，绝不入库）")
    cred = json.loads(CRED_PATH.read_text(encoding="utf-8"))
    if not cred.get("account") or not cred.get("password"):
        raise CredMissing(f"{CRED_PATH} 里缺 account 或 password")
    return cred
def _spawn(rel: str) -> None:
    """按安装目录下的相对路径起一个进程（都不带参数——客户端自己拉起它们时也不带）。"""
    from cn_broker_api.drivers.tdxquant.health import tdx_install_root

    exe = tdx_install_root() / rel
    if not exe.exists():
        _say(f"[!] 找不到 {exe}")
        raise SystemExit(2)
    _say(f"启动 {exe}")
    subprocess.Popen([str(exe)], cwd=str(exe.parent), close_fds=True)
def start_client() -> None:
    """行情+量化那一半（`Tdxw.exe`）。MCP 那个端口就是它开的。"""
    _spawn("Tdxw.exe")
def start_trade_module() -> None:
    """交易那一半（`NewTc/TC.exe`，32 位）。

     **它不会自己起来**：客户端开了自动登录之后，`Tdxw.exe` 启动只登行情，交易模块要人在
    界面上点一下【交易】才拉起 ⇒ 于是"既没登上、也没有登录框"这种谁都不动的僵局
    （2026-08-21 实测，等 120 秒也没弹）。自己 `Popen` 它就行：实测 4 秒后交易登录框就出来了，
    与客户端自己拉起它的命令行一样（都不带参数）。
    """
    _spawn(str(Path("NewTc") / "TC.exe"))
def _do_trade_login(dlg: int, ctls: List[Ctl], cred: dict) -> bool:
    """交易登录框：核账号 → 填密码 → 量星号核对 → 点一次【登录】。核不上就清空不提交。"""
    want_acc = str(cred.get("account") or "")
    try:
        pw_c = pick_password(ctls)
        acc_c = pick_account(ctls, want_acc) if want_acc else None
        btn = find_red_button(grab(dlg))
        if btn is None:
            raise Ambiguous("没找到【登录】那块红（界面换版了？先 --probe --shot 看一眼）")
    except Ambiguous as e:
        _say(f"[!] 认不准，中止：{e}\n"
              f"   （认错的代价是密码输错，连着几次就锁一天 ⇒ 宁可不动手）")
        return False
    ox, oy, _w, _h = _wrect(dlg)
    bcx, bcy = (btn[0] + btn[2]) // 2, (btn[1] + btn[3]) // 2
    if acc_c is not None:
        _say(f"  账号已核对：0x{acc_c.hwnd:x} 里是 {acc_c.text.strip()}（只核对不填）")
    pwd = str(cred["password"])
    focus_field(pw_c)
    clear_field(pw_c)
    type_chars(pw_c, pwd)
    got = glyph_width(grab(dlg), pw_c, (ox, oy))
    want = GLYPH_W * len(pwd)
    lo, hi = want - GLYPH_W * GLYPH_TOL - 2, want + GLYPH_W * GLYPH_TOL + 2
    _say(f"  密码框里星号总宽 {got}px（{len(pwd)} 个字符应当在 {lo:.0f}~{hi:.0f}px）")
    if not (lo <= got <= hi):
        clear_field(pw_c)
        _say(" 宽度核不上 ⇒ 按键可能掉了几个。**已清空、不提交**（不提交＝不算一次错误尝试）")
        return False
    _say(f"  核对过了，点【登录】（客户区 {bcx},{bcy}）——只点一次，失败不重试")
    click(dlg, bcx, bcy)
    return True


# ── 高层入口：状态驱动 ─────────────────────────────────

def ensure_logged_in(cred: dict, *, wait: int = 180, start: bool = True,
                     guest: bool = False, minimize: bool = True) -> Tuple[bool, str]:
    """把"交易通道可用"这件事做成。返回 (成不成, 一句话)。

     **状态驱动，不是弹框驱动**：目标是"通道可用"，不是"把某个框填了"。客户端自己开了
    自动登录之后启动就直接登进行情、压根不弹框，而"等登录框"那种写法会把"本来就好了"
    读成超时失败（2026-08-21 实测踩到）。所以循环是 `已登上? → 有框? → 处理`
    ⇒ **天生幂等**，定时任务可以放心反复跑。

     `start=True` 时**两个进程都要保证**：`Tdxw.exe`（行情+量化，MCP 端口的主人）与
    `NewTc/TC.exe`（交易）。 后者**不会自己起来**——客户端的自动登录只登行情，交易模块要人
    在界面上点【交易】才拉起，缺它的表现是"既没登上、也没有登录框"的僵局。

     **失败绝不重试**：密码连续输错会被券商锁一天。失败就如实返回，让上层去告警。
    """
    if not _WIN:
        return False, "只能在 Windows 上跑（客户端是 Windows 程序）"
    pids = _target_pids()
    if start:
        for name, boot in (("Tdxw.exe", start_client), ("TC.exe", start_trade_module)):
            if name in pids.values():
                continue
            boot()
            t0 = time.time()
            while name not in pids.values() and time.time() - t0 < 90:
                time.sleep(1.0)
                pids = _target_pids()
            if name not in pids.values():
                return False, f"起了 {name} 但 90 秒内没见到这个进程"
    if not pids:
        return False, f"没有 {' / '.join(PROCS)} 在跑（start=False 时不替你拉起来）"
    _say(f"目标进程：{pids}")

    t_end = time.time() + wait
    done_client_gate = False
    acted = False                 # 这一趟有没有真动手（过门/填密码）
    while True:
        ok, detail = channel_ok(cred)
        if ok:
            # 本来就登着就不动窗口（多半是人正在看它），只有这一趟真登过才收起来。
            if minimize and acted:
                # 只在确认可用之后才最小化：失败时收窗口等于把唯一能看出哪里不对的东西藏起来。
                got = minimize_client(_target_pids())
                _say(f"已把客户端最小化（{got} 个窗口）——它照常连着")
            return True, detail

        dlg = find_login_dialog(_target_pids())
        if dlg is not None:
            ctls = snapshot(dlg)
            kind = classify(ctls)
            _say(f"遇到登录框 0x{dlg:x}，这是哪道门：{kind}")
            if kind == "client":
                if not done_client_gate:
                    done_client_gate = pass_client_login(dlg, ctls, guest=guest)
                    acted = True
                    if not done_client_gate:
                        return False, "过不了客户端(行情)登录那道门"
            elif kind == "trade":
                acted = True
                if not _do_trade_login(dlg, ctls, cred):
                    return False, "交易登录没做成（认不准或密码没填对 ⇒ 已中止，未提交）"
            else:
                return False, "认不出这是哪道门 ⇒ 中止"

        if time.time() > t_end:
            missing = "TC.exe" not in _target_pids().values()
            return False, (f"{wait} 秒内既没登上、也没等到登录框。"
                           + ("交易模块（TC.exe）没在跑" if missing
                              else "交易模块在跑但不弹框"))
        time.sleep(2.0)
