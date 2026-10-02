@echo off
rem  Open the saved ("trusted") Chromium profile and jump to the order page.
rem  Double click  -> uses acc2 (20253007148, the account that booked 09-30)
rem  Or drag a profile folder (dist\.profiles\accN) onto this file.

set "CHROME=C:\Users\Cavminde\Desktop\Tennis\Tennis Booking V5.0\dist\bundled_chromium\chrome-win64\chrome.exe"
set "PDIR=C:\Users\Cavminde\Desktop\Tennis\Tennis Booking V5.0\dist\.profiles\acc2"

if not "%~1"=="" set "PDIR=%~1"

echo Profile: %PDIR%
start "" "%CHROME%" --user-data-dir="%PDIR%" --no-first-run --no-default-browser-check "https://hdscw.hainanu.edu.cn/#/order"
