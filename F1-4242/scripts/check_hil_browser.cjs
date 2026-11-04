/* Compatibility entry for the complete horizontal HIL browser regression.
 * Set HIL_REVIEW_URL to a local offline workbench (default port 18447).
 * Launches its own Chrome; all action requests are intercepted by test fixtures.
 */
require('./check_hil_workspace_browser.cjs');
