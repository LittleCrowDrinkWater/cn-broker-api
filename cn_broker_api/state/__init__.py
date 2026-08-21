"""机器写的状态（JSON，人别去改）。每种状态一个模块，这里只做汇总导出。

⭐ 落盘的怕丢（计数清零＝闸失效），`vault` 里的密码相反——**怕留下**，所以只在内存。
⭐ 两种额度（密码提交 / 进程拉起）刻意分开计：混成一个计数器，会让「客户端崩了三次」
把当天的密码额度也吃掉。
"""
from cn_broker_api.state.atomic import atomic_write_json
from cn_broker_api.state.cached_health import CachedHealth
from cn_broker_api.state.health_cache import HealthCache
from cn_broker_api.state.last_run import LastRun
from cn_broker_api.state.latch import SubmitLatch
from cn_broker_api.state.start_budget_used_up import StartBudgetUsedUp
from cn_broker_api.state.submit_blocked import SubmitBlocked
from cn_broker_api.state.vault import PasswordVault
from cn_broker_api.state.watchdog_state import WatchdogState

__all__ = ["atomic_write_json", "CachedHealth", "HealthCache", "LastRun",
           "PasswordVault", "StartBudgetUsedUp", "SubmitBlocked", "SubmitLatch",
           "WatchdogState"]
