> 返回 [README](../README.md)

# 实现细节与踩坑

## 出错与停止

### 状态有两份，必须一起复位

`quick.running`（任务线程）和 `browser.running`（浏览器阶段）是两个独立字段，前端按

```js
busy = quick.running || browser.running
```

决定按钮状态。**任何一条退出路径都必须把两者都清掉**，否则界面会卡在「运行中…」而点停止又报「当前没有运行中的任务」。

异常可能发生在浏览器子进程启动之前（环境检查 / 解析问卷 / 生成答案），这时没有 `run_browser_engine` 的 `finally` 来复位，只能靠 `quick_worker` 自己的 `finally`：

```python
finally:
    with LOCK:
        S.browser_state.update(running=False, captcha=False, captcha_mode="")
        if S.browser_state["phase"] != "done":
            S.browser_state.update(phase="idle", current=0, round=0, rounds=0)
        S.proc = None
        S.quick_running = False
```

`/api/quick/stop` 另有一层兜底：任务已经结束但 `browser_state` 还挂着 `running` 时，清理并返回 `{ok: true, cleaned: true}`，而不是 400。

### 抓取失败的信息要能定位

`fetch_meta()` 原来把所有「没拿到 `divQuestion`」都归成「shortid 错误或问卷已关闭」，但实际更常见的是被限流后返回了一个提示页。现在分两步：

**重试**（默认 2 次，退避 1–2.4s）：同一条链接刚才能开、隔几十秒失败一次很常见，一次失败就让整个任务崩掉不合理。

**诊断**（`_diagnose_page`）：读返回页的标题和正文，判断是「问卷已停止收集 / 已关闭 / 需要密码 / 被要求输入验证码 / 请求过于频繁 / 返回空页面」中的哪一种，再拼进异常信息：

```
抓不到问卷页（zzzz9zzz，已试 3 次）：页面打开了（标题「无法显示网页」，1153 字节）但没有题目容器
```

### 短链必须先规范化

手填短链、粘整条链接、二维码里的链接，三种输入都要先收敛成短链本身（`wjx_autofill.normalize_shortid`）。少做这一步的后果：

```
输入  https://v.wjx.cn/vm/tKJc7Gq.aspx
拼出  https://v.wjx.cn/vm/https://v.wjx.cn/vm/tKJc7Gq.aspx.aspx
```

服务端把这条畸形路径当成不存在的问卷，返回一个提示页，报出来的错还像「被要求输入验证码」—— 而同一秒里「抓取并解析」是成功的（它有自己的提取逻辑），于是出现「抓取成功但开始作答失败」的怪现象。

现在 `fetch_meta()` 入口先规范化，再用 `^[A-Za-z0-9]{4,24}$` 校验，格式不对直接报「短链格式不对」，不浪费三次重试。

### 多选限项：解析、渲染、提交三层都要卡

**第一层：解析器**。问卷星不把限项写在属性里，只能从题干文案抠。这里有两个坑：

```python
# 题干是 get_text("\n") 拼出来的，提示常被拆成 "最多\n选择\n3项"
(re.compile(r"最多\s*[选择]{1,2}\s*(\d+)\s*项"), "max")   # \s 要能吃换行
```

另一处：限项有时只在 `span.qtypetip` 里（题干本身没写）。所以 `extract_limit()` 要同时吃 `prompt + type_hint`，并且先把 `qtypetip` 从树里 `extract()` 掉，否则提示会混进题干。

**第二层：前端**。`data-max` 取 `max_select || exact_select`；`checkMax()` 在 change 事件里超限就回滚本次勾选、抖动并提示。导入文件和后端回填不走 change，所以 `applyAnswers()` 末尾补一次 `trimOverLimit()`。

**第三层：后端**。`_answer_ok()` 校验多选不超过上限，不合法就丢弃该题改用随机值；`validate_answers()` 再查一遍。

### 提交成功与否，只看跳转，不看正文

原来的判断：

```python
body = self.page.content()
if "提交成功" in body or "感谢" in body:
    return {"ok": True, "url": url}
```

**`page.content()` 包含 `<meta property="og:description" content="…非常感谢">`**，所以任何一份问卷点完提交，只要等一秒就"成功"——哪怕页面根本没变。有未答题时问卷星不跳转，只把页面滚回那道题并填 `.errorMessage`，于是出现了「实际没提交却记成功」。

现在改成 `_page_state()`：

| 状态 | 判据 |
|---|---|
| `success` | URL 含 `complete` 或 `joinid=` |
| `unanswered` | 任一 `.errorMessage` 有文字，或 `#divQuestion` 里出现「请完成 / 未作答 / 还有题目 / 必答」 |
| `error` | `#ValError` / `.layui-layer-content` 等可见且有文字 |
| `pending` | 以上都不是 |

只看 `#divQuestion` 的 inner_text，不碰整页 HTML —— meta 就在 `<head>` 里。

另外提交前加了一道自检：`missing_required` 非空（必答题没填上，包括"压根没有答案可填"和"点了但抛异常"）就**不点提交按钮**，直接返回失败：

```
[警告] 必答题没填上：Q4、Q8、Q15、Q16
[第 1/1 轮] 结果 {'ok': False, 'msg': '必答题未填写：Q4、Q8、Q15、Q16（共 4 题），未提交'}
[总计] 成功 0 · 失败 1
```

点了也是白点，而且打回后的状态最容易被误判。

### 停止的响应时间

| 阶段 | 机制 | 实测 |
|---|---|---|
| 环境检查 / 解析问卷 / 生成答案 | `stop_event` + 各阶段前的检查点 | 0.3s |
| 浏览器填写 / 等待验证码 | `proc.terminate()` 直接终止子进程 | 1–3s |
| 轮间隔 | 倒计时按秒检查 `stop_event` | ≤1s |

验证码等待期间子进程不产出 stdout，只设 `stop_event` 会等到 45 秒超时才停 —— 这是为什么停止要直接 terminate 子进程。

---

## 滚动布局（踩过的坑）

页面是「宽屏锁视口 + 内部滚动，窄屏整页滚动」的双模式，改动时注意三条：

1. **滚动容器是 `html` 不是 `body`。** 只给 `body` 解 `overflow` 不会解除视口锁定，`html { overflow: hidden }` 会直接让整页滑不动。窄屏媒体查询里必须 `html { height: auto; overflow-y: auto }` 和 `body { overflow: visible }` 一起改。

2. **flex / grid 子项要显式 `min-height: 0`。** 默认 `min-height: auto` 等于内容高度，21 道题会把 `.answer-area` 和 `main` 撑到 6000px 以上，滚动容器拿不到高度，`overflow-y: auto` 形同虚设。

3. **不要用 `height: calc(100% - 58px - 186px)` 硬编码。** 顶栏换行或日志区高度变化就会错位，改成 `body { display: flex; flex-direction: column }` + `main { flex: 1 1 auto; min-height: 0 }` 由浏览器分配。

自查方法：浏览器控制台执行

```js
const q = document.querySelector('#questions');
console.log(q.clientHeight, q.scrollHeight);  // 前者必须明显小于后者
```

另外：Windows 的 `SO_REUSEADDR` 允许两个进程绑同一端口，旧进程会继续应答，表现为「代码改了但页面没变」。服务已设 `allow_reuse_address = False`，端口被占时直接报错。

## 日志乱码与「假成功」

两个报障都出在这两个地方，记一下。

### 日志乱码

Windows 上 Python 的 stdout 编码跟随系统 locale：中文系统 cp936、英文系统 cp437。
而 `server.py` 按 UTF-8 读子进程输出，两边对不上，网页日志栏就是一堆 `锟斤拷`。

本机看着正常、换台电脑就乱，多半是那台机器没开「使用 Unicode UTF-8 提供全球语言支持」。

修法是 `console.py`，每个能被单独执行的入口在 `main()` 第一行调用：

```python
def force_utf8():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    os.environ["PYTHONIOENCODING"] = "utf-8"   # 子进程继承
```

父进程按 UTF-8 读、子进程按 UTF-8 写，两端对齐。`PYTHONIOENCODING` 写进环境变量，
`server.py` 起的 `wjx_browser.py` 不用自己再猜一次。

实测对照（`PYTHONIOENCODING=gbk` 模拟旧环境）：

```
修复前  字节 b'\xd5\xe2\xd2\xbb\xd0\xd0...'     按 UTF-8 读 → ��һ�������ģ�����
修复后  字节 b'\xe8\xbf\x99\xe4\xb8\x80\xe8\xa1...' 按 UTF-8 读 → 这一行是中文：作者 Richard Huang
```

### 提交「假成功」

三个漏洞叠在一起，表现就是「题没填上也报成功」。

**1. 只看点击有没有抛异常，不看有没有真的选上。**

填题用的是坐标点击，落到别处、被浮层挡住、被前端校验拦下，都不会报错。
典型场景：Q8 限选 3 项，答案给了 4 项，第 4 次点击被前端静默拦下——
旧代码认为「点了 4 次没报错」→ 填好了 → 提交 → 服务端收下 3 项 → 记成功。

现在每题填完回读 DOM：

| 题型 | 选中标记（实测）|
|---|---|
| 单选 / 多选 | `input[type=radio\|checkbox]:checked`（input 本身 `display:none`）|
| 矩阵 | `a` 加 class `rate-on` |
| 填空 | 输入框 value |

不一致就重试，仍不一致记进 `missing_required`。

**2. 提交前不复核整页。**

逐题验证只管当场，后面题目的滚动、联动有可能把前面的答案挤掉。
`fill_all()` 结束和 `submit()` 开头各回读一次整页，缺哪题报哪题，直接不提交。

**3. 成功判据是字符串匹配。**

```python
if "结果 {'ok': True" in line:      # 老做法
    ok += 1
```

`wjx_browser.py` 的结果格式变一个字，统计就全错。

现在改成结构化输出：

```
[RESULT] {"ok": true, "url": "...", "joinid": "128153311680"}
```

父进程按 JSON 解析。同时 `ok: true` 必须带答卷编号才成立：

```
移动版完成页  ...completemobile2.aspx?activityid=xxx&joinactivity=128153311680
PC 版        ...&joinid=128153311680
```

两种都认，拿不到编号一律降级为失败。之前只认 `joinid`，把移动版的成功全判成失败——
这个 bug 是实测时撞出来的，改完当天就复现了一次。

实测三种场景：

| 场景 | 旧行为 | 现在 |
|---|---|---|
| 正常 16 题 | 通过 | 通过，0 误判 |
| Q8 给不存在的选项 99 | 重试后失败 | `必答题未填写：Q8（共 1 题），未提交` |
| Q8 给 4 项（上限 3）| **记成功** | `点完回读不一致` → 重试 → `必答题未填写：Q8，未提交` |

第三种就是「没作答却提交了」——现在在点提交之前就拦住了。

### 顺带修的：填空题答案被丢掉

`_answer_ok()` 用来过滤「换问卷后残留的答案」，但只写了单选/多选/矩阵三个分支，
末尾 `return False`。结果任何填空题答案都被判成非法丢弃，必答填空永远「未提供」：

```
merge_answers(survey, {"4": "测试"})  →  used: 0, dropped: ['4']
```

补上填空与下拉分支，未知题型返回 `True` 交给 `validate_answers` 统一判：

```
→ used: 3, dropped: []   校验通过
```

---