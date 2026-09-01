(() => {
  const body = document.body;
  const sidebarToggle = document.querySelector("[data-sidebar-toggle]");
  const sidebarClose = document.querySelector("[data-sidebar-close]");
  const sidebarLinks = document.querySelectorAll(".sidebar .nav-item");
  const container = document.querySelector("[data-partial-refresh]");
  const refreshButton = document.querySelector("[data-manual-refresh]");
  let filterSubmitTimer = null;
  let carouselResizeHandlerAttached = false;

  const closeSidebar = () => {
    if (!sidebarToggle) {
      return;
    }
    body.classList.remove("nav-open");
    sidebarToggle.setAttribute("aria-expanded", "false");
  };

  const openSidebar = () => {
    if (!sidebarToggle) {
      return;
    }
    body.classList.add("nav-open");
    sidebarToggle.setAttribute("aria-expanded", "true");
  };

  if (sidebarToggle) {
    sidebarToggle.addEventListener("click", () => {
      if (body.classList.contains("nav-open")) {
        closeSidebar();
        return;
      }
      openSidebar();
    });
  }

  if (sidebarClose) {
    sidebarClose.addEventListener("click", closeSidebar);
  }

  sidebarLinks.forEach((link) => {
    link.addEventListener("click", () => {
      if (window.matchMedia("(max-width: 980px)").matches) {
        closeSidebar();
      }
    });
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeSidebar();
    }
  });

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
      initializeClientMatchCarousels();
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

  const clientMatchCarouselMetrics = (track, cards) => {
    const total = cards.length;
    if (total === 0) {
      return { total: 0, visible: 0, firstVisible: 0, lastVisible: 0, step: 0, atStart: true, atEnd: true };
    }
    const step = total > 1 ? cards[1].offsetLeft - cards[0].offsetLeft : cards[0].offsetWidth;
    const safeStep = Math.max(1, step);
    let firstVisible = 0;
    let nearestDistance = Number.POSITIVE_INFINITY;
    cards.forEach((card, index) => {
      const distance = Math.abs(card.offsetLeft - track.scrollLeft);
      if (distance < nearestDistance) {
        nearestDistance = distance;
        firstVisible = index;
      }
    });
    const viewportEnd = cards[firstVisible].offsetLeft + track.clientWidth + 2;
    let visible = 0;
    for (let index = firstVisible; index < total; index += 1) {
      if (cards[index].offsetLeft + cards[index].offsetWidth <= viewportEnd) {
        visible += 1;
      }
    }
    visible = Math.max(1, Math.min(total, visible));
    const lastVisible = Math.min(total - 1, firstVisible + visible - 1);
    const maxScroll = Math.max(0, track.scrollWidth - track.clientWidth);
    return {
      total,
      visible,
      firstVisible,
      lastVisible,
      step: safeStep,
      atStart: track.scrollLeft <= 2,
      atEnd: track.scrollLeft >= maxScroll - 2,
    };
  };

  const updateClientMatchCarousel = (carousel) => {
    const track = carousel.querySelector("[data-client-match-track]");
    const prevButton = carousel.querySelector("[data-client-match-prev]");
    const nextButton = carousel.querySelector("[data-client-match-next]");
    const status = carousel.querySelector("[data-client-match-status]");
    if (!(track instanceof HTMLElement) || !(prevButton instanceof HTMLButtonElement) || !(nextButton instanceof HTMLButtonElement)) {
      return;
    }
    const cards = Array.from(track.querySelectorAll(".client-match-card")).filter((card) => card instanceof HTMLElement);
    const metrics = clientMatchCarouselMetrics(track, cards);
    const needsControls = metrics.total > metrics.visible;
    prevButton.hidden = !needsControls || metrics.atStart;
    nextButton.hidden = !needsControls || metrics.atEnd;
    prevButton.disabled = !needsControls || metrics.atStart;
    nextButton.disabled = !needsControls || metrics.atEnd;
    if (status instanceof HTMLElement) {
      status.textContent = needsControls
        ? `${metrics.firstVisible + 1}-${metrics.lastVisible + 1} of ${metrics.total}`
        : "";
    }
  };

  const initializeClientMatchCarousels = () => {
    const carousels = document.querySelectorAll("[data-client-match-carousel]");
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    carousels.forEach((carousel) => {
      const track = carousel.querySelector("[data-client-match-track]");
      const prevButton = carousel.querySelector("[data-client-match-prev]");
      const nextButton = carousel.querySelector("[data-client-match-next]");
      if (!(track instanceof HTMLElement) || !(prevButton instanceof HTMLButtonElement) || !(nextButton instanceof HTMLButtonElement)) {
        return;
      }
      const cards = Array.from(track.querySelectorAll(".client-match-card")).filter((card) => card instanceof HTMLElement);
      if (carousel.dataset.clientMatchCarouselReady === "true") {
        updateClientMatchCarousel(carousel);
        return;
      }
      carousel.dataset.clientMatchCarouselReady = "true";

      const moveByGroup = (direction) => {
        const metrics = clientMatchCarouselMetrics(track, cards);
        if (metrics.total <= metrics.visible || metrics.step <= 0) {
          return;
        }
        const currentIndex = Math.round(track.scrollLeft / metrics.step);
        const targetIndex = Math.max(
          0,
          Math.min(metrics.total - metrics.visible, currentIndex + direction * metrics.visible),
        );
        track.scrollTo({
          left: cards[targetIndex].offsetLeft - cards[0].offsetLeft,
          behavior: reducedMotion ? "auto" : "smooth",
        });
      };

      prevButton.addEventListener("click", () => moveByGroup(-1));
      nextButton.addEventListener("click", () => moveByGroup(1));
      track.addEventListener("scroll", () => {
        window.requestAnimationFrame(() => updateClientMatchCarousel(carousel));
      }, { passive: true });
      track.addEventListener("keydown", (event) => {
        if (event.key === "ArrowLeft") {
          event.preventDefault();
          moveByGroup(-1);
        }
        if (event.key === "ArrowRight") {
          event.preventDefault();
          moveByGroup(1);
        }
      });
      updateClientMatchCarousel(carousel);
    });

    if (!carouselResizeHandlerAttached && carousels.length > 0) {
      carouselResizeHandlerAttached = true;
      window.addEventListener("resize", () => {
        window.requestAnimationFrame(() => {
          document.querySelectorAll("[data-client-match-carousel]").forEach(updateClientMatchCarousel);
        });
      });
    }
  };

  initializeClientMatchCarousels();

  if (!container) {
    return;
  }

  const refreshSeconds = Number(container.getAttribute("data-refresh-seconds") || "0");
  if (!Number.isFinite(refreshSeconds) || refreshSeconds <= 0) {
    return;
  }

  window.setInterval(runPartialRefresh, refreshSeconds * 1000);
})();
