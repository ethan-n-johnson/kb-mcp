// Behaviour for the knowledge-base UI. No inline scripts or handlers exist in
// the templates (a strict Content-Security-Policy applies), so everything is
// wired here.
(function () {
  "use strict";

  var TOKEN_KEY = "kbToken";

  function readToken() {
    try {
      return window.sessionStorage.getItem(TOKEN_KEY) || "";
    } catch (err) {
      return "";
    }
  }

  function storeToken(value) {
    try {
      if (value) {
        window.sessionStorage.setItem(TOKEN_KEY, value);
      } else {
        window.sessionStorage.removeItem(TOKEN_KEY);
      }
      return true;
    } catch (err) {
      return false;
    }
  }

  function showTokenState() {
    var status = document.getElementById("token-status");
    var clear = document.getElementById("token-clear");
    var has = readToken() !== "";
    if (status) {
      status.textContent = has ? "write token set" : "read only";
      status.classList.toggle("token-set", has);
    }
    if (clear) {
      clear.hidden = !has;
    }
  }

  function setupTokenBox() {
    var form = document.getElementById("token-form");
    var input = document.getElementById("token-input");
    var clear = document.getElementById("token-clear");
    if (form && input) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var value = input.value.trim();
        if (value) {
          storeToken(value);
        }
        input.value = "";
        showTokenState();
      });
    }
    if (clear) {
      clear.addEventListener("click", function () {
        storeToken("");
        showTokenState();
      });
    }
    showTokenState();
  }

  // Send the token with every request that is not a GET (writes).
  document.addEventListener("htmx:configRequest", function (event) {
    var verb = String(event.detail.verb || "get").toLowerCase();
    var token = readToken();
    if (verb !== "get" && token) {
      event.detail.headers["Authorization"] = "Bearer " + token;
    }
  });

  // htmx does not swap 4xx and 5xx responses by default. The server answers
  // errors with a notice fragment, so swap it in; for anything that is not
  // HTML (a proxy error page, JSON), show a plain message instead.
  document.addEventListener("htmx:beforeSwap", function (event) {
    var xhr = event.detail.xhr;
    if (!xhr || xhr.status < 400) {
      return;
    }
    var type = xhr.getResponseHeader("Content-Type") || "";
    if (type.indexOf("text/html") === -1) {
      var box = document.createElement("div");
      box.className = "notice notice-error";
      box.setAttribute("role", "alert");
      box.textContent = "The request failed (HTTP " + xhr.status + ").";
      event.detail.serverResponse = box.outerHTML;
    }
    // A button that must stay on screen names a place for its error notice.
    var requester = event.detail.requestConfig && event.detail.requestConfig.elt;
    var selector = requester && requester.getAttribute("data-error-target");
    var place = selector ? document.querySelector(selector) : null;
    if (place) {
      event.detail.target = place;
      event.detail.swapOverride = "innerHTML";
    }
    event.detail.shouldSwap = true;
    event.detail.isError = false;
  });

  // Clear an old error notice when a new request starts.
  document.addEventListener("htmx:beforeRequest", function () {
    var flash = document.getElementById("flash");
    if (flash) {
      flash.replaceChildren();
    }
  });

  document.addEventListener("htmx:sendError", function (event) {
    var target = event.detail.target || event.target;
    if (!target) {
      return;
    }
    var box = document.createElement("div");
    box.className = "notice notice-error";
    box.setAttribute("role", "alert");
    box.textContent = "Could not reach the server. Check your connection and try again.";
    target.replaceChildren(box);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setupTokenBox);
  } else {
    setupTokenBox();
  }
})();
