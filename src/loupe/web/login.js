"use strict";

/* Posts with fetch because the page's CSP blocks form submission. */
const form = document.getElementById("login");
const errorSlot = document.getElementById("error");

function showError(message) {
  errorSlot.className = "alert";
  errorSlot.setAttribute("role", "alert");
  errorSlot.replaceChildren();
  const label = document.createElement("b");
  label.textContent = "Error";
  const text = document.createElement("span");
  text.textContent = message;
  errorSlot.append(label, text);
  errorSlot.hidden = false;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = form.querySelector("button[type=submit]");
  submit.disabled = true;
  submit.setAttribute("aria-busy", "true");
  try {
    const response = await fetch("/auth/sign-in", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: form.email.value, password: form.password.value }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Sign in failed (${response.status})`);
    location.assign("/");
  } catch (error) {
    showError(error.message);
    submit.disabled = false;
    submit.removeAttribute("aria-busy");
  }
});

document.getElementById("email").focus();
