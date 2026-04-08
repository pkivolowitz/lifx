/* GlowUp Installer — welcome page logic
 *
 * Enables the "Next" button only after the user accepts the license.
 * Next currently has no target page — it will be wired in a later step.
 */

"use strict";

document.addEventListener("DOMContentLoaded", function () {
    var checkbox = document.getElementById("accept-license");
    var btnNext  = document.getElementById("btn-next");

    /* Enable / disable Next based on the license checkbox. */
    checkbox.addEventListener("change", function () {
        btnNext.disabled = !checkbox.checked;
    });

    /* Next button — navigate to the CLI-only decision page. */
    btnNext.addEventListener("click", function () {
        if (!checkbox.checked) return;
        window.location.href = "cli.html";
    });
});
