"use strict";

const form = document.getElementById("login-form");
const password = document.getElementById("password");
const submit = document.getElementById("sign-in");
const message = document.getElementById("error");
const loginToken = document.querySelector('meta[name="nutcracker-login-token"]')?.content || "";
const next = new URLSearchParams(location.search).get("next");
const destination = ["/", "/index.html", "/console.html"].includes(next) ? next : "/console.html";
document.getElementById("open-console").href = destination;
let authenticatedToken = null;

function showError(text = "") {
  message.textContent = text;
  message.hidden = !text;
}

async function readResponse(response) {
  if (!(response.headers.get("content-type") || "").includes("application/json")) {
    throw new Error("The local launcher did not respond. Open Nutcracker on your Mac and try again.");
  }
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "Could not sign in. Try again.");
  return body;
}

async function checkSignIn() {
  if (!["localhost", "127.0.0.1"].includes(location.hostname) || location.protocol !== "http:"
      || !loginToken || loginToken === "__NUTCRACKER_LOGIN_TOKEN__") {
    showError("Open the private Nutcracker launcher on your Mac to sign in.");
    return;
  }
  try {
    const body = await readResponse(await fetch("/api/auth", { credentials: "same-origin", cache: "no-store" }));
    if (body.authenticated) {
      authenticatedToken = body.token;
      form.hidden = true;
      document.getElementById("signed-in").hidden = false;
      document.getElementById("heading").textContent = "You’re signed in.";
      document.getElementById("intro").textContent = `Signed in as ${body.username}. Your Windows desktop is ready to open.`;
    } else if (!body.configured) {
      document.getElementById("setup").hidden = false;
      showError("Local sign-in has not been set up on this Mac.");
    } else {
      submit.disabled = false;
    }
  } catch (error) {
    showError(error.message);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (submit.disabled) return;
  submit.disabled = true;
  showError();
  const body = JSON.stringify({ username: document.getElementById("username").value, password: password.value });
  password.value = "";
  try {
    await readResponse(await fetch("/api/login", {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-Nutcracker-Token": loginToken }, body,
    }));
    location.replace(destination);
  } catch (error) {
    showError(error.message);
    submit.disabled = false;
    password.focus();
  }
});

document.getElementById("sign-out").addEventListener("click", async () => {
  if (!authenticatedToken) return;
  try {
    await readResponse(await fetch("/api/logout", {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-Nutcracker-Token": authenticatedToken }, body: "{}",
    }));
    location.replace("/login.html");
  } catch (error) {
    showError(error.message);
  }
});

window.addEventListener("pagehide", () => { password.value = ""; });
checkSignIn();
