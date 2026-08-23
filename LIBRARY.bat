@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title 3D Print Library

:menu
cls
echo.
echo   ==================================================
echo      3D PRINT LIBRARY
echo   ==================================================
echo.
echo   BROWSE
echo     1   Open the gallery
echo     2   Open the spreadsheet
echo.
echo   CHECK THINGS  (safe - changes nothing)
echo     3   Status: what's downloaded, is everything working
echo     4   Show which folders get scanned
echo     5   Test thumbnails on 6 small models
echo.
echo   PICTURES
echo     6   Make thumbnails for items that have none
echo     7   Throw away all thumbnails and make them again
echo.
echo   FOLDERS AND FILES
echo     8   Add a folder to the library
echo     9   Stop scanning a folder
echo    10   Look for new files in all folders
echo    11   Fix files I moved
echo    12   Remove entries for files that are gone
echo    13   Re-apply the keyword rules to everything
echo.
echo   OTHER
echo     R   Refresh gallery + spreadsheet  (fast, no pictures)
echo     B   Back up the catalog right now
echo     U   Undo - put the newest backup back
echo     H   Help - what these actually do
echo     Q   Quit
echo.
set "c="
set /p "c=  Choose: "

if "%c%"=="1"  ( start "" "gallery.html" & goto menu )
if "%c%"=="2"  ( start "" "catalog.csv"  & goto menu )
if "%c%"=="3"  goto status
if "%c%"=="4"  goto srcshow
if "%c%"=="5"  goto compare
if "%c%"=="6"  goto missing
if "%c%"=="7"  goto redo
if "%c%"=="8"  goto addfolder
if "%c%"=="9"  goto srcdel
if "%c%"=="10" goto rescanall
if "%c%"=="11" goto relocate
if "%c%"=="12" goto prune
if "%c%"=="13" goto reclass
if /i "%c%"=="R" goto refresh
if /i "%c%"=="B" goto backup
if /i "%c%"=="U" goto restore
if /i "%c%"=="H" goto help
if /i "%c%"=="Q" exit /b
goto menu


:backup
cls & echo.
python update_catalog.py --backup
echo.
echo   Backups kept (newest first):
dir /b /o-d "backups\*.json" 2>nul | more +0
goto pause

:restore
cls & echo.
echo   Puts the newest backup back as the catalog. The file it replaces is
echo   backed up first, so this can itself be undone.
echo.
set "y="
set /p "y=  Type Y to continue: "
if /i not "%y%"=="Y" goto menu
cls & echo.
python update_catalog.py --restore-backup
python update_catalog.py --rebuild-views
goto pause

:reclass
cls & echo.
echo   Re-labels everything already in the catalog using rules.json as it
echo   stands now. No folders are scanned and no pictures are re-made.
echo   You get a preview first; nothing changes until you confirm.
echo.
python update_catalog.py --reclassify --dry-run
echo.
set "y="
set /p "y=  Apply these changes? Type Y: "
if /i not "%y%"=="Y" goto menu
cls & echo.
python update_catalog.py --reclassify
goto pause

:status
cls & echo. & echo   Checking...& echo.
python update_catalog.py --diagnose
goto pause

:srcshow
cls & echo.
python update_catalog.py --sources
echo   You can also open sources.txt in Notepad and edit it directly.
goto pause

:compare
cls & echo. & echo   Rendering 6 small models two ways. Takes under a minute.& echo.
python update_catalog.py --compare-engines 6
if exist "_engine_test\compare.html" start "" "_engine_test\compare.html"
goto pause

:missing
cls & echo.
echo   Makes pictures only for items that don't have one.
echo   Ctrl+C is safe - finished ones are kept, it resumes.
echo.
python update_catalog.py --thumbs-only --engine mesh
goto pause

:redo
cls & echo.
echo   Rebuilds EVERY thumbnail from scratch.
echo   Only worth doing if the current pictures look wrong.
echo   Takes 20-40 minutes. Ctrl+C is safe and it resumes.
echo.
set "y="
set /p "y=  Type Y to continue: "
if /i not "%y%"=="Y" goto menu
cls & echo.
python update_catalog.py --thumbs-only --force --engine mesh
goto pause

:addfolder
cls & echo.
echo   Paste the folder to add, then press Enter.
echo   Example:   D:\Dropbox\New STLs
echo.
set "f="
set /p "f=  Folder: "
if not defined f goto menu
cls & echo.
python update_catalog.py "%f%" --engine mesh
goto pause

:srcdel
cls & echo.
python update_catalog.py --sources
echo.
echo   Paste the folder to STOP scanning.
echo   Your files stay. Existing catalog entries stay.
echo.
set "f="
set /p "f=  Folder: "
if not defined f goto menu
cls & echo.
python update_catalog.py --remove-source "%f%"
goto pause

:rescanall
cls & echo.
echo   Checks every folder in the scan list for new files.
echo   Existing entries and pictures are kept.
echo.
set "y="
set /p "y=  Type Y to continue: "
if /i not "%y%"=="Y" goto menu
cls & echo.
python update_catalog.py --rescan-all --engine mesh
goto pause

:relocate
cls & echo.
echo   Finds items whose folder moved and updates them in place,
echo   keeping the same thumbnail. Nothing is deleted.
echo.
echo   If you moved a folder somewhere NEW, do option 8 first so
echo   the new location is known, then run this.
echo.
set "y="
set /p "y=  Type Y to continue: "
if /i not "%y%"=="Y" goto menu
cls & echo.
python update_catalog.py --relocate
goto pause

:prune
cls & echo.
echo   Removes catalog entries whose files no longer exist.
echo   This does NOT touch your model files - only the catalog.
echo   Run option 11 first so moved files aren't mistaken for gone.
echo.
echo   If a drive isn't plugged in, its entries are LEFT ALONE rather than
echo   treated as deleted, so this is safe to run with something offline.
echo.
set "y="
set /p "y=  Type Y to continue: "
if /i not "%y%"=="Y" goto menu
cls & echo.
python update_catalog.py --relocate --prune
goto pause

:refresh
cls & echo. & echo   Rebuilding gallery and spreadsheet...& echo.
python update_catalog.py --rebuild-views
goto pause

:help
cls
echo.
echo   WHAT THESE DO
echo   -------------
echo   1  Gallery: browse everything. Each card has "copy filename"
echo      and "copy path" buttons - paste those into Windows search
echo      or Explorer to find the real file on disk.
echo.
echo   3  Status: how many models are downloaded from Dropbox, how
echo      many are still online-only, whether rendering works.
echo.
echo   6  Most-used option. After Dropbox downloads more files, this
echo      makes pictures for the new ones. Skips everything already done.
echo.
echo   8  Point it at any folder of models to fold it into the library.
echo      It only adds what's new.
echo.
echo   11 The catalog stores where files were. If you reorganise, this
echo      matches moved folders by their contents and updates them,
echo      keeping the same thumbnail so nothing is re-rendered.
echo.
echo   13 Changed rules.json? This re-labels everything already in the
echo      catalog from the new rules - no rescan, nothing re-rendered.
echo      It shows you what would change before it changes anything.
echo.
echo   NOTHING IN THIS MENU EVER MOVES, RENAMES OR DELETES YOUR
echo   MODEL FILES. The library only reads them. Options 11 and 12
echo   change the catalog only, and it is backed up first.
echo.
echo   Backups happen automatically before anything that writes the
echo   catalog, including the long picture runs. They live in the
echo   backups  folder; option U puts the newest one back.
echo.
goto pause

:pause
echo.
echo   --------------------------------------------------
pause
goto menu
