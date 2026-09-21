@echo off
chcp 65001 >nul
REM ============================================================
REM  海大网球订场 V4.1 · 打包脚本
REM  V4.1 新增：挂机过夜 —— 开抢前 15 分钟自动刷新 token（本机记住密码重登）
REM              目标日期自动跟随「今天 + 可提前天数」，跨天挂机不再失效
REM              死代码清理、CAS 报错快速失败、停止按钮修复、登录提速
REM  源码：src\    产物：dist\HainanU_Tennis_Booking_V4_1.exe（端口 8085，与 V4.0 相同）
REM  最后一步会额外生成分发包（LZMA zip），见 过程文件\make_release_zip_lzma_v41.py
REM ============================================================
cd /d "%~dp0"

REM ---- 选一个装了 playwright + PyInstaller 的解释器 ----
set PY=
for %%P in (
    "C:\Users\Cavminde\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "python"
) do (
    if not defined PY (
        %%~P -c "import playwright, PyInstaller" >nul 2>&1
        if not errorlevel 1 set PY=%%~P
    )
)
if not defined PY (
    echo [失败] 没找到装了 playwright 和 PyInstaller 的 Python。
    echo        请先执行：python -m pip install playwright pyinstaller
    pause & exit /b 1
)
echo        使用解释器：%PY%

echo [1/5] 清理旧构建...
if exist build rmdir /s /q build
REM 只删旧 exe 和 spec，保留 dist 里的 config.json / .profiles（用户数据不丢）
if exist dist\HainanU_Tennis_Booking_V4_1.exe del /q dist\HainanU_Tennis_Booking_V4_1.exe
if exist HainanU_Tennis_Booking_V4_1.spec del /q HainanU_Tennis_Booking_V4_1.spec

echo [2/5] 自检（accounts / booker / browserlogin / V4.1 挂机刷新）...
"%PY%" src\selftest.py
if errorlevel 1 (
    echo [失败] 自检没过，先修好再打包。
    pause & exit /b 1
)

echo [3/5] 打包中（约 1-2 分钟）...
REM 注意：必须在项目根目录执行，入口写 src\app_server.py，
REM       --add-data 的路径才是相对于根目录解析的。
REM       在 src\ 里执行会导致 index.html 找不到。
"%PY%" -m PyInstaller --clean --noconfirm --onefile --console ^
    --name HainanU_Tennis_Booking_V4_1 ^
    --distpath "dist" --workpath "build" --specpath "." ^
    --add-data "src/index.html;." ^
    --collect-all playwright ^
    --hidden-import accounts ^
    --exclude-module tkinter --exclude-module unittest ^
    --exclude-module pydoc --exclude-module pydoc_data ^
    "src\app_server.py"
if errorlevel 1 (
    echo [失败] 打包出错，请看上面的日志。
    pause & exit /b 1
)

echo [4/5] 收尾...
REM 不复制 src\config.json —— 那是开发时的配置，别跟着发出去。
REM exe 首次启动会在自己旁边生成一个干净的 config.json。
if exist dist\config.json del /q dist\config.json

REM 把内置浏览器（bundled_chromium）一起放进 dist，
REM 这样不依赖用户机器上有没有装 Edge/Chrome，双击就能弹窗。
if exist bundled_chromium (
    echo 正在复制内置浏览器到 dist\bundled_chromium ...
    if not exist dist\bundled_chromium mkdir dist\bundled_chromium
    xcopy /e /i /y bundled_chromium dist\bundled_chromium >nul
)

echo [5/5] 生成分发包（LZMA 压缩的 zip，约 15-20 分钟，可跳过）...
echo        如果只想更新本机跑的 exe，按 Ctrl+C 中断即可。
if exist "过程文件\make_release_zip_lzma_v41.py" (
    echo        注意：这一步要求 release_zip\HainanU_Tennis_Booking_V4_1\ 已备好最新内容。
    "%PY%" "过程文件\make_release_zip_lzma_v41.py"
) else (
    echo        （跳过：没找到 过程文件\make_release_zip_lzma_v41.py）
)

echo.
echo [完成] 产物：dist\HainanU_Tennis_Booking_V4_1.exe
echo      连同 dist\bundled_chromium\ 一起拷到任意文件夹（比如桌面）双击即可。
echo      注意：内置浏览器文件夹 bundled_chromium 必须和 exe 放在同一目录！
echo      配置文件、设备信任档案都生成在 exe 旁边：
echo        config.json            账号库 + 全部参数（含挂机用的密码）
echo        .profiles\账号id\       该账号的浏览器档案（信任该设备靠它）
echo        bundled_chromium\       内置 Chromium（无需系统装浏览器）
echo      服务端口 8085，启动后自动打开浏览器。
pause
