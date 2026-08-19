(() => {
  const container = document.querySelector("[data-partial-refresh]");
  const refreshButton = document.querySelector("[data-manual-refresh]");
  let filterSubmitTimer = null;

  const submitFilterForm = (form) => {
    if (!(form instanceof HTMLFormElement) || form.dataset.submitting === "true") {
      return;
    }
    form.dataset.submitting = "true";
    form.requestSubmit();
  };

  document.addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement || target instanceof HTMLSelectElement || target instanceof HTMLTextAreaElement)) {
      return;
    }
    const form = target.closest("form[data-auto-submit-filters='true']");
    if (!(form instanceof HTMLFormElement)) {
      return;
    }
    if (target instanceof HTMLSelectElement) {
      window.clearTimeout(filterSubmitTimer);
      submitFilterForm(form);
    }
  });

  document.addEventListener("input", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement) || target.type !== "text") {
      return;
    }
    const form = target.closest("form[data-auto-submit-filters='true']");
    if (!(form instanceof HTMLFormElement)) {
      return;
    }
    window.clearTimeout(filterSubmitTimer);
    filterSubmitTimer = window.setTimeout(() => {
      submitFilterForm(form);
    }, 350);
  });

  const runPartialRefresh = async () => {
    if (!container) {
      return;
    }
    const url = new URL(window.location.href);
    url.searchParams.set("partial", "1");
    try {
      const response = await fetch(url.toString(), {
        headers: { "X-Requested-With": "sports-edge-dashboard" },
      });
      if (!response.ok) {
        return;
      }
      container.innerHTML = await response.text();
    } catch (_error) {
      // Keep the current page state if refresh fails.
    }
  };

  if (refreshButton) {
    refreshButton.addEventListener("click", async () => {
      const endpoint = refreshButton.getAttribute("data-refresh-endpoint");
      const cooldownSeconds = Number(refreshButton.getAttribute("data-cooldown-seconds") || "30");
      if (!endpoint || refreshButton.disabled) {
        return;
      }
      const originalText = refreshButton.textContent;
      refreshButton.disabled = true;
      refreshButton.textContent = "Refreshing...";
      try {
        await fetch(endpoint, {
          method: "POST",
          headers: { "X-Requested-With": "sports-edge-dashboard" },
        });
      } catch (_error) {
        // Keep the current page state if the manual refresh request fails.
      }
      await runPartialRefresh();
      window.setTimeout(() => {
        refreshButton.disabled = false;
        refreshButton.textContent = originalText;
      }, Math.max(1, cooldownSeconds) * 1000);
    });
  }

  if (!container) {
    return;
  }

  const refreshSeconds = Number(container.getAttribute("data-refresh-seconds") || "0");
  if (!Number.isFinite(refreshSeconds) || refreshSeconds <= 0) {
    return;
  }

  window.setInterval(runPartialRefresh, refreshSeconds * 1000);
})();
