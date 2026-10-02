# dmgbuild settings that produced dmg-DS_Store, the Finder layout of the .dmg window:
# background, window size, icon size and where the two icons sit.
#
# packaging/build_dmg.sh never mounts the image (`hdiutil create -srcfolder` builds it straight
# from a directory), so it can't lay the window out by driving Finder. It copies dmg-DS_Store in
# as .DS_Store instead, next to .background.tiff; the background is found by its path on a
# volume named "Tempo", so the volume name must not change.
#
# To change the layout, edit this file and regenerate from the repo root:
#
#   python3 -m venv /tmp/dmgvenv && /tmp/dmgvenv/bin/pip install dmgbuild
#   mkdir -p /tmp/dmg/Tempo.app
#   tiffutil -cathidpicheck desktop/installer/dmg-background.png \
#     desktop/installer/dmg-background@2x.png -out /tmp/dmg/background.tiff
#   /tmp/dmgvenv/bin/dmgbuild -s desktop/installer/dmg-layout.py \
#     -D app=/tmp/dmg/Tempo.app -D background=/tmp/dmg/background.tiff \
#     Tempo /tmp/dmg/layout.dmg
#   hdiutil attach -nobrowse -readonly -mountpoint /tmp/dmg/mnt /tmp/dmg/layout.dmg
#   cp /tmp/dmg/mnt/.DS_Store desktop/installer/dmg-DS_Store
#   hdiutil detach /tmp/dmg/mnt
#
# The placeholder Tempo.app is enough: the layout refers to items by name.

format = 'UDRW'
filesystem = 'HFS+'
files = [defines['app']]  # noqa: F821 (provided by dmgbuild)
symlinks = {'Applications': '/Applications'}
background = defines['background']  # noqa: F821
window_rect = ((200, 120), (660, 400))
default_view = 'icon-view'
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
show_icon_preview = False
include_icon_view_settings = True
arrange_by = None
icon_size = 128
text_size = 13
label_pos = 'bottom'
# The centres of the two plain squares in dmg-background.png (measured, at 1x), either side
# of the arrow at (330, 200).
icon_locations = {'Tempo.app': (167, 200), 'Applications': (493, 200)}
