# Tasks: UI Polish — Small Design Details

## Task List

### T1: Add context menu suppression to base.css (P1)
- **Model**: opencode/minimax-m3.1-flash-free
- **Status**: pending
- **Description**: Add CSS rules to prevent text selection and context menus on UI chrome elements in base.css
- **Files**: web/css/base.css
- **Acceptance**: UI chrome elements have `user-select: none`

### T2: Add tap highlight and cursor styles to base.css (P2)
- **Model**: opencode/minimax-m3.1-flash-free
- **Status**: pending
- **Description**: Add `-webkit-tap-highlight-color: transparent` and `cursor: pointer` rules to base.css
- **Files**: web/css/base.css
- **Acceptance**: Interactive elements show pointer cursor and no tap highlight

### T3: Hide number input spinners in base.css (P3)
- **Model**: opencode/minimax-m3.1-flash-free
- **Status**: pending
- **Description**: Add CSS to hide number input spinner arrows
- **Files**: web/css/base.css
- **Acceptance**: No spinner arrows visible on number inputs

### T4: Add global contextmenu prevention in main.js (P1)
- **Model**: opencode/minimax-m3.1-flash-free
- **Status**: pending
- **Description**: Add global event listener in main.js to suppress contextmenu on non-text elements
- **Files**: web/js/main.js
- **Acceptance**: Right-click on UI elements doesn't show browser context menu

### T5: Add drag prevention for images/icons (P2)
- **Model**: opencode/minimax-m3.1-flash-free
- **Status**: pending
- **Description**: Add JS to prevent dragging on images and SVG icons
- **Files**: web/js/main.js or web/js/core/dom.js
- **Acceptance**: Images/icons cannot be dragged

### T6: Update pub.css for public pages (P2)
- **Model**: opencode/minimax-m3.1-flash-free
- **Status**: pending
- **Description**: Apply same UI polish rules to pub.css for public pages
- **Files**: web/assets/pub.css
- **Acceptance**: Public pages have same polish as main dashboard

### T7: Update share.css for share portal (P2)
- **Model**: opencode/minimax-m3.1-flash-free
- **Status**: pending
- **Description**: Apply same UI polish rules to share.css
- **Files**: web/assets/share.css
- **Acceptance**: Share portal has same polish

### T8: Add draggable="false" to HTML images (P3)
- **Model**: opencode/minimax-m3.1-flash-free
- **Status**: pending
- **Description**: Add draggable="false" attribute to img and svg elements in HTML files
- **Files**: web/index.html, web/milestone.html, web/report.html, web/share.html, web/guest_pending.html
- **Acceptance**: All images have draggable="false"

### T9: Run tests and verify (P1)
- **Model**: opencode/minimax-m3.1-flash-free
- **Status**: pending
- **Description**: Run test_new_ui.py and visual verification
- **Files**: tests/test_new_ui.py
- **Acceptance**: Tests pass, no regressions
