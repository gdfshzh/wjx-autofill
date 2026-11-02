# 问卷星批量作答控制台 —— 使用说明

作者：黄智皓
本项目完全免费，禁止商用和学术造假

浏览器界面的逐题作答工具。答题区与控制坞是一体的：勾选的答案、脚本正在填的题、运行状态都在同一屏内联动。后端零第三方依赖（标准库 HTTP 服务），前端纯 HTML/CSS/JS。

## Windows 免安装版（不装 Python）

目标机器什么都不用装，Win10 / Win11 x64 直接跑。

| 产物 | 体积 | 启动 | 说明 |
|---|---|---|---|
| `wjx_probe_win64.exe` | **69.8 MB** | 双击；首次约 15 秒，之后约 4 秒 | 单文件，首次把 160 MB 运行时解到 `_runtime/` |
| `wjx_probe_portable_win64.zip` | **59.4 MB** | 解压后双击 `启动.bat`，秒开 | 目录版，整目录可拷 U 盘 |

两个都自带 Python 3.13.12 和全部依赖，不读注册表、不写系统目录、不碰 PATH。

### 目录结构

```
wjx_probe/
  python\            官方 embeddable Python 3.13.12
    python.exe
    python313.dll
    python313.zip          标准库
    Lib\site-packages\     playwright / lxml / pillow / zxing-cpp / curl_cffi ...
  app\               项目本体
    server.py
    web\
  启动.bat           入口
```

`python313._pth` 里把 `Lib\site-packages` 加进 `sys.path`，wheel 直接解压进去，
**全程没经过 pip**——embeddable 包本来就不带 pip，也不需要。

### exe 的启动逻辑

```
双击 exe
  ├─ PyInstaller 把自身解到 %TEMP%\_MEIxxxxx（含 62 MB 的 bundle.zip，2~3 秒）
  ├─ 检查 exe 同目录 _runtime\.version
  │    版本一致        → 跳过解压
  │    不一致 / 不存在 → 解压 bundle.zip 到 _runtime（160 MB，约 8 秒）
  │                     升级时先读出 survey.json / fill_template.json，解完再写回
  └─ 启动 _runtime\python\python.exe _runtime\app\server.py
```

为什么不是每次都解压：158 MB 解一遍要 5~8 秒，而且会覆盖用户解析好的问卷。
所以解一次、按版本号判断，升级时才重解并保留数据。

参数：

| 参数 | 作用 |
|---|---|
| `--port 9000` | 换端口 |
| `--no-browser` | 不自动开浏览器 |
| `--doctor` | 只做环境自检（转给 `run.py doctor`） |
| `--rebuild` | 删掉版本标记，强制重解压一次 |

### 重新生成

```bash
python build_win64.py              # 目录版 + zip + exe，约 2.5 分钟
python build_win64.py --no-exe     # 跳过 PyInstaller，只出目录和 zip
python build_win64.py --refresh    # 重新下载 embeddable 与依赖
```

流程：下载 `python-3.13.12-embed-amd64.zip`（11 MB）→ 改 `_pth` →
把 `offline_deps/*.whl` 解到 `site-packages` → 复制源码 → 打 `bundle.zip` → PyInstaller 打包。

### 体积构成

```
playwright\driver\node.exe    90 MB    驱动层，省不掉
其余依赖（PIL/lxml/curl_cffi/zxing/bs4...）  50 MB
Python 标准库 + 解释器              13 MB
项目源码                            0.2 MB
----------------------------------------------
解压后 160 MB  →  压缩后 62 MB  →  exe 69.8 MB（含 PyInstaller 自身运行时）
```

playwright 的包自带一个 Node 驱动的浏览器协议层，`node.exe` 就是它。它不下载浏览器
（浏览器用系统的），但这 90 MB 省不掉，除非把整套浏览器控制重写成 CDP 直连。
之前把二维码从 OpenCV 换成 zxing-cpp，已经从包里砍掉 54 MB。

playwright 走 `channel="msedge"`，用系统自带 Edge，不下载 Chromium（省 150 MB）。
目标机器需要装 Edge 或 Chrome，Win10/11 默认带 Edge。

### 依赖清单

| 包 | 用途 | 必需 |
|---|---|---|
| beautifulsoup4 + lxml | 解析问卷 HTML | 是 |
| playwright | 真浏览器作答 | 否 |
| zxing-cpp + pillow | 二维码解码 | 否 |
| curl_cffi | 浏览器 TLS 指纹 | 否 |
| psutil | 并发探测内存测量 | 否 |

## 脚本版（需本机 Python）

目标机器已经有 Python 3.8+ 时，用这两个更小：

| 产物 | 体积 | 依赖怎么来 |
|---|---|---|
| `wjx_probe_offline.pyz` | 51.8 MB | 内置 wheel，缺依赖时离线安装，全程不联网 |
| `wjx_probe_all_in_one.py` | 130 KB | 缺依赖时联网 `pip install` |

### 打包与运行

```bash
python build_single.py --bundle-deps     # 离线版，依赖一起打进去
python build_single.py                   # 轻量版，只有一个脚本
```

运行：

```bash
python wjx_probe_offline.pyz
```

Windows 双击 `启动.bat` 即可，它会自动挑存在的那个，离线版优先。

`.pyz` 是 Python 标准的 zipapp 格式，`python 文件.pyz` 直接执行。离线版不用 `.py` 是因为：51.8 MB 的数据如果塞进 base64 字符串常量会变成 69 MB，白涨 33%。zipapp 把 `payload.zip` 原样存进外层 zip，体积不膨胀，启动也不用先解 base64。

### 参数

| 参数 | 作用 |
|---|---|
| `--port 9000` | 换端口；被占用时自动顺延并提示实际端口 |
| `--no-browser` | 不自动打开浏览器 |
| `--doctor` | 只做环境自检，不启动服务 |
| `--rebuild` | 强制重写运行时里的代码文件 |
| `--reset-data` | 连 `survey.json` 与模板一起重置 |
| `--clean` | 清掉运行时（保留问卷与模板）后退出 |
| `--skip-install` | 不自动安装依赖 |

打包器参数：

| 参数 | 作用 |
|---|---|
| `--bundle-deps` | 下载依赖 wheel 并打进包 |
| `--refresh-deps` | 重新下载（依赖清单变了之后用） |
| `--deps-dir DIR` | wheel 缓存目录，默认 `offline_deps/` |

### 内置依赖包

`--bundle-deps` 先跑 `pip download` 把 wheel 下到 `offline_deps/`，再打进包里：

```
beautifulsoup4 4.15.0      0.10 MB      playwright 1.64.1         37.00 MB
certifi 2026.10.23         0.13 MB      psutil 7.2.2               0.13 MB
cffi 2.1.1                 0.18 MB      pycparser 3.12             0.06 MB
curl_cffi 0.17.0           2.01 MB      pyee 14.0.0                0.01 MB
greenlet 3.5.6             0.31 MB      soupsieve 3.0.4            0.04 MB
lxml 6.1.3                 3.82 MB      typing_extensions 4.16.0   0.04 MB
pillow 13.0.0              6.95 MB      zxing_cpp 3.1.1            0.96 MB
-----------------------------------------------------------------------------
合计 14 个                                                           51.7 MB
```

启动时只装缺的那几个：

```
[2/3] 检查依赖
      [警告] zxing-cpp   缺失（二维码解码）· 可选
      离线安装（内置 14 个 wheel，不联网）：zxing-cpp
Defaulting to user installation because normal site-packages is not writeable
Looking in links: ...\_runtime\wheels
Processing .\_runtime\wheels\zxing_cpp-3.1.1-cp313-abi3-win_amd64.whl
Successfully installed zxing-cpp-3.1.1
      [OK]   zxing-cpp 已安装
```

注意 `Looking in links` 而不是 `Looking in indexes` —— `--no-index --find-links` 把索引关了，pip 根本不会访问 PyPI。装不了的极端情况（比如 wheel 损坏）才会回退联网，并打印 `内置 wheel 装不上，转联网安装`。

**wheel 绑定 Python 版本**。lxml / zxing-cpp 这些是 C 扩展，文件名里的 `cp313` 就是 ABI 版本。打包时把版本写进包里，运行时不一致会明确说明并改走联网：

```
[警告] 内置依赖包 14 个 wheel，但内置 wheel 是 Python 3.11 的，当前是 3.13，缺依赖时走联网
```

纯 Python 包（playwright、beautifulsoup4）其实跨版本通用，但二进制包不行，所以整体按版本判断，不做混合——半装一半装不上比全走联网更难查。

### 为什么换成 zxing-cpp

最初用 `opencv-python` 解二维码，它加 numpy 一共 54 MB，占离线包一半以上。换成 `zxing-cpp` + `Pillow` 只要 7.9 MB，识别率反而更好——`cv2.QRCodeDetector` 对低分辨率截图经常认不出来。

`qr_decode.py` 统一了两个后端：优先 zxing-cpp，机器上装了 cv2 就自动回退，都没有时抛出的错误里直接带安装命令。前端上传二维码、`run.py qr` 命令行都走同一个入口。

### 依赖清单

| 包 | 用途 | 必需 |
|---|---|---|
| beautifulsoup4 + lxml | 解析问卷 HTML | 是 |
| playwright | 真浏览器作答 | 否 |
| zxing-cpp + pillow | 二维码解码 | 否 |
| curl_cffi | 浏览器 TLS 指纹 | 否 |
| psutil | 并发探测内存测量 | 否 |

必需包装不上会直接退出并打印手动安装命令；可选包缺失只降级对应功能，服务照常启动。

### 运行时目录

默认是单文件旁边的 `_runtime/`，目录不可写时退到 `%LOCALAPPDATA%\wjx_probe_runtime`。

- 代码文件（`*.py`、`web/*`）内容不同才写，重新打包后下次启动立即生效
- 数据文件（`survey.json`、`fill_template.json`）只在不存在时写入，解析过的问卷和配好的模板不会被冲掉
- `wheels/` 里的 wheel 只判断存在与否，不逐个比对内容——52 MB 每次读一遍会让启动卡住
- `--clean` 只删代码，问卷与模板保留

### 启动弹窗

每次打开页面都会出现，显示作者、授权与限制三行，不写 localStorage，刷新即重现。点「我已知悉」、点遮罩空白处或按 Esc 关闭；日志栏里另有一行常驻签名。文案由 `GET /api/about` 提供，页面里只留静态兜底。

### 双击 `启动.bat` 报「错误码 9009」

`9009` 是 cmd 的「找不到命令」，含义是 `python` 这个命令执行不了，不是脚本内部的错。两种成因：

1. PATH 里只有 Microsoft Store 的 `python.exe` 占位符（0 字节），执行即返回 9009
2. 装了 Python，但安装时没勾选 "Add python.exe to PATH"

`启动.bat` 现在按顺序实测，任一步通了就用它：

```
py -3 -c "import sys"
where python 的每一行 → 逐条跑 -c "import sys"     ← 商店占位符会在这里被筛掉
%LOCALAPPDATA%\Programs\Python\Python3xx\python.exe
%ProgramFiles%\Python3xx\python.exe 与 C://Python3xx//python.exe
注册表 HKCU/HKLM\Software\Python\PythonCore 下的 ExecutablePath
```

全都不通才打印安装指引。启动时先回显 `解释器：<路径>` 和 `主程序：<文件名>`，出问题时有据可查。

`.bat` 本身用 **GBK 编码**写出、开头 `chcp 936`。cmd 是按当前代码页逐行解析批处理文件的，UTF-8 的中文在 936 下会在多字节边界裂开，后半句被当成命令执行，表现是 `'xx' is not recognized` 再加一个 9009 —— 这个坑比缺 Python 更隐蔽，注释行也会中招。

## 统一入口

```bash
python run.py doctor                  # 环境自检：依赖 / 指纹 / 问卷可达性
python run.py qr --image qr.jpg       # 二维码解码，只取链接
python run.py parse --url <链接>       # 抓页面解析出 survey.json
python run.py fill --url <链接>        # 真浏览器逐题填写（可见，最像人）
python run.py batch --url <链接> --count 5 --gap 60   # HTTP 批量
python run.py pipeline --qr qr.jpg    # 二维码 -> 解析 -> 答案 -> 提交 一条龙
python run.py gui                     # 网页控制台
python run.py probe --levels 1,2,4,6  # 探测本机并行上限
python run.py par --job "OOHKkG8:3" --workers 3   # 并行作答，见 PARALLEL.md
```

各文件分工：

| 文件 | 作用 |
|---|---|
| `run.py` | 统一命令行入口 |
| `parse_wjx.py` | 页面解析 → `survey.json` |
| `qr_decode.py` | 二维码解码，zxing-cpp 优先、OpenCV 回退 |
| `wjx_autofill.py` | HTTP 会话、签名、提交 |
| `humanize.py` | 浏览器指纹、资源预热、人类节奏模型 |
| `wjx_browser.py` | Playwright 真浏览器填写（鼠标轨迹 / 打字 / 滚动） |
| `server.py` + `web/` | 网页控制台 |
| `probe_parallel.py` | 探测本机可并行的浏览器会话数 |
| `parallel.py` | 并行作答（多问卷 / 多轮），见 `PARALLEL.md` |
| `launcher_template.py` | 单文件启动器模板（含依赖自检、解包、启动） |
| `build_single.py` | 打包器，生成 `wjx_probe_all_in_one.py` 与 `启动.bat` |

## 启动

```bash
cd wjx_probe
python server.py
# 默认 http://127.0.0.1:8765 ，自动打开浏览器
```

可选参数：

```bash
python server.py --port 9000      # 换端口
python server.py --no-browser     # 不自动开浏览器
```

启动时自动读取同目录下的 `survey.json`；没有就从顶栏填短链或上传二维码抓取。

---

## 一、界面结构

从上到下三块，没有别的面板：

```
┌─ 顶栏 ── 品牌 · [📷 二维码] [短链] [抓取并解析] · 问卷标题 ─┐
├─ 答题区 ─────────────────────────────────────────────────┤
│  答题区  18/18   [填空题模板] [随机][首项][末项][清空][导入][导出] │
│  ● 逐题填写中  1/3  Q11 · …  [每轮随机] 次数 间隔 [开始][停止] │
│  ─────────────── 进度条 ─────────────────────────────────  │
│  Q1 …  Q2 …  Q3 …（题目卡片，独立滚动）                    │
├─ 日志 ────────────────────────────────────────────────────┤
│  22:20:34  第 1/1 轮 成功  joinid=...                     │
└──────────────────────────────────────────────────────────┘
```

原来的「一键运行四步条」和右侧「批量提交面板 / 执行顺序 / 提交内容」全部移除。那几块和浏览器作答没有关系，分散了操作入口：跑一次要同时看顶栏步骤条、右侧卡片、下方进度、日志四处。现在只有一条通道。

---

## 二、答题区与控制坞怎么联动

这是本次的核心。两者不是「两个面板互相通知」，而是同一份状态的两个视图。

| 联动点 | 表现 |
|---|---|
| **当前题** | 脚本正在填 Q11 → 控制坞显示 `Q11 · 您更倾向的…`，同时答题区把 Q11 滚到视野中并加蓝色边框高亮。两处指向同一道题，题号只有一处来源（`browser_state.current`） |
| **进度** | 进度条 = 已填题数 / 总题数，跟着当前题推进；填到矩阵题也计一格（`_matrix` 会打一条 `点击 Q21` 日志） |
| **答案来源** | 点「开始作答」时把**答题区当前勾的答案**提交给后端。脚本不再自己随机一套、界面另显示一套 |
| **缺题补齐** | 只勾了 15 题也能开始，剩下 6 题由后端随机补齐；补齐后的完整答案在运行结束时回填界面，界面显示的就是刚提交的 |
| **问卷不符** | 界面答案带问卷指纹（题干+题型+选项的 MD5）。后端解析出的问卷指纹不同时，旧答案整份作废并在日志里说明，不会把 A 问卷的答案错填进 B 问卷 |
| **运行中锁定** | 运行期间答题区变暗且不可点击，工具按钮与次数/间隔输入框一并禁用。改了不生效比锁住更让人困惑 |
| **停止** | 「停止」直接终止浏览器子进程，2 秒内生效；验证码等待期间也能立即停（之前要等 45 秒超时） |

### 状态灯只有一套

控制坞左侧那颗灯是唯一的状态指示，由两路数据合成（`buildView()`）：

| 灯 | 文案 | 来源 |
|---|---|---|
| 灰 | 未开始 / 已停止 | 无任务 |
| 蓝（呼吸） | 正在打开浏览器 / 逐题填写中 / 提交中 / 轮间隔等待 | `browser/status.phase` |
| 蓝（呼吸） | 检查运行环境 / 解析问卷 / 生成答案 | `quick/status` 里 running 的那一步 |
| 黄 | 验证码拦截 / 等待你点验证 | `browser/status.captcha` |
| 绿 | 本轮完成 | `phase=done` |
| 红 | 失败 | `quick/status.result.ok === false` |

「检查环境 → 解析问卷 → 生成答案」这三步在浏览器起来之前跑，各不到 1 秒。它们不再占一条独立的步骤条，而是复用同一个状态位显示。

---

## 三、题型与交互

| 题型 | 控件 | 约束 |
|---|---|---|
| 单选 | 圆形单选钮 | 同题互斥 |
| 多选 | 方形复选框 | 超过限项时**自动回滚本次勾选**、抖动并提示；限项从题干或提示角标解析，写在题号后 `Q8 [多选 · 最多3项]` |
| 矩阵量表 | 每行一个下拉 | 每行独立，点行内任意位置聚焦该行下拉 |
| 填空(单行) | 单行输入框 | 必答且无来源时点「开始作答」会被拦下，见第四节 |
| 填空(多行) | 多行文本框 | 同上；脚本逐字符键入，间隔 55–190ms |
| 下拉 | 原生 select | 脚本已支持 |

工具按钮：`随机填充`（多选项数在限项内随机抽样）、`全部首项`、`全部末项`、`清空`、`导入`、`导出`（`answers.json`）。

---

## 四、填空题模板

填空题不能靠随机生成（编不出真实城市名），所以单独给一个面板集中管理候选答案。

### 入口与结构

答题区工具条左侧的 **`填空题模板 ①`**，角标是已配置候选的题数。点开从右侧滑出抽屉：

```
┌ 填空题模板                    关闭 ┐
│ 每行一个候选答案。批量填写时按题分别取值 │
│ ┌────────────────────────────────┐ │
│ │  ●   每轮随机取值                │ │   ← 开关
│ └────────────────────────────────┘ │
│ 关闭：每轮都填第一个候选，多轮答案完全一致 │   ← 文案随开关变
│                                    │
│ ┌ Q5  您目前就读城市 ─────────────┐ │
│ │ 北京                            │ │   ← 一行一个候选
│ │ 上海                            │ │
│ │ 广州                            │ │
│ │ 杭州                            │ │
│ │ 4 个候选                        │ │
│ └────────────────────────────────┘ │
│                                    │
│ 1 题 / 4 个候选        [ 保存模板 ] │
└────────────────────────────────────┘
```

抽屉按 Esc 或点遮罩关闭。运行期间「填空题模板」按钮和其余工具一起禁用。

### 模板与题目的对应

**按 `topic`（题号）绑定，不按题干文本。** 数据落在 `fill_template.json`：

```json
{
  "sig": "db59505263b1",
  "random": false,
  "items": { "5": ["北京", "上海", "广州", "杭州"] }
}
```

- `items` 的 key 是题号，value 是候选数组
- `sig` 是保存时那份问卷的指纹（题干+题型+选项的 MD5）
- 再次打开时若当前问卷 `sig` 与模板不符：抽屉里提示「模板是按上一份问卷配的，请核对题号」，工具条上的按钮描一圈黄边，**运行时整份不套用**并在日志里说明

  为什么是整份作废而不是按题号过滤：换了问卷后 Q5 还是 Q5，但题干完全可能是另一道题，把旧问卷的「北京/上海」填进新问卷的 Q5 是静默错位，比报错更糟。

答题区里的填空题输入框显示候选的第一项并置灰，右侧挂 `模板 4 候选` 标签。**这里的值不会被直接提交**，只是让你一眼看到「不随机时会填什么」。

### 取值逻辑

`server.py: build_round_answers()` 在启动任务时把一份答案展开成 `count` 份：

```python
for _ in range(count):
    ans = dict(base)
    for t, pool in pools.items():
        if random_fill:
            bag = bags.get(t) or []
            if not bag:                 # 用完了重新洗
                bag = list(pool); random.shuffle(bag)
            val = bag.pop()
        else:
            val = pool[0]               # 永远第一项
        ans[t] = val
```

随机用的是**洗牌队列**，不是每轮独立 `random.choice()`。独立随机会出现「连续三轮同一个城市」，而「多轮答案各不相同」要的是覆盖，不是概率上应该不同。抽空一轮后重新洗牌，所以候选 N 个时，前 N 轮保证不重样。

多题互不干扰：每题一个队列，Q5 抽到第 2 个、Q6 抽到第 3 个互不影响。

### 开关差异

| | 关闭（默认） | 开启 |
|---|---|---|
| 每轮取值 | 候选[0] | 洗牌队列依次取 |
| 4 轮实际结果 | 北京 / 北京 / 北京 / 北京 | 杭州 / 上海 / 北京 / 广州 |
| 多轮答卷 | 内容完全相同 | 候选够多时全不同 |
| 适用 | 验证流程、造一条固定样本 | 造多样化数据 |

实测日志（4 轮、4 个候选）：

```
生成答案: done 界面 17 题 + 补齐 1 题（含模板 1 题）
填空题模板生效（每轮随机）：1 题
  第 1 轮 Q5=杭州
  第 2 轮 Q5=上海
  第 3 轮 Q5=北京
  第 4 轮 Q5=广州
```

### 与答卷数据的关系

每轮用的值在**服务端**就定好了，通过 `_answers_run.json` 的 `{"rounds": [...]}` 传给浏览器子进程，子进程按 `rounds[i-1]` 取。好处是日志能看到每轮到底填了什么，而不是「浏览器里现抽、事后无从追查」。

`wjx_browser.py` 同时兼容两种格式：

```json
{"1":"2","5":"北京"}                   // 单份，每轮都用
{"rounds":[{...},{...}]}              // 多轮，第 i 轮取 rounds[i-1]
```

### 必答填空的拦截

模板和答题区都没给值时，点「开始作答」会被拦下并滚动到那道题：

```
Q5「您目前就读城市」是必答填空，请直接填写或配到填空题模板里
```

不塞占位符（比如「未填写」）——那会污染问卷数据，宁可让人补一下。

---

## 五、每轮随机答案

控制坞参数行最左边有个滑块开关「**每轮随机**」：

```
● 逐题填写中  1/3  Q7 · …     [每轮随机 ●] 次数 [3]  间隔 [60] s   [开始作答] [停止]
```

开启后**单选题 / 多选题 / 矩阵量表每轮重新抽样**，多轮提交的答卷不再是同一份的复制。关闭时每轮都用答题区当前的答案。

开关状态存 `localStorage`，刷新后保持上次选择。

### 两个随机维度是独立的

| 开关 | 管什么 | 数据来源 |
|---|---|---|
| 控制坞「每轮随机」 | 单选 / 多选 / 矩阵量表 | 从题目选项里重新抽，受限项约束 |
| 模板抽屉「每轮随机取值」 | 填空题 | 从你配的候选池里按洗牌队列取 |

可以只开一个：选择题随机 + 填空固定，或者反过来。

### 开与关的差别

实测（学校版 17 题，界面答案 Q1=2、Q2=3）：

```
关闭：第1轮 Q1=2 Q2=3 Q3=3     第2轮 Q1=2 Q2=3 Q3=3     第3轮 Q1=2 Q2=3 Q3=3
开启：第1轮 Q1=3 Q2=3 Q3=2     第2轮 Q1=3 Q2=3 Q3=3     第3轮 Q1=2 Q2=2 Q3=1
```

三条约束保证随机的答案仍然合法（`random_answers()` 负责）：

- 多选不超过题干限项（`最多选N项` / `限选N项`）
- 必答题全部覆盖
- 矩阵每行都落在 1~量表上限

开启后答题区的勾选**不再作为提交内容**，只作为参考。答题区标题旁会出现蓝色标签「每轮随机 · 勾选仅供参考」，避免误会。

### 多选项数的分布

原来多选的项数是 `random.randint(1, 选项数)` 均匀分布，8 个选项里勾 7 个的概率和勾 3 个一样高 —— 真人不会这么填，样本一眼假。改成峰值在区间 1/3 处的三角分布：

```
Q9（7 个选项）  1项 1.5%  2项 18.5%  3项 29.8%  4项 26.8%  5项 13.5%  6项 8.8%  7项 1.2%
```

「随机填充」按钮用的是同一个函数，所以手填时也更自然了。

### 运行结束回填哪一轮

展开成 N 份后，回填界面的是**第 1 轮实际提交的内容**（`rounds[0]`），不是展开前那份基准 —— 这样界面显示的就是真提交过的答案，而不是「理论上会提交」的。

---

## 六、参数

控制坞里两个数字，运行时禁用：

| 参数 | 默认 | 范围 | 含义 |
|---|---|---|---|
| **次数** | 3 | 1–200 | 开几轮。每轮新建独立浏览器上下文（新 cookie、新 nonce），相当于一份全新答卷 |
| **每轮随机** | 关 | 开 / 关 | 开启后选择题每轮重抽，见第五节 |
| **间隔** | 60 | 0–7200 秒 | 上一轮结果返回后开始倒计时，结束后才开下一轮 |
| 代理 | 空 | — | 窄屏隐藏，宽屏可填 `http://host:port` |

「无头运行」开关已移除：无头模式验证码几乎必弹且人工无法接管，只保留有头。需要无头时走命令行 `wjx_browser.py --headless`。

---

## 七、验证码

触发后控制坞整块变暖色，进度条变黄，下方出现提示条：

```
⚠ 脚本正在自动尝试通过验证码，若弹窗一直不走，请到浏览器窗口里点一下
```

脚本自己会先点三轮（带贝塞尔轨迹、落点随机）。点不过就转成「等待你点验证」，此时你在弹出的浏览器窗口里点一下，脚本检测到 `isCaptchaValid` 变真会自动继续，最多等 5 分钟。

---

## 八、接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/survey` | `{survey, shortid, sig}`，`sig` 是问卷指纹 |
| POST | `/api/survey` | `{shortid, proxy}` 抓取并解析，落盘 `survey.json` |
| POST | `/api/quick/start` | `{shortid?, qr_base64?, answers, survey_sig, count, gap, proxy, engine, headless, choice_random}` |
| POST | `/api/quick/stop` | 停止（会 terminate 浏览器子进程） |
| GET | `/api/quick/status` | `{running, steps, result}`，result 含 `answers` 与 `sig` |
| GET | `/api/browser/status` | `{running, phase, current, total, round, rounds, captcha, captcha_mode}` |
| GET | `/api/batch/status?since=N` | 增量日志（界面只用它的 logs 部分） |
| GET | `/api/fill/template` | `{items, random, sig, current_sig, stale}` 填空题模板 |
| POST | `/api/fill/template` | `{items:{"5":["北京","上海"]}, random}` 保存并落盘 |
| GET | `/api/answers/random` | 随机答案，每次重新抽样 |
| GET | `/api/answers/template` | 固定首项模板 |
| POST | `/api/answers/preview` | 按答案拼 `submitdata` |
| POST | `/api/batch/start` `pause` `stop` | HTTP 批量接口，界面已不再使用，命令行/外部调用仍可用 |

---

## 九、文件

| 文件 | 作用 |
|---|---|
| `server.py` | HTTP 服务 + 任务调度 + 答案合成 |
| `web/index.html` / `style.css` / `app.js` | 界面（答题区 + 控制坞 + 日志） |
| `wjx_browser.py` | Playwright 逐题填写、验证码处理 |
| `wjx_autofill.py` | HTTP 会话、签名、提交 |
| `parse_wjx.py` | 页面解析、题型兼容性检查 |
| `survey.json` | 问卷结构（启动时自动加载） |
| `fill_template.json` | 填空题模板：`{sig, random, items:{题号:[候选]}}` |
| `REPORT.md` / `DETECTION.md` | 协议逆向、风控分析 |
| `PARALLEL.md` | 并行粒度、并发探测、两种执行模式的实测对比 |

---

## 十、运行环境

- Python 3.8+，标准库即可跑服务
- `beautifulsoup4` + `lxml`：解析页面
- `playwright`：浏览器填写（用系统 Edge，无需下载浏览器）
- `curl_cffi`：HTTP 模式的 TLS 指纹

```bash
python -m pip install beautifulsoup4 lxml playwright curl_cffi
```

---

## 十一、出错与停止

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

## 十二、滚动布局（踩过的坑）

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

## 十三、日志乱码与「假成功」

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
修复后  字节 b'\xe8\xbf\x99\xe4\xb8\x80\xe8\xa1...' 按 UTF-8 读 → 这一行是中文：作者 黄智皓
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

## 十四、选择题选项概率（按轮次）

给每道选择题的每个选项设权重，并按轮次分别启用，用来做目标分布取样。
入口在答题区工具条「选项概率」，和填空题模板同级。

### 数据结构

落盘文件 `choice_weights.json`（运行目录下，原子写）：

```json
{
  "version": 1,
  "sig": "6827c3c49210",
  "updated": "2026-11-02 17:35:00",
  "rounds": {
    "1": {
      "8": {
        "v": 1,
        "sig": "1d4fd549b6",
        "mode": "multi_target",
        "weights": {"1": 0.7, "2": 0.6, "3": 0.3, "4": 0.2, "5": 0.1, "6": 0.1},
        "count": null,
        "fit_error": 0.000099
      }
    },
    "2": { }
  }
}
```

- 顶层 `sig` 是问卷指纹（题干 + 题型 + 选项），换问卷整份作废。
- 每题 `sig` 是题目选项指纹，选项集合变了只丢这一题，不影响同轮其他题。
- `rounds` 的键是界面上的轮次号（1 起），值是独立对象；复制配置走深拷贝，
  改第 2 轮不会反向影响第 1 轮。
- 未出现的轮次/题目 = 未配置，走原来的均匀随机逻辑。

### 四种 mode

| mode | 适用 | weights 含义 | 抽样 |
|---|---|---|---|
| `single` | 单选 / 下拉 | 相对权重，保存时归一化 | 一次加权抽取，权重即出现概率 |
| `multi_target` | 多选 | 每个选项的目标出现概率（0-1） | 在满足限项的全部组合上做 IPF，按组合分布抽 |
| `multi_weight` | 多选 | 相对权重 | 先定项数，再按权重不放回抽 |
| `matrix` | 矩阵量表 | 分值 1..N 的分布 | 每行独立抽一次 |

`multi_target` 的概率之和等于平均选中项数：上限 3 项、6 个选项时，
每项 27.8% 对应平均选 1.67 项。目标和落在项数区间端点时会被收紧到固定项数，
例如目标和 2.0 且上限 2 项，则每轮必选 2 项。

选项数超过 12 时组合数太大（2^12 以上），自动退化为 `multi_weight` 并给出警告。

### 校验规则

保存与预览都走 `choice_weights.validate_config()`：

- 选项值必须在当前题目里，负数和 NaN 直接拒绝；
- 全部为 0 拒绝；
- 单选的权重和自动归一化到 1；
- 多选目标概率单项 >100% 拒绝；
- 目标和与限项矛盾时自动缩放到最近边界，并返回 `warnings`；
- 缩放后单项 >100% 判为不可行（例如固定选 3 项，目标和 2.2 被拉到 3，
  某项会被推到 136%），直接拒绝并说明原因；
- 固定选 N 项但正权重选项不足 N 个，拒绝。

### 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/weights` | 取整份配置、当前问卷指纹、`stale`、统计 |
| POST | `/api/weights` | `action=set/clear/copy` |
| POST | `/api/weights/preview` | 按草稿或已存配置跑蒙特卡洛，返回目标 vs 实际 |

`action=set`：

```json
{"action":"set","round":2,"topic":"8",
 "config":{"mode":"multi_target","weights":{"1":0.7,"2":0.6},"count":null}}
```

`config` 为空或 `clear:true` 等于删除该轮该题。`action=clear` 支持
`{round, topic}`、`{round}`、`{all:true}` 三种粒度。
`action=copy` 支持 `{from:1,to:[2,3]}` 或 `{from:1,to:"all",count:5}`；
再加 `topic` 时只复制该题（目标轮次里的其他题保持不变），不带 `topic` 才是整轮复制。

`preview` 请求体：

```json
{"topic":"8","config":{...},"samples":600}
```

不传 `config` 时用 `round + topic` 读已存配置。返回：

```json
{"ok":true,"target":{"1":0.7},"actual":{"1":0.697},
 "mean_count":1.99,"count_hist":{"1":1146,"2":1731,"3":1123},
 "fit_error":0.000099,"warnings":[]}
```

多选的 `actual` 分母是轮次数，不是勾选格总数 —— 用后者会把 70% 显示成 35%。

### 前端交互

1. 打开抽屉 → `GET /api/weights`，按当前轮次渲染题目列表与已配置圆点。
2. 选题 → 从 `rounds[round][topic]` 取配置；没有就用默认值
   （单选取 `100/n`，多选目标取「期望项数 / 选项数」）。
3. 改数字或拖滑块 → 本地更新草稿，380ms 防抖后调 `preview`，
   用返回的 `actual` 画第二根条，并显示与目标的最大偏差。
4. 切轮次/切题 → 先确认丢弃未保存修改；轮次切换保持当前题目，方便逐轮对比。
5. 保存 → `POST /api/weights`，服务端归一化/校验后返回整份配置，
   前端用返回值重建列表、徽标和统计。
6. 运行 → `quick_worker` 重新从内存（已落盘）读配置，按 `cur_sig` 过滤，
   编译成 `{轮次: {topic: QuestionSampler}}`，交给 `build_round_answers()`。

### 与「每轮随机」的关系

概率配置优先级更高：某题在某轮有配置，就按配置抽，即使「每轮随机」是关的；
没配置的题仍按开关走（关=用答题区答案，开=每轮随机）。
这样「只给 Q8 的第 2 轮设分布」不会强迫其他题也重抽。

### 自检

```bash
python selftest_weights.py
```

覆盖单选经验分布、多选目标边缘概率、限项、0 权重、矩阵、非法输入、
轮次隔离、落盘读取，共 30 项。

## 十五、题目文本复制

答题区工具条「复制题目」：把当前 `survey.json` 里的题目整理成
「题号 + 题型标注 + 选项」的纯文本，点一次自动写剪贴板，同时弹预览框可改后再复制。

### 输出格式

```
1.您的年级是 [单选题]
A.2007级
B.2006级
2.请对学校的安全管理制度的满意度作个评价 [量表题]
很不满意
不满意
3.请对学校的安全管理制度的满意度作个评价 [矩阵题]
很不满意 不满意 一般 满意 很满意
寝室里面用电安全管理
食堂就餐安全管理
4.请按您认为的安全要素的重要程度排序 [排序题]
用电安全
食堂就餐安全
5.您觉得我们学校有哪些安全措施令您觉得很有安全感
```

规则：

- 题号统一重排为 `1..N`，原题干自带的 `1.`、`1、`、`（2）`、`第3题`
  会先剥掉，避免分段后重复出现 `1.`。
- 题型标注：单选 `[单选题]`、多选 `[多选题]`、量表 `[量表题]`、
  矩阵 `[矩阵题]`、排序 `[排序题]`、下拉 `[下拉题]`、矩阵填空 `[矩阵填空题]`；
  普通填空不标，导入端靠「没有选项」识别。
- 单选/多选/下拉的选项统一加 `A.` `B.` 前缀；选项文本里原本带的
  `A.`、`A、`、`(A)` 会先剥掉，不会出现 `A.A.xxx`。
- 量表/排序一行一个值，不加字母；矩阵先输出刻度（空格分隔），再一行一个子项。
- 题干末尾空的 `（）` 会清掉；题干中间的标点原样保留。
- 题与题之间不插空行，与样例一致；文本末尾带一个换行。

### 接口

`GET /api/survey/export`

```json
{"ok": true, "title": "...", "shortid": "OOHKkG8",
 "text": "1.您的性别: [单选题]\nA.男\n...", "count": 16,
 "warnings": ["第 2 题（量表/滑块）没有解析到选项"]}
```

只格式化当前内存里的 `S.survey`，不重新抓页面 —— 避免复制动作把正在作答的
`survey.json` 覆盖掉。要抓新问卷仍走顶栏「抓取并解析」。

### 解析侧补充

`parse_wjx.py` 之前只给单选/多选取 `options`。量表（type 5）和排序（type 8）
在移动端同样是可选项列表，现在也调用 `parse_options()`；矩阵填空（type 11）
也走 `parse_matrix()`。取不到时 `warnings` 会指出题号，文本仍保留题干，不会整份失败。

### 前端

- `web/app.js` 的 `exportSurveyText()`：GET 接口 → 填 textarea → 尝试
  `navigator.clipboard.writeText()` → 被拦截时退回隐藏 textarea +
  `document.execCommand('copy')`，仍失败就全选让用户按 Ctrl+C。
- 预览框可编辑；「复制到剪贴板」复制当前 textarea 内容，「重新生成」重新拉一次。

### 自检

```bash
python selftest_survey_text.py
```

用用户给的样例逐字比对，另外覆盖题号清洗、选项前缀去重、空括号、
缺选项警告，共 9 项。

## 十六、Git 版本管理

仓库已初始化，主分支 `main`，当前版本打了标签 `v1.2.0`。

```bash
git log --oneline --decorate
git status
git diff
```

### 入库范围

`git ls-files` 只包含源码、前端、文档和自检脚本，共 29 个文件。

以下内容在 `.gitignore` 里，不进仓库：

- 构建产物：`wjx_probe_win64.exe`、`wjx_probe_offline.pyz`、
  `wjx_probe_portable_win64.zip`、`wjx_probe_all_in_one.py`、`bundle.zip`；
- 运行时目录：`_runtime/`、`_portable/`、`_emb/`、`_pyi/`、`offline_deps/`；
- 运行数据：`survey.json`、`choice_weights.json`、`fill_template.json`、
  `answers*.json`、`cookies.txt`；
- 抓下来做分析的 `page_mobile.html`、`jqmobo2.js` 等。

发布产物用 `python build_single.py --bundle-deps` 与
`python build_win64.py` 重建，不需要占 Git 历史。

### 回滚

```bash
# 看某次提交改了什么
git show <commit>

# 单个文件回到某个版本
git checkout v1.2.0 -- server.py

# 整个工作区回到标签（会丢弃未提交修改）
git reset --hard v1.2.0
```

### 提交约定

- `feat:` 新功能
- `fix:` 修 bug
- `docs:` 文档
- `chore:` 构建、依赖、仓库维护
- `test:` 自检脚本

改完一个可验证的单元就提交一次，不把多个无关改动混在同一个 commit 里。
