#!/bin/sh
# library.sh — the same menu as LIBRARY.bat, for macOS and Linux.
#
# Every option here runs the identical update_catalog.py command its numbered
# twin runs on Windows, so the README and the help screen describe both.
# Nothing in this script touches your model folders; it only ever reads them.

set -u
cd "$(dirname "$0")" || exit 1

PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
    echo "Python 3 was not found on your PATH. Install it, then run this again."
    exit 1
fi

run() { "$PY" update_catalog.py "$@"; }

reveal() {
    if [ ! -e "$1" ]; then
        echo "  Not there yet: $1"
        echo "  Make it first — option R rebuilds the gallery and spreadsheet."
        return 1
    fi
    if command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1 &
    elif command -v open >/dev/null 2>&1; then open "$1" >/dev/null 2>&1 &
    else echo "  Open this yourself:  $PWD/$1"; fi
}

pause() {
    printf '\n  --------------------------------------------------\n'
    printf '  Press Enter to continue '
    read -r _ignored || true
}

ask() {                       # ask "prompt"  -> 0 if the answer began with y
    printf '  %s ' "$1"
    read -r reply || return 1
    case "$reply" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

prompt_folder() {             # prints the folder, or nothing if cancelled
    printf '  Folder: '
    read -r folder || return 1
    [ -n "$folder" ] || return 1
    printf '%s' "$folder"
}

menu() {
    clear 2>/dev/null || true
    cat <<'EOF'

  ==================================================
     3D PRINT LIBRARY
  ==================================================

  BROWSE
    1   Open the gallery
    2   Open the spreadsheet

  CHECK THINGS  (safe - changes nothing)
    3   Status: what's downloaded, is everything working
    4   Show which folders get scanned
    5   Test thumbnails on 6 small models
   14   Show what the keyword rules did not recognise
   15   Find kits you have more than one copy of

  PICTURES
    6   Make thumbnails for items that have none
    7   Throw away all thumbnails and make them again
   16   Change how thumbnails look

  FOLDERS AND FILES
    8   Add a folder to the library
    9   Stop scanning a folder
   10   Look for new files in all folders
   11   Fix files I moved
   12   Remove entries for files that are gone
   13   Re-apply the keyword rules to everything

  OTHER
    R   Refresh gallery + spreadsheet  (fast, no pictures)
    B   Back up the catalog right now
    U   Undo - put the newest backup back
    H   Help - what these actually do
    Q   Quit

EOF
}

help_screen() {
    clear 2>/dev/null || true
    cat <<'EOF'

  WHAT THESE DO
  -------------
  1  Gallery: browse everything. Each card has "copy filename"
     and "copy path" buttons - paste those into your file search
     or file manager to find the real file on disk.

  3  Status: how many models are downloaded from the cloud, how
     many are still online-only, whether rendering works, and
     what happened to every thumbnail that does not exist.

  6  Most-used option. After your cloud folder downloads more
     files, this makes pictures for the new ones. Skips
     everything already done, and is safe to interrupt.

  8  Point it at any folder of models to fold it into the
     library. It only adds what's new.

  11 The catalog stores where files were. If you reorganise,
     this matches moved folders by their contents and updates
     them, keeping the same thumbnail so nothing is re-rendered.

  13 Changed rules.json? This re-labels everything already in
     the catalog from the new rules - no rescan, nothing
     re-rendered. It shows you what would change first.

  14 Shows what the rules did not recognise, and the filenames
     inside those folders - the raw material for writing
     patterns. Prints names only, never full paths.

  16 Changes the colour scheme used for rendered thumbnails.
     You see samples first, and the choice is remembered in
     rules.local.json - your own file, never overwritten by
     an update.

  15 Lists kits whose contents are identical - the same model
     downloaded twice. Reports only; nothing is ever deleted.

  NOTHING IN THIS MENU EVER MOVES, RENAMES OR DELETES YOUR
  MODEL FILES. The library only reads them. Options 11 and 12
  change the catalog only, and it is backed up first.

  Backups happen automatically before anything that writes the
  catalog, including the long picture runs. They live in the
  backups  folder; option U puts the newest one back.

EOF
}

while :; do
    menu
    printf '  Choose: '
    read -r choice || exit 0
    case "$choice" in
    1)  reveal gallery.html || pause ;;
    2)  reveal catalog.csv  || pause ;;
    3)  clear 2>/dev/null || true; echo; echo "  Checking..."; echo
        run --diagnose; pause ;;
    4)  clear 2>/dev/null || true; echo
        run --sources
        echo "  You can also open sources.txt in a text editor and edit it directly."
        pause ;;
    5)  clear 2>/dev/null || true; echo
        echo "  Rendering 6 small models two ways. Takes under a minute."; echo
        run --compare-engines 6
        [ -f _engine_test/compare.html ] && reveal _engine_test/compare.html
        pause ;;
    6)  clear 2>/dev/null || true; echo
        echo "  Makes pictures only for items that don't have one."
        echo "  Ctrl+C is safe - finished ones are kept, it resumes."; echo
        run --thumbs-only --engine mesh; pause ;;
    7)  clear 2>/dev/null || true; echo
        echo "  Rebuilds EVERY thumbnail from scratch."
        echo "  Only worth doing if the current pictures look wrong."
        echo "  Takes a long time. Ctrl+C is safe and it resumes."; echo
        if ask "Type Y to continue:"; then
            clear 2>/dev/null || true; echo
            run --thumbs-only --force --engine mesh; pause
        fi ;;
    8)  clear 2>/dev/null || true; echo
        echo "  Paste the folder to add, then press Enter."
        echo "  Example:   /Users/you/Dropbox/New STLs"; echo
        f=$(prompt_folder) || continue
        [ -n "$f" ] || continue
        clear 2>/dev/null || true; echo
        run "$f" --engine mesh; pause ;;
    9)  clear 2>/dev/null || true; echo
        run --sources
        echo
        echo "  Paste the folder to STOP scanning."
        echo "  Your files stay. Existing catalog entries stay."; echo
        f=$(prompt_folder) || continue
        [ -n "$f" ] || continue
        clear 2>/dev/null || true; echo
        run --remove-source "$f"; pause ;;
    10) clear 2>/dev/null || true; echo
        echo "  Checks every folder in the scan list for new files."
        echo "  Existing entries and pictures are kept."; echo
        if ask "Type Y to continue:"; then
            clear 2>/dev/null || true; echo
            run --rescan-all --engine mesh; pause
        fi ;;
    11) clear 2>/dev/null || true; echo
        echo "  Finds items whose folder moved and updates them in place,"
        echo "  keeping the same thumbnail. Nothing is deleted."; echo
        echo "  If you moved a folder somewhere NEW, do option 8 first so"
        echo "  the new location is known, then run this."; echo
        if ask "Type Y to continue:"; then
            clear 2>/dev/null || true; echo
            run --relocate; pause
        fi ;;
    12) clear 2>/dev/null || true; echo
        echo "  Removes catalog entries whose files no longer exist."
        echo "  This does NOT touch your model files - only the catalog."
        echo "  Run option 11 first so moved files aren't mistaken for gone."; echo
        echo "  If a drive isn't mounted, its entries are LEFT ALONE rather"
        echo "  than treated as deleted, so this is safe to run offline."; echo
        if ask "Type Y to continue:"; then
            clear 2>/dev/null || true; echo
            run --relocate --prune; pause
        fi ;;
    13) clear 2>/dev/null || true; echo
        echo "  Re-labels everything already in the catalog using rules.json"
        echo "  as it stands now. No folders are scanned and no pictures are"
        echo "  re-made. You get a preview first; nothing changes until you"
        echo "  confirm."; echo
        run --reclassify --dry-run
        echo
        if ask "Apply these changes? Type Y:"; then
            clear 2>/dev/null || true; echo
            run --reclassify; pause
        fi ;;
    14) clear 2>/dev/null || true; echo
        echo "  Lists what none of the rules matched, with the names of the"
        echo "  files inside, so you can see what patterns are missing."
        echo "  Changes nothing."; echo
        run --unmatched 25; pause ;;
    15) clear 2>/dev/null || true; echo
        echo "  Finds kits catalogued more than once and writes the full list"
        echo "  to potential_duplicates.csv. It DELETES NOTHING and never"
        echo "  touches your model folders - deciding what to remove is yours."; echo
        run --duplicates 20
        [ -f potential_duplicates.csv ] && echo "  Spreadsheet:  potential_duplicates.csv"
        pause ;;
    16) clear 2>/dev/null || true
        cat <<'EOF'

  COLOUR SCHEMES
    slate      the default - pale grey-blue on charcoal
    paper      warm grey on off-white
    blueprint  blue on deep navy
    bronze     warm metal on near-black
    mono       plain grey on black
    resin      pale green on charcoal

EOF
        printf '  Which one? (or press Enter to cancel): '
        read -r theme || continue
        [ -n "$theme" ] || continue
        clear 2>/dev/null || true; echo
        echo "  Rendering 6 samples in \"$theme\" so you can look before committing."
        echo "  Nothing else is touched."; echo
        run --sample 6 --style "$theme" || { pause; continue; }
        [ -f _render_test/preview.html ] && reveal _render_test/preview.html
        echo
        if ask "Keep this scheme for future pictures? Type Y:"; then
            run --set-style "$theme"
            echo
            echo "  Pictures you already have keep the old look until rebuilt."
            if ask "Rebuild them all now? That takes a while. Type Y:"; then
                clear 2>/dev/null || true; echo
                run --thumbs-only --force --engine mesh
            fi
        fi
        pause ;;
    r|R) clear 2>/dev/null || true; echo
         echo "  Rebuilding gallery and spreadsheet..."; echo
         run --rebuild-views; pause ;;
    b|B) clear 2>/dev/null || true; echo
         run --backup
         echo
         echo "  Backups kept (newest first):"
         ls -1t backups/*.json 2>/dev/null | head -20
         pause ;;
    u|U) clear 2>/dev/null || true; echo
         echo "  Puts the newest backup back as the catalog. The file it"
         echo "  replaces is backed up first, so this can itself be undone."; echo
         if ask "Type Y to continue:"; then
             clear 2>/dev/null || true; echo
             run --restore-backup
             run --rebuild-views
             pause
         fi ;;
    h|H) help_screen; pause ;;
    q|Q) exit 0 ;;
    *)   ;;
    esac
done
