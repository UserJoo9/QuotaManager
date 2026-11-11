# Implementation Plan: UI Polish — Small Design Details

**Branch**: `ui-polish` | **Date**: 2026-11-11 | **Spec**: specs/ui-polish/spec.md

## Summary

Apply global CSS and minimal JS fixes to suppress browser default behaviors on UI chrome elements across the QuotaManager dashboard and public pages. The primary approach is CSS-first (global styles in base.css) with targeted JS event handlers where CSS alone isn't sufficient.

## Technical Context

**Language/Version**: CSS3, JavaScript (ES modules), HTML5
**Primary Dependencies**: None (vanilla CSS/JS, no build step)
**Target Platforms**: Desktop browsers (Chrome, Firefox, Safari, Edge) + mobile (iOS Safari, Android Chrome)
**Project Type**: Web application (FastAPI backend + vanilla JS frontend)
**Constraints**: No build step — plain ES modules under web/. Changes must be CSS-first. Must work in both LTR and RTL modes.

## Constitution Check

No constitution file exists. Proceeding with standard web best practices.

## Design

### Approach: CSS-First Global Styles

All fixes will be applied via global CSS rules in `base.css` (main dashboard) and `pub.css`/`share.css` (public pages). JS changes only where CSS alone is insufficient.

### Changes Required

1. **base.css** (main dashboard):
   - Add `user-select: none` to UI chrome elements
   - Add `-webkit-tap-highlight-color: transparent` to all interactive elements
   - Add `cursor: pointer` to clickable elements
   - Hide number input spinners
   - Add context menu suppression via CSS `user-select` + JS event handler
   - Ensure `draggable="false"` on images/icons via CSS

2. **pub.css** (public pages):
   - Already has tap-highlight; add user-select and contextmenu rules

3. **share.css** (share portal):
   - Already has tap-highlight and user-select on `.custom-cb-wrap`; extend to all UI chrome

4. **main.js** (dashboard):
   - Add global `contextmenu` event listener to suppress on non-text elements
   - Add `dragstart` prevention on images/icons

5. **HTML files** (index.html, milestone.html, report.html, share.html, guest_pending.html):
   - Add `draggable="false"` to img/logo elements
   - Add `oncontextmenu="return false"` where appropriate

### Complexity Tracking

No significant complexity. All changes are additive CSS/JS rules.

## Implementation Strategy

1. Start with base.css (covers main dashboard)
2. Then pub.css (public pages)
3. Then share.css (share portal)
4. Then main.js (JS event handlers)
5. Then HTML files (draggable attributes)
6. Verify with tests (test_new_ui.py)
