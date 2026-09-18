# mutmut `segfault` 判决的根因调查（真幸存变异为什么从门禁视野里消失）

* 日期：2026-09-18
* 来源：独立验证报告 [`docs/independent-test-2026-09-18/report.md`](../independent-test-2026-09-18/report.md)
  P1-3（"约 45% 的变异体被判成 `segfault` 而该判决不计入门禁任何计数"）
* 结论一句话：**判决是错的，而且根因不是 mutmut 的逻辑，是 macOS 上"父进程热过
  SystemConfiguration、fork 出的子进程再碰一次"会 SIGSEGV**。仓库侧可修，已修并有反例。
* 时间盒：90 分钟（到点即停）。**已定位根因并验证修复**，未定部分写在 §5。

---

## 0. 结论（先读这段）

1. **`-11` 是 mutmut 的子进程真的被 SIGSEGV 杀掉了**，不是 mutmut 的分类 bug。
   `.meta` 里 906 条 `-11`、6 条 `-24`（SIGXCPU = timeout）——两者合计 912，
   正好是 `1110`（killed+survived+no_tests）与 `2022`（实际评估数）之差。
2. **崩溃点在 macOS 的系统代理解析**：`urllib.request.getproxies()`
   → `_scproxy.get_proxy_settings()` → `SystemConfiguration` → `CoreFoundation`
   → `libsystem_trace` 的 `_os_log_preferences_refresh`。本机 16 份崩溃报告**逐份栈相同**。
3. **触发条件是"热"**：父亲进程先调用过一次 `getproxies()`，fork 之后子进程**再调一次**，
   才崩。单纯 fork 后第一次调用不崩。mutmut 默认的 `fork` 隔离恰好满足这个条件——
   它先在主进程里跑完 clean tests（`ForkRunner.run_clean_tests` 不 fork），
   测试集里有一个用例会调 `urlopen`，于是主进程被"热"了，之后每个 fork 出来的变异体
   子进程再碰到这条路径就崩。
4. **修法**：让 `getproxies()` 在**环境变量那一层**就短路，永远不碰 SystemConfiguration。
   `tests/conftest.py` 里两行（`no_proxy` / `NO_PROXY` = `*`），写在 import 期，
   mutmut 的主进程与它 fork 的所有子进程都继承。**全量重跑后 `-11` 归零**（§4）。
5. **对门禁的影响（重要）**：修好之后幸存变异数从 **180 涨到 700**——原先被
   `segfault` 吞掉的那 906 条里，有大量是**真幸存变异**。也就是说"存活率 0.1648"这个数
   是**分类口径造成的假象**，不是测试变强。这条更正必须在所有引用处回灌。

---

## 1. 证据链 A：现象与控制变量

### A1 判决分布（修之前）

```bash
.venv/bin/python - <<'PY'
import json, collections, pathlib
for p in sorted(pathlib.Path('mutants').rglob('*.meta')):
    d = json.loads(p.read_text())
    ec = d.get('exit_code_by_key', {})
    print(p, len(ec), dict(sorted(collections.Counter(str(v) for v in ec.values()).items())))
PY
```

实测（`feat/integrations-boundaries` tip，Python 3.14.6，mutmut 3.8.0）：

```
mutants/harness/approval.py.meta            29  {'1': 26, '-11': 3}
mutants/harness/execution.py.meta         1157  {'-11': 505, '1': 483, '0': 161, '33': 4, '-24': 4}
mutants/harness/loop.py.meta               542  {'-11': 322, '1': 217, '-24': 2, '0': 1}
mutants/harness/store/checkpoints.py.meta  294  {'1': 186, '-11': 76, '0': 18, '33': 14}
合计 2022：{'1': 912, '-11': 906, '0': 180, '33': 18, '-24': 6}
```

对照 mutmut 自己的编号表（`.venv/lib/python3.14/site-packages/mutmut/stats.py::status_by_exit_code`）：

| 退出码 | mutmut 状态 | 本仓库实测条数 |
|---|---|---|
| `1` / `3` | `killed` | 912 |
| `0` | `survived` | **180** |
| `33` / `5` | `no tests` | 18 |
| `-11` / `-9` | **`segfault`** | **906（44.8%）** |
| `-24` / `24` / `152` / `255` | `timeout` | 6 |

`912 + 180 + 18 = 1110`（= 入库基线的 `total`），`1110 + 906 + 6 = 2022`（= 实际评估数）。
这正是独立验证报告 F25 / P1-2 定位的那 912 条差额——**它全部是"没有判决"的变异体**，
不是"没被评估"。

### A2 焦点案例

`harness/approval.py` 逐字未改，`main` 基线的 3 条幸存变异
（`is_expired__mutmut_1` / `is_expired__mutmut_4` / `validates__mutmut_5`）
在 tip 上**恰好这 3 条**变成 `-11`。单条复现：

```bash
.venv/bin/python -m mutmut run "harness.approval.xǁApprovalBindingǁis_expired__mutmut_1" --max-children 1
# → 💥 harness.approval.xǁApprovalBindingǁis_expired__mutmut_1（segfault）
```

而独立验证报告已用反例证明它是**真幸存变异**（手工 `apply` 后整套默认用例全绿）。
⇒ 判决错误，这一点不依赖根因。

---

## 2. 证据链 B：崩溃栈（根因）

macOS 会把 Python 解释器的崩溃写成 `.ips`。本机 16 份当天崩溃报告**逐份**：

```bash
python3 - <<'PY'
import json, pathlib, collections
c = collections.Counter()
for p in sorted(pathlib.Path.home().glob("Library/Logs/DiagnosticReports/Python-2026-09-18-*.ips")):
    d = json.loads(p.read_text(errors="replace").partition("\n")[2])
    th = d["threads"][d["faultingThread"]]
    names = [d["usedImages"][f["imageIndex"]].get("name", "?") for f in th["frames"]]
    c[tuple(names[3:20])] += 1
for k, v in c.most_common():
    print(v, "|", " -> ".join(k))
PY
# 16 | Python -> libsystem_platform.dylib -> libsystem_trace.dylib -> libsystem_trace.dylib
#    -> CoreFoundation -> CoreFoundation -> CoreFoundation -> CoreFoundation -> CoreFoundation
#    -> CoreFoundation -> CoreFoundation -> SystemConfiguration -> _scproxy.cpython-314-darwin.so
#    -> Python -> Python -> Python -> Python
```

即：`_scproxy.get_proxy_settings` → `SCDynamicStoreCopyProxiesWithOptions`
→ `_CFXPreferences copyAppValueForKey` → `_os_log_preferences_refresh` → **SIGSEGV**。

`_scproxy` 只被 `urllib.request.getproxies()`（macOS 分支）使用。也就是说：
**崩溃与变异体的语义毫无关系，只与"进程在 fork 前后碰过系统代理解析"有关。**

### B1 谁在测试集里调 `getproxies()`（全量扫描，只有一个调用点）

```bash
cat > /tmp/mut-inv/proxyprobe.py <<'PY'
import traceback, urllib.request
_orig = urllib.request.getproxies
def patched():
    for f in [f for f in traceback.extract_stack() if "/tests/" in f.filename or "harness/" in f.filename][-4:]:
        print(f"[proxyprobe] {f.filename}:{f.lineno} {f.name}")
    return _orig()
urllib.request.getproxies = patched
PY
PYTHONPATH=/tmp/mut-inv .venv/bin/python -m pytest -o addopts= -p no:cacheprovider -p proxyprobe -q -s tests/
# [proxyprobe] …/tests/test_observability_sink.py:37 exported
# [proxyprobe] …/harness/otel.py:152 post_otlp_json
```

`tests/test_observability_sink.py` 的 `exported` fixture → `integrations/otlp_local_sink.py::round_trip`
→ `harness/otel.py::post_otlp_json` → `urllib.request.urlopen` → `getproxies()`。
默认测试集里**只有这一条**路径会走到系统代理解析。

### B2 控制变量矩阵（最小复现，不依赖 mutmut）

`/tmp/mut-inv/repro2.py`：`warm`(父进程先调一次 `getproxies`) × `thr`(fork 前起一个后台线程)
× `env`(设 `http_proxy`) → fork → 子进程再调一次，记录子进程退出码：

```
warm=False thr=False env=False -> child exit: 0
warm=False thr=False env=True  -> child exit: 0
warm=False thr=True  env=False -> child exit: -6    ← objc fork-safety abort（另一种崩溃）
warm=False thr=True  env=True  -> child exit: 0
warm=True  thr=False env=False -> child exit: -11   ← mutmut 记的 segfault
warm=True  thr=False env=True  -> child exit: 0
warm=True  thr=True  env=False -> child exit: -11
warm=True  thr=True  env=True  -> child exit: 0
```

两个结论：

* `-11` 需要"父进程先热过"这一条；单线程 fork 后首次调用不崩（`warm=False thr=False` → 0）。
* `http_proxy`（任何一个 `*_proxy` 非空环境变量）让 `getproxies_environment()` 返回非空，
  `getproxies()` 于是在 `return getproxies_environment() or getproxies_macosx_sysconf()` 的
  `or` 处短路，**两条崩溃路径一起消失**。

### B3 为什么 mutmut 的默认隔离会满足"热"这个条件

`mutmut/workers/isolation.py` 的 `ForkRunner`：

* `get_mutant_runner()` 在**主进程**里建 `PytestRunner`（主进程已 import pytest）；
* `collect_or_load_stats()` → `ForkRunner.collect_stats()` **不 fork**，直接在主进程里跑一遍测试集；
* `run_clean_tests()` → 同样**不 fork**，主进程跑一遍完整的 clean tests；
* 之后每个变异体 `os.fork()`，子进程里 `pytest.main()` 跑这个变异体的测试子集。

⇒ 主进程在第一次 fork 之前就已经被 clean tests 里的 `urlopen` 热过了。
凡是"测试子集里包含那次 `urlopen`"的变异体，子进程都会崩——
**判成 `segfault` 的边界由"测试选择"决定，不由变异体的语义决定**。
这解释了两个反直觉事实：

* `approval.py` 逐字未改，3 条幸存变异却集体变 `-11`（它们的测试子集恰好覆盖那条路径）；
* 存活数 94 → 18、76 条变 `-11`（`checkpoints.py` 同样逐字未改）。

`register_timeout()`（第一次 submit 时起一个 daemon 线程）是第二层风险：
它让主进程变成多线程，于是 fork 出来的子进程碰 Objective-C 时会走 `objc_initializeAfterForkError`
分支直接 abort（`-6`，矩阵第二行）。带 `no_proxy` 修复后这条也不会被碰到。

---

## 3. 修法（已落地）

```python
# tests/conftest.py（import 期，父进程先跑到这里，fork 的子进程才继承得到）
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
```

为什么是这里、而不是别处：

| 备选 | 为什么不选 |
|---|---|
| 改用 `process_isolation = "forkserver"` | 只是把 `-11` 换成 `-6`（fork server 第一次 `register_timeout` 之后就是多线程，孙进程碰 ObjC 会 abort，而 `-6` 在 mutmut 里落到默认分类 `suspicious`，**照样不可见**）。 |
| 只在 `scripts/mutation_check.py` 里给 `mutmut run` 传环境变量 | 能修好门禁，但**裸 `mutmut run` 与任何别的 fork 型 runner（pytest-xdist）仍会崩**；问题的真正范围是"测试集碰了系统代理解析"，不是"门禁传了环境变量"。 |
| 把 `harness/otel.py` 的 `urlopen` 换成不走代理的 opener | 会**去掉 OTLP 导出的系统代理支持**——那是内核路径的行为改动，代价大于收益，而崩溃的根因在 fork 之后的子进程里，不在这一处调用。 |
| 给 mutmut 打补丁 / 换版本 | 根因在 macOS 的 `SystemConfiguration` 与 fork 语义，不是 mutmut 的逻辑；打补丁等于把可复现性交给一个外部补丁。 |

副作用评估：用例只连回环地址（本地 OTLP 接收器、HTTP 接收器），`no_proxy=*` 的语义就是
"回环直连"，与用例的意图一致；已验证 `urlopen` 到本地接收器仍返回 200。

**行为改变（口径更正，2026-09-18 独立验证实测）**：在**配了系统代理**的机器上，回环请求
此前会被交给代理，现在直连——实测"代理命中 1 → 0"。本文件初稿写的是"没有任何用例的实际
HTTP 行为改变"，**那句话与实测不符**，此处按实测改写。这个改变本身是**修正**而不是回归
（回环请求走代理本来就是运气，见 `tests/conftest.py` 的注释），但它确实是一处可观测的行为
差异，因此记在这里而不是抹掉。

---

## 4. 修复验证（全量重跑，修复前 vs 修复后）

```bash
rm -rf mutants/ && .venv/bin/python -m mutmut run --max-children 4
```

| | 修复前（tip，2026-09-18 入库基线） | 修复后 |
|---|---|---|
| 实际评估数 | 2022 | 2049 |
| `killed` (1) | 912 | **1331** |
| `survived` (0) | 180 | **700** |
| `no tests` (33) | 18 | 18 |
| **`segfault` (-11)** | **906** | **0** |
| `timeout` (-24) | 6 | **0** |
| 判定类合计 | 1092 | 2031 |
| 不可见空间 | 912 / 2022 = **45.1%** | 18 / 2049 = **0.9%** |
| 幸存率 | 0.1648 | **0.3447** |

> 数字口径：`survivor_rate = survived / (killed + survived)`，与 `scripts/mutation_check.py`
> 一致。"修复后"一列由 `reports/mutation_baseline.json`（schema v2）再生，claim 见
> `mutation-survivors` / `mutation-survivor-rate` / `mutation-inconclusive` /
> `mutation-invisible-share`。旁证：`not checked` 在本轮归零，结果表被完整填满。
>
> **2022 vs 2049 这 27 条的差，实测到的是这些**：差**全部**在 `harness/execution.py`
> （结果表里 1157 → 1184 条），其余三个模块逐条不变（`loop.py` 542、`checkpoints.py` 294、
> `approval.py` 29）。同时 `mutmut print-time-estimates` 现在列出 **2049**，与结果表
> **完全相等**——也就是说"列出数 ≠ 评估数"这个说法本身就不成立：mutmut 列出多少就评估多少，
> 1110 与 2022 的差是"**有判决 vs 无判决**"的差，不是"列出 vs 评估"的差。
> **哪 27 条没有逐一核对出来**，也不该靠猜：`mutants/*.meta` 是**单文件覆盖写**、不是
> append-only，本轮第一次 `mutmut run` 就把它重写了。⇒ 从缓存里读出的计数只在**读的那一刻**
> 有意义：这也是为什么入库的基线必须自带 `status_by_mutant`（逐条状态），
> 而不是只留一个总数。

**三个必须说清的点：**

1. `segfault` 与 `timeout` **双双归零**：906 + 6 = 912 条全部回到可判定集。
   这说明根因确实是"fork 子进程碰系统代理解析"，不是"变异体本身会让解释器崩"。
2. **幸存率从 0.1648 涨到 0.3447 不是变差了，是变诚实了**。原先那 912 条根本没被判定，
   而基线与文档把 `survivor_rate` 放在台面上——它是**分类口径造成的假象**。
   所有引用这个数的地方（README / HANDOFF / docs/ / reports/）已按新口径回灌。
3. **全量重跑变慢了**：修复前一轮 ≈3–6 分钟，修复后 **8 分 35 秒**（`--max-children 4`）。
   原因是被杀的变异体会 `-x` fail-fast 停下，而幸存的变异体要跑完整个测试子集——
   幸存者从 180 涨到 700，自然更慢。nightly 的预算因此从 `--timeout 1320`（22 分钟）
   上调到 `--timeout 2400`（40 分钟），job 上限 25 → 45 分钟——这是**预算**调整，
   不是判据放宽（超时仍然判失败）；同步写在 `docs/testing.md` §2 与 `nightly.yml` 注释里。
   CI runner 上的真实耗时只能由 CI 自己回答，本机给不出。

独立验证报告 §3 P1-3 的那个反例（`is_expired__mutmut_9` 手工 apply 后全绿）
在本轮修复后由 mutmut 自己判成 `survived`——判决与手工验证一致。
`harness/approval.py` 的 3 条（`is_expired__mutmut_1` / `is_expired__mutmut_4` /
`validates__mutmut_5`）同样回到 `survived`，与 `main` 基线的名字集合逐条相同
（`main` 基线见 `docs/independent-test-2026-09-18/report.md` §2 F23③）。

### 4.1 修复后的三个"改坏必须变红"验证（实跑，非单元测试）

| # | 注入 | 命令 | 实测 |
|---|---|---|---|
| V1 | 构造"180 条基线幸存变异全部缺失"（报告 §3 P0-1 的原始场景）：拿真实 v2 基线额外塞 180 条幸存变异，结果表里当然没有它们 | `scripts/mutation_check.py --module "harness.approval.*" --baseline /tmp/…/baseline_plus180.json` | **退出 1**，`[vanished-from-results]` 180 条，逐条打印名字。修复前这一格是"有 180 条…已被杀死（好事，不判失败）" + **退出 0** |
| V2 | 在 `harness/approval.py` 里加一个**没有任何用例调用**的函数（含一条永不被执行的分支），跑完还原 | `scripts/mutation_check.py --module "harness.approval.*"` | **退出 1**，`[new-no-tests]` 10 条，全在 `x__gate_probe_uncovered__mutmut_*` 上。修复前同一注入是 `no_tests 0 → 12` 而**退出 0** |
| V3 | 把**修复前的真实 `segfault` 判决**原样 replay 回结果表（819 条落进当前变异体集合的） | `scripts/mutation_check.py`（全量） | **退出 1**，`[decided-to-inconclusive]` 819 条 + `[inconclusive-grew]` 819 条；不可见空间打印 `837/2049 = 40.8%`；名单里正是报告点名的 `is_expired__mutmut_1` / `__mutmut_4` / `validates__mutmut_5` |

三格各自都有一格**对照**紧挨着跑：未注入时同一命令 **退出 0**（见 §6 的运行记录）。
`--update-baseline` 在一致的工作树上可重复：连跑两次产出的基线逐字相同。

---

## 5. 未定部分 / 残余边界

1. **环境边界**：结论建立在 macOS（darwin 25.6.0 arm64）+ mutmut 3.8.0 上。
   **Python 版本不是这一条的变量**——已用同一个最小复现在两版上各跑一次（`/tmp/venv311`
   是按 CI 版本建的 3.11.15 环境）：

   ```
   ### 3.14.6
     cold nothr noenv => child exit: 0
     warm nothr noenv => child exit: -11
     warm nothr env   => child exit: 0
   ### 3.11.15
     cold nothr noenv => child exit: 0
     warm nothr noenv => child exit: -11
     warm nothr env   => child exit: 0
   ```

   两版逐格相同 ⇒ 这是 **macOS 框架 + fork 语义**的问题，不是某个 CPython 版本的特性。
   **CI（Linux/ubuntu）上没有 `_scproxy`，预期不受影响——这一点仍未在本机实测**，
   只是机理上 `getproxies()` 在 Linux 走的是环境变量/注册表分支，不碰 CoreFoundation。
2. **`-6`（objc fork-safety abort）没有在本仓库的实际运行里观察到**：
   它是控制变量矩阵里的另一条路径，被 `no_proxy` 一并堵住了，但"堵住之后还会不会从别的
   入口出现"没有穷举。矩阵里的 `warm=False thr=True env=False` 那一格说明：
   只要子进程碰 Objective-C 就有这个风险，而本仓库的测试集只从 `_scproxy` 这一个入口碰它。
3. **`no_proxy=*` 会掩盖"真的需要代理的用例"**：今天没有这样的用例（用例只连回环地址），
   但这是一个**有意的取舍**——测试进程不读宿主代理配置。若将来有用例要验证代理行为，
   必须显式在该用例内部覆盖，不能依赖宿主环境。
4. **没有追到 `_os_log_preferences_refresh` 内部的哪一行**（那是 Apple 的二进制）。
   能定到"父进程热过 + fork 子进程再调"这一层，足够给出可验证的修法；再深挖属于 Apple 框架
   内部实现，本仓库拿不到符号。
5. **那 27 条差异没有逐条核对**（见 §4 的口径说明）。`mutants/*.meta` 是覆盖写的单文件，
   本轮第一次 `mutmut run` 就重写了修复前的那份，无法再逐条比对。可以确定的是差**只在**
   `harness/execution.py`，且 `print-time-estimates` 与结果表现在**完全相等**（2049 = 2049），
   所以它不影响判定，也不影响"列出数 = 评估数"这个更正。**这是观测与推断，不是逐条实测。**
6. **Linux/CI 上是否存在同族问题未测**（机理上不应有：`getproxies_macosx_sysconf` 只在 macOS
   存在），但本机没有 Linux 环境可跑。

---

## 6. 对门禁设计的建议（已落地）

根因修好之后，`segfault` 这个类别**仍然会存在**（任何真崩溃都会）：它不该被当成"无事发生"。
本轮同时改了判据（`scripts/mutation_check.py`）：

1. **全状态记账**：mutmut 的每一个状态都进三类之一（判定类 / 未覆盖类 / 无结论类），
   每次运行打印"不可见空间"的规模；`survivor_rate` 明确标注**不是覆盖率**。
2. **基线 schema v2**：逐条记录每个变异体的状态，于是可以判
   "基线里 `killed`/`survived` 的变异体本轮变成无结论"与"基线里的变异体本轮完全缺失"。
3. **无结论集合只对变化敏感**：集合本身进基线（`inconclusive`），门禁判它的**增长**。
   这一条正是"根因无解"那条分支所需要的机制；本包根因已修，机制保留，不再需要它兜底。
4. `reports/mutation_baseline.json` 的 `note` 必须写明：
   `inconclusive` 里**已证实至少有一条真盲区**（`is_expired__mutmut_9` 曾被误判为
   `segfault`），因此不得把它读成"不是盲区"。
