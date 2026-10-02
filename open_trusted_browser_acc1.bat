@echo off
rem Open trusted Chromium profile for ACC1 (account two / Wang Han / 20243003502) -> order page
rem Double-click to run in your own session so the window appears on your desktop.
set "CHROME=C:\Users\Cavminde\Desktop\Tennis\Tennis Booking V5.0\dist\bundled_chromium\chrome-win64\chrome.exe"
set "PDIR=C:\Users\Cavminde\Desktop\Tennis\Tennis Booking V5.0\dist\.profiles\acc1"
echo Profile: %PDIR%
start "" "%CHROME%" --user-data-dir="%PDIR%" --no-first-run --no-default-browser-check "https://hdscw.hainanu.edu.cn/#/order"
