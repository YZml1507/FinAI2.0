#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""并发取数守卫 —— 让 `LESSONS.md §25.7` 这条教训**在被违反时报错**，而不只是被写下来。

⛔ 这个模块存在的唯一理由：**散文形式的教训会在下一轮消失。**
   用户 2026-08-06 的原话：「如果下次仍然犯同样的错，那教训经验文档还有什么用呢？」
   判据与 `FINDING-197` 同构 ——
   **如果一条教训只写在文档里、没有任何东西在违反时变红，那它下一轮就不存在。**

本模块把 R29 实测到的**两条**并发陷阱变成机械约束：

① **不得在打点扫描运行期间对同一批端点做对照测量**（`FINDING-205`）
   实测经过：我一边跑 1,029 个探针的扫描，一边开新进程测 `macro_china_cpi_yearly`，
   两次（35s / 150s）都失败，我差点把它归因成"接口不可用"。
   而逐个排除后证明：直接调用 49.1s 成功、`setdefaulttimeout` 后 42.5s 成功、
   `trust_env` patch 后 50.9s 成功、spawn 子进程 63.1s 成功 —— **四种方式全部成功**。
   唯一的差别是**我自己制造的并发**。
   ⚠ 更刺眼的是：我在限流器的注释里**亲手写过**「无法观测其他进程是否也在打同一家」，
   然后违反了它。

② **不得在打点扫描运行期间修改打点产物**（`FINDING-204`）
   打点器每 25 个探针**整份重写**产物，故期间的任何修改都会被静默覆盖。
   我的 12 条重判就是这样丢的，而当时我的 guard 用了比 flush 周期短 50 倍的检测窗口。

用法（两行，加在任何联网取数脚本的入口）：

    from finai.probe_guard import assert_no_concurrent_probe
    assert_no_concurrent_probe("我的脚本名")     # 有扫描在跑就抛错

若确实需要并发（例如打点器自己），显式传 `allow=True` 或设
`FINAI_ALLOW_CONCURRENT_PROBE=1` —— **让绕过成为一个可见的决定，而不是默认行为。**
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 打点器的 lockfile。存在且 PID 存活 ⇒ 有扫描在跑。
PROBE_LOCK = PROJECT_ROOT / "artifacts" / "interface_matrix" / "auto_probe.lock"

#: 显式绕过的环境变量。⭐ 命名刻意冗长 —— 绕过应该看起来像一个决定。
BYPASS_ENV = "FINAI_ALLOW_CONCURRENT_PROBE"


class ConcurrentProbeError(RuntimeError):
    """有打点扫描在运行时又发起了联网取数 / 修改产物。

    ⛔ 这不是"保守起见拦一下"，而是拦一个**已经发生过并造成误判的**错误：
    `FINDING-205` 里我因此把一个 49 秒能成功的接口记成了 `FAIL_UNREACHABLE`，
    并据此请用户去排查一个完全正常的域名。
    """


#: 锁的绝对上限。超过它即视为陈旧 —— ⛔ 这条路径**不询问 PID 是否存活**，
#: 故判活机制坏掉时系统仍能自愈（`FINDING-282` 兜底 ③）。
#: 取 24h：实测最长一次全量打点 1,066 个探针远小于此，不会误杀在跑的扫描。
LOCK_MAX_AGE_S = 24 * 3600


def _pid_alive(pid: int) -> bool:
    """进程是否存活。Windows 用 `OpenProcess`，POSIX 用 `signal 0`。

    ⛔ **不用 `tasklist`**（`FINDING-282` / `FINDING-284`）。曾经的实现是
    `subprocess.run(["tasklist", ...], timeout=15)`，实测 **6/6 全部 15.02s
    `TimeoutExpired`**，被 `except Exception: return True` 吞成「存活」
    ⇒ `probe_in_progress` 的「陈旧锁」分支（只在判死时才走）**恒不可达**
    ⇒ 一旦有锁残留，取数被永久拦住，且理由是假的。
    ⛔ **我曾把根因归到「带 `timeout=` 的控制台子进程」这一形状，该归因已撤回**
      （`FINDING-288`）。四腿复测：无 `timeout` 同样 **3/3 挂满 90s**，
      连不带 `/FI` 过滤器的裸 `tasklist` 也挂 2/3 ⇒ **`tasklist` 这个可执行文件
      在本机当前调用即挂**，与调用形状无关。旧归因源于**单次**观测，
      而单次成功不能证否一个"有时挂"的现象（`FINDING-182` 同一错法）。
    ⭐ 但修法方向不受该撤回影响，反而更必要：正确修法是**不用子进程回答判活**，
      而不是调大 timeout —— 后者只把「15 秒后误判活」变成「更久后误判活」，形状不变。
    ⭐ 附带好处：不再有子进程 ⇒ 不再有编码面（`FINDING-231`）可踩。

    ⚠ 保守方向仍然保留，但**只在"进程确实存在但无权查询"时**（`ERROR_ACCESS_DENIED`）：
    宁可多拦一次，不可漏拦 —— 漏拦的代价是一个静默的错误结论（`FINDING-205`）。
    ⛔ 与旧实现的关键差别：**"查不到"不再等于"存活"**。
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        try:
            import ctypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = k32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                # 打不开：ACCESS_DENIED ⇒ 进程存在但无权查询（判活，保守）；
                # 其余（INVALID_PARAMETER=87 等）⇒ 该 PID 不存在（判死）。
                return ctypes.get_last_error() == ERROR_ACCESS_DENIED
            try:
                code = ctypes.c_ulong()
                ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
                # ⚠ 已退出的进程其 handle 仍可打开（僵尸句柄），必须看退出码，
                #   否则"能打开"会被误读成"活着"。
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                k32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            # ctypes 不可用（极罕见）时退到 psutil；⛔ 仍不退回 tasklist。
            try:
                import psutil

                return psutil.pid_exists(int(pid))
            except Exception:  # noqa: BLE001
                return True   # 两条都不可用 ⇒ 保守判活，但 `_lock_expired` 仍能兜底
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


#: `create_time` 与 `started_at` 比对的容差。取 5s 吸收两者的时钟/取值时刻差异
#: （锁在 `network_gate()` 之后写，探针进程本身的创建更早，故正常情况下差值为**负**）。
PID_REUSE_TOLERANCE_S = 5.0


def _pid_is_reused(info: dict) -> tuple[bool, str]:
    """写锁进程是否已退出、其 PID 被**同一次开机内**的另一个进程复用。

    ⛔ `FINDING-316` 实测逼出来的第三条兜底。撞上的形状：
       锁写于本地 `00:41:29`，而 `PID=16192` 实测是
       `cmd.exe /c "hermes mcp serve"`，`create_time` = `00:43:47`
       —— **比锁晚 137.6 秒才诞生**。于是：
         · `_pid_alive` → True（PID 真活着，只是换了主人）
         · `boot_id` 兜底不触发（复用发生在**同一次开机内**，boot_id 完全匹配）
         · 24h 绝对上限不触发（锁只有几分钟）
       ⇒ 三条释放路径同时失效，取数被一个**假理由**永久拦住。
       ⭐ `FINDING-282` 的注释曾写「boot_id 不匹配也顺手消除 PID 复用」——
         那句只对**跨重启**的复用成立，同一次开机内的复用它接不住。

    判据是结构性的：**进程创建时间不可能晚于它自己写下的锁**。
    ⛔ 刻意不比对进程名/命令行 —— 探针以 `python` / `cmd` 等多种形态出现过，
       白名单会漏判；而"创建时间不能倒流"不依赖任何名单。
    ⚠ 拿不到 `create_time`（psutil 缺失 / 无权 / 进程已消失）时返回 False，
      维持保守方向交回判活 —— 宁可多拦一次（`FINDING-205`）。
    """
    started = info.get("started_at")
    try:
        pid = int(info.get("pid", -1))
    except (TypeError, ValueError):
        return False, ""
    if pid <= 0 or not started:
        return False, ""
    try:
        from datetime import datetime, timezone

        import psutil

        t_lock = datetime.fromisoformat(str(started))
        if t_lock.tzinfo is None:
            t_lock = t_lock.replace(tzinfo=timezone.utc)
        t_proc = psutil.Process(pid).create_time()
    except Exception:  # noqa: BLE001
        return False, ""
    delta = t_proc - t_lock.timestamp()
    if delta > PID_REUSE_TOLERANCE_S:
        return True, (f"PID {pid} 的创建时间比锁晚 {delta:.1f}s ⇒ 它不是写锁的那个"
                      f"进程（同一次开机内 PID 已被复用），写锁进程必已退出")
    return False, ""


def _lock_expired(info: dict) -> tuple[bool, str]:
    """锁是否已按**与判活无关**的判据过期。返回 `(是否过期, 理由)`。

    ⛔ 存在的理由（`FINDING-282` 兜底 ③）：判活是**唯一**释放路径时，
       判活一坏，陈旧锁就永不释放。这里给出三条不问"PID 是否存活"的释放判据：

    ① **重启跨越**：锁记的 `boot_id` 与当前不同 ⇒ 写锁的那个进程必然已死。
       ⚠ 只覆盖**跨重启**的 PID 复用，同一次开机内的复用它接不住（`FINDING-316`）。
    ② **PID 复用**：该 PID 的 `create_time` 晚于锁的 `started_at` ⇒ 换了主人。
    ③ **绝对上限**：`started_at` 距今超过 `LOCK_MAX_AGE_S`。

    ⚠ 三条都**只在能确定时**才判过期；拿不到 `boot_id` / `create_time` /
      时间不可解析时返回 False（维持保守方向，交给判活）。
    """
    boot_id = info.get("boot_id")
    if boot_id:
        try:
            import psutil

            if str(boot_id) != str(int(psutil.boot_time())):
                return True, "锁写于上次开机之前（boot_id 不匹配）⇒ 写锁进程必已退出"
        except Exception:  # noqa: BLE001
            pass
    reused, why = _pid_is_reused(info)
    if reused:
        return True, why
    started = info.get("started_at")
    if started:
        try:
            from datetime import datetime, timezone

            t0 = datetime.fromisoformat(str(started))
            if t0.tzinfo is None:
                t0 = t0.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - t0).total_seconds()
            if age > LOCK_MAX_AGE_S:
                return True, (f"锁已存在 {age / 3600:.1f}h，超过绝对上限 "
                              f"{LOCK_MAX_AGE_S / 3600:.0f}h")
        except Exception:  # noqa: BLE001
            pass
    return False, ""


def probe_in_progress() -> tuple[bool, str]:
    """是否有打点扫描在运行。返回 `(是否在跑, 人读的理由)`。

    ⛔ `FINDING-204`：**不用任何时序信号推断**（mtime 窗口 / 行数是否增长）。
    那两种我都试过、都失败过：
      · mtime 3s 窗口   —— flush 间隔实测 1~5 分钟，3s 必然落在两次之间
      · 行数 70s 兜底   —— 打点卡在含 33 项内循环的慢探针上，200 秒行数不变
    ⭐ 只认**存在性**（lockfile + PID），把竞态问题变成状态问题。
    """
    if not PROBE_LOCK.exists():
        return False, "无 lockfile"
    try:
        info = json.loads(PROBE_LOCK.read_text(encoding="utf-8"))
        pid = int(info.get("pid", -1))
    except Exception:  # noqa: BLE001
        return True, f"lockfile 存在但不可解析（保守判为在跑）: {PROBE_LOCK.name}"
    # ⭐ 先走**与判活无关**的释放路径（`FINDING-282` 兜底）：判活坏掉时仍能自愈。
    expired, why = _lock_expired(info)
    if expired:
        return False, f"lockfile 存在但已过期（陈旧锁）：{why}"
    if _pid_alive(pid):
        started = info.get("started_at", "?")
        planned = info.get("planned", "?")
        return True, (f"打点扫描进行中: PID={pid} 启动于 {started} "
                      f"计划 {planned} 个探针")
    return False, f"lockfile 存在但 PID {pid} 已退出（陈旧锁）"


def assert_no_concurrent_probe(caller: str, *, allow: bool = False) -> None:
    """有打点扫描在跑就抛 `ConcurrentProbeError`。

    Args:
        caller: 调用方名字，出现在报错里，便于定位是谁违反了约束。
        allow:  显式允许并发（打点器自己传 True）。

    ⭐ 为什么是**抛错**而不是打印警告：
    警告会被忽略 —— `FINDING-205` 那次我如果只看到一行警告，
    大概率还是会把结果当真。教训要生效，必须让违反**无法继续**。
    """
    if allow or os.environ.get(BYPASS_ENV) == "1":
        return
    running, reason = probe_in_progress()
    if not running:
        return
    # ⚠ 不得用 `relative_to(PROJECT_ROOT)` —— lockfile 若在项目外（测试 monkeypatch、
    #   或将来把它配到别处）会抛 ValueError，**让守卫自己崩掉**。
    #   守卫崩掉比不拦更糟：调用方看到的是 ValueError 而不是"有扫描在跑"。
    try:
        lock_display = PROBE_LOCK.relative_to(PROJECT_ROOT)
    except ValueError:
        lock_display = PROBE_LOCK

    raise ConcurrentProbeError(
        f"\n⛔ [{caller}] 拒绝执行：{reason}\n"
        f"\n为什么拦你（LESSONS §25.7 / FINDING-205 实测）：\n"
        f"  打点扫描正在对同一批端点发请求。此时再发一路请求会：\n"
        f"    ① 抢同一端点的配额 —— 实测把一个 49 秒能成功的接口\n"
        f"       推成两次超时失败（35s 与 150s），我差点记成『接口不可用』\n"
        f"       并据此请用户去排查一个完全正常的域名；\n"
        f"    ② 若你要改产物，改动会被打点器下一次 flush 静默覆盖\n"
        f"       （FINDING-204：我的 12 条重判就是这样丢的）。\n"
        f"\n怎么办：\n"
        f"  · 等扫描结束（查 {lock_display}）\n"
        f"  · 或确实需要并发时显式绕过：{BYPASS_ENV}=1\n"
        f"    ⚠ 绕过后得到的失败结果**不可作为『接口不可用』的证据**。\n"
    )
