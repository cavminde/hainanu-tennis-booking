@echo off
chcp 65001 >nul
REM ============================================================
REM  海大网球订场 V5.0 · 打包脚本
REM  V5.0 新增：「场地管理」标签页 —— 已订场地列表（账号/真名/学号/日期/时段/
REM              场地/金额/状态）+ 退订按钮（默认干运行，三道闸防误退）
REM  源码：src\    产物：dist\HainanU_Tennis_Booking_V5_0.exe（端口 8085）
REM  V5.0 不再生成分发 zip —— 打包完直接用 dist\ 目录即可。
REM ============================================================
cd /d "%~dp0"

REM ---- 选一个装了 playwright + PyInstaller 的解释器 ----
REM 自检脚本里全是 ✓ / 中文，GBK 控制台会抛 UnicodeEncodeError，强制 UTF-8
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
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

echo [1/4] 清理旧构建...
if exist build rmdir /s /q build
REM 只删旧 exe 和 spec，保留 dist 里的 config.json / .profiles（用户数据不丢）
if exist dist\HainanU_Tennis_Booking_V5_0.exe del /q dist\HainanU_Tennis_Booking_V5_0.exe
if exist HainanU_Tennis_Booking_V5_0.spec del /q HainanU_Tennis_Booking_V5_0.spec

echo [2/4] 自检（accounts / booker / browserlogin / 放号周期 / 一键关场）...
"%PY%" src\selftest.py
if errorlevel 1 (
    echo [失败] 自检没过，先修好再打包。
    pause & exit /b 1
)

echo [3/4] 打包中（约 1-2 分钟）...
REM 注意：必须在项目根目录执行，入口写 src\app_server.py，
REM       --add-data 的路径才是相对于根目录解析的。
REM       在 src\ 里执行会导致 index.html 找不到。
"%PY%" -m PyInstaller --clean --noconfirm --onefile --console ^
    --name HainanU_Tennis_Booking_V5_0 ^
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

echo [4/4] 收尾...
REM 内置浏览器已经在 dist\bundled_chromium 里了（从 V4.3 目录整体拷过来的），
REM 这里兜一次底：万一被人删了，就试着从 V4.3 目录补回来。
if not exist dist\bundled_chromium\chrome-win64\chrome.exe (
    echo        缺内置浏览器，尝试从 V4.3 目录补齐...
    if exist "..\Tennis Booking V4.3\dist\bundled_chromium" (
        xcopy /e /i /y "..\Tennis Booking V4.3\dist\bundled_chromium" dist\bundled_chromium >nul
    )
)

echo.
echo [完成] 产物：dist\HainanU_Tennis_Booking_V5_0.exe
echo      连同 dist\bundled_chromium\ 一起拷到任意文件夹（比如桌面）双击即可。
echo      注意：内置浏览器文件夹 bundled_chromium 必须和 exe 放在同一目录！
echo      配置文件、设备信任档案都生成在 exe 旁边：
echo        config.json            账号库 + 全部参数（含挂机用的密码）
echo        .profiles\账号id\       该账号的浏览器档案（信任该设备靠它）
echo        bundled_chromium\       内置 Chromium（无需系统装浏览器）
echo      服务端口 8085，启动后自动打开浏览器。
echo      V5.0 新增：顶部「场地管理」标签页可查看 / 退订已抢到的场地。
pause
