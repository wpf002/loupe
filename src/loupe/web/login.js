"use strict";

/* Posts with fetch because the page's CSP blocks form submission. */
const form = document.getElementById("login");
const errorSlot = document.getElementById("error");
const title = document.getElementById("title");
const submit = document.getElementById("submit");
const switchMode = document.getElementById("switch");
const password = document.getElementById("password");

const MODES = {
  signIn: {
    title: "Sign In",
    submit: "Sign In",
    other: "Create Account",
    endpoint: "/auth/sign-in",
    autocomplete: "current-password",
    next: "create",
  },
  create: {
    title: "Create Account",
    submit: "Create Account",
    other: "Sign In",
    endpoint: "/auth/create-account",
    autocomplete: "new-password",
    next: "signIn",
  },
};

let mode = MODES.signIn;

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

function applyMode(next) {
  mode = next;
  title.textContent = mode.title;
  document.title = `${mode.title} · Loupe`;
  submit.textContent = mode.submit;
  switchMode.textContent = mode.other;
  password.autocomplete = mode.autocomplete;
  errorSlot.hidden = true;
}

switchMode.addEventListener("click", () => applyMode(MODES[mode.next]));

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  submit.disabled = true;
  submit.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(mode.endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: form.email.value, password: password.value }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `${mode.submit} failed (${response.status})`);
    location.assign("/");
  } catch (error) {
    showError(error.message);
    submit.disabled = false;
    submit.removeAttribute("aria-busy");
  }
});

document.getElementById("email").focus();
