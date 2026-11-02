@echo off
rem 936 = 简体中文；本文件也是 GBK 编码，两者对齐才不会截断中文
chcp 936 >nul
title 问卷星批量作答控制台
cd /d "%~dp0"
setlocal enabledelayedexpansion

echo.
echo   ============================================
echo     问卷星批量作答控制台
echo     作者：黄智皓
echo     本项目完全免费，禁止商用和学术造假
echo   ============================================
echo.

set "PY="

rem ---- 1) 官方 py launcher，装在 C:\Windows，不受 PATH 顺序影响
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
  set "PY=py -3"
  goto :pick
)

rem ---- 2) 逐个实测 PATH 里的 python
rem      Microsoft Store 的 python.exe 是 0 字节占位符，执行即返回 9009。
rem      所以不能只用 where 判断「存在」，必须真跑一次 -c 才知道能不能用。
for /f "delims=" %%P in ('where python 2^>nul') do (
  "%%P" -c "import sys" >nul 2>nul
  if not errorlevel 1 (
    set "PY=%%P"
    goto :pick
  )
)

rem ---- 3) 常见安装目录（官方安装器 + 用户级安装）
for %%D in (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python39\python.exe"
  "%ProgramFiles%\Python313\python.exe"
  "%ProgramFiles%\Python312\python.exe"
  "%ProgramFiles%\Python311\python.exe"
  "%ProgramFiles%\Python310\python.exe"
  "C:\Python313\python.exe"
  "C:\Python312\python.exe"
  "C:\Python311\python.exe"
  "C:\Python310\python.exe"
) do (
  if exist %%D (
    set "PY=%%D"
    goto :pick
  )
)

rem ---- 4) 注册表里的安装记录（前三种都漏掉时兜底）
for %%R in (HKCU HKLM) do (
  for /f "tokens=2,*" %%A in ('reg query "%%R\Software\Python\PythonCore" /s /v ExecutablePath 2^>nul ^| findstr /i "ExecutablePath"') do (
    if exist "%%B" (
      set "PY=%%B"
      goto :pick
    )
  )
)

:notfound
echo   没有找到可用的 Python。
echo.
echo   这不是工具的问题，是系统里缺解释器，或者装了解释器但没进 PATH。
echo.
echo     1. 装一个（推荐）
echo        https://www.python.org/downloads/
echo        安装第一步务必勾选 "Add python.exe to PATH"，装完重新双击本文件
echo.
echo     2. 已经装了，但从这里找不到
echo        开始菜单搜 "py"，在打开的窗口里执行：
echo          py -3 wjx_probe_offline.pyz
echo.
echo     3. 上面两条都不行
echo        把这个窗口截图发给作者
echo.
pause
exit /b 1

:pick
rem 离线版优先：它把依赖一起带上了，不需要联网
set "TARGET="
if exist "wjx_probe_offline.pyz" set "TARGET=wjx_probe_offline.pyz"
if not defined TARGET if exist "wjx_probe_all_in_one.py" set "TARGET=wjx_probe_all_in_one.py"
if not defined TARGET (
  echo   没找到主程序文件。
  echo   wjx_probe_offline.pyz 或 wjx_probe_all_in_one.py 要和本文件放在同一目录。
  echo.
  pause
  exit /b 1
)

echo   解释器：%PY%
echo   主程序：%TARGET%
echo.
%PY% "%TARGET%" %*
set "CODE=%errorlevel%"
if not "%CODE%"=="0" (
  echo.
  echo   启动失败，退出码 %CODE%
  echo   把上面 20 行输出发给作者即可定位。
  echo.
  pause
)
exit /b %CODE%
