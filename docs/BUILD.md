> 返回 [README](../README.md)

# 构建与打包

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