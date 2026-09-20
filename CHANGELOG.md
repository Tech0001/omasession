# Changelog

User-visible changes to OmaSession are recorded here. Marketplace verification
is a separate process tied to the exact repository commit in its verification
request.

## 0.3.1 — 2026-09-20

- Added separate **Restore App Windows** cards for Google Chrome and Ghostty,
  with independent app toggles.
- Added native browser app restoration: the Chrome card can ask Chrome to
  restore its saved windows and tabs, then apply the saved Hyprland workspace
  placement.
- Preserved the separate repair path for Chrome windows that are already open,
  while making exact browser-title matches win before approximate matches.
- Improved restore behavior when a browser title sidecar is unavailable and
  made the panel/status fallback safer when an auxiliary read fails.
- Added the isolated Chrome restart diagnostic and expanded coverage for the
  browser restore and panel commands.

## 0.3.0 — 2026-09-17

- Added the Chrome automatic browser-repair toggle.
- Documented OmaBackup as the companion project for restoring Omarchy's dotfiles
  and configuration.
