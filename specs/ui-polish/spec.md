# Feature Specification: UI Polish — Small Design Details

**Feature Branch**: `ui-polish`

**Created**: 2026-11-11

**Status**: Implemented

**Input**: User description: "Fix small UI design details across the QuotaManager dashboard. Example issue: browser default context menus appear when right-clicking on lists/menus — these should be suppressed. Find and fix ALL similar small UI design issues."

## User Scenarios & Testing

### User Story 1 - No browser default context menus on interactive elements (Priority: P1)

When a user right-clicks on any custom UI element (buttons, cards, lists, menus, nav items, tables, drawers, dialogs), the browser's default context menu should NOT appear. Only native browser elements (text inputs, links) should show the default context menu.

**Why this priority**: The browser's default context menu breaks the app's polished feel and exposes browser internals (inspect element, view source) that shouldn't be accessible to end users.

**Independent Test**: Right-click on various UI elements (sidebar nav, user cards, device rows, quota tiles, buttons) — no browser context menu should appear. Right-click on a text input — the default context menu SHOULD appear.

**Acceptance Scenarios**:
1. **Given** the dashboard is open, **When** user right-clicks on a sidebar nav item, **Then** no browser context menu appears
2. **Given** the dashboard is open, **When** user right-clicks on a user card, **Then** no browser context menu appears
3. **Given** the dashboard is open, **When** user right-clicks on a text input, **Then** the browser context menu DOES appear (for copy/paste)

### User Story 2 - No text selection on UI chrome (Priority: P1)

Text in UI chrome elements (buttons, nav labels, card headers, badges, icons) should not be selectable. Only content text (logs, reports, terminal output, form inputs) should be selectable.

**Why this priority**: Selectable UI text creates visual noise when users try to select content, and makes the app feel less like a native application.

**Independent Test**: Try to select text in buttons, nav items, card headers — selection should not work. Try to select text in logs/terminal — selection should work.

**Acceptance Scenarios**:
1. **Given** the dashboard is open, **When** user tries to select a button label, **Then** text selection does not occur
2. **Given** the dashboard is open, **When** user tries to select a nav item label, **Then** text selection does not occur
3. **Given** the terminal/logs view is open, **When** user tries to select log text, **Then** text selection DOES work

### User Story 3 - No tap highlight on mobile (Priority: P2)

On mobile/touch devices, tapping interactive elements should not show the default blue/gray tap highlight flash.

**Why this priority**: The tap highlight flash makes the app feel like a web page rather than a native app.

**Independent Test**: On a mobile device or mobile emulator, tap various buttons and cards — no highlight flash should appear.

**Acceptance Scenarios**:
1. **Given** the dashboard is open on a mobile device, **When** user taps a button, **Then** no tap highlight flash appears
2. **Given** the dashboard is open on a mobile device, **When** user taps a card, **Then** no tap highlight flash appears

### User Story 4 - No image/icon dragging (Priority: P2)

Images and SVG icons in the UI should not be draggable by default.

**Why this priority**: Draggable images create visual artifacts and confusion.

**Independent Test**: Try to drag an icon or logo — it should not move.

**Acceptance Scenarios**:
1. **Given** the dashboard is open, **When** user tries to drag the logo, **Then** the logo does not move
2. **Given** the dashboard is open, **When** user tries to drag an icon, **Then** the icon does not move

### User Story 5 - Consistent cursor styles (Priority: P2)

All clickable elements should show `cursor: pointer` on hover. Non-interactive elements should show `cursor: default`.

**Why this priority**: Missing cursor styles make it unclear what's clickable.

**Independent Test**: Hover over various elements — clickable ones should show pointer cursor, non-clickable ones should show default.

**Acceptance Scenarios**:
1. **Given** the dashboard is open, **When** user hovers over a clickable card, **Then** cursor changes to pointer
2. **Given** the dashboard is open, **When** user hovers over non-interactive text, **Then** cursor stays default

### User Story 6 - Hidden number input spinners (Priority: P3)

Number inputs should not show browser default spinner arrows.

**Why this priority**: Spinner arrows look unprofessional and are rarely used.

**Independent Test**: Look at any number input — no spinner arrows should be visible.

**Acceptance Scenarios**:
1. **Given** a number input is displayed, **When** user views it, **Then** no spinner arrows are visible

### User Story 7 - Consistent focus styles (Priority: P3)

All interactive elements should have consistent, visible focus indicators for keyboard navigation.

**Why this priority**: Keyboard accessibility requires visible focus indicators.

**Independent Test**: Tab through the UI — every interactive element should show a visible focus ring.

**Acceptance Scenarios**:
1. **Given** the dashboard is open, **When** user tabs through elements, **Then** each interactive element shows a focus ring

## Edge Cases

- What happens when a dialog is open and user right-clicks? → Context menu should still be suppressed
- What happens on touch devices where right-click isn't available? → Long-press should not show context menu on interactive elements
- What about text in terminal/logs? → Should remain selectable for copy
- What about form inputs? → Should remain fully functional (selectable, context menu available)

## Requirements

### Functional Requirements

- **FR-001**: System MUST suppress browser default context menu on all non-text UI elements
- **FR-002**: System MUST apply `user-select: none` to UI chrome (buttons, nav, cards, badges, headers, icons)
- **FR-003**: System MUST apply `-webkit-tap-highlight-color: transparent` to all interactive elements
- **FR-004**: System MUST disable drag on all images and SVG icons
- **FR-005**: System MUST apply `cursor: pointer` to all clickable elements
- **FR-006**: System MUST hide number input spinner arrows
- **FR-007**: System MUST maintain visible focus indicators on all interactive elements
- **FR-008**: System MUST preserve text selection in content areas (logs, terminal, reports, form inputs)
- **FR-009**: System MUST preserve context menu in form inputs (for copy/paste)
- **FR-010**: System MUST apply these styles consistently across main dashboard, public pages, and share portal

### Key Entities

- **UI Chrome**: Buttons, nav items, cards, badges, headers, icons, tiles, switches
- **Content Areas**: Logs, terminal output, reports, form inputs, text areas
- **Interactive Elements**: Buttons, links, inputs, selects, clickable cards/rows

## Success Criteria

### Measurable Outcomes

- **SC-001**: Zero browser context menus appear when right-clicking on non-text UI elements
- **SC-002**: No text selection occurs on UI chrome elements
- **SC-003**: No tap highlight flash on mobile devices
- **SC-004**: No draggable images/icons in the UI
- **SC-005**: All clickable elements show pointer cursor
- **SC-006**: No number input spinner arrows visible
- **SC-007**: All interactive elements have visible focus indicators
- **SC-008**: Text selection works in content areas and form inputs

## Assumptions

- Changes are CSS-first (global styles) with minimal JS changes
- Public pages (milestone.html, report.html, share.html, guest_pending.html) should follow the same rules
- The detector.js (anti-devtools) already disables context menu globally but only when devtools detection is active — we need a CSS/JS-based solution that works regardless
- Existing `user-select` on `.custom-cb-wrap` in share.css is the pattern to follow
