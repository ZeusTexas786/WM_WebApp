const menuButton = document.querySelector(".menu-button");
const mobileMenu = document.querySelector(".mobile-menu");
const trialDialog = document.querySelector("#trialDialog");
const cookieCard = document.querySelector("#cookieCard");
const billingButtons = document.querySelectorAll("[data-billing]");
const priceEls = document.querySelectorAll(".price");

function closeMenu() {
  document.body.classList.remove("menu-open");
  if (menuButton) {
    menuButton.setAttribute("aria-expanded", "false");
  }
  if (mobileMenu) {
    mobileMenu.setAttribute("aria-hidden", "true");
  }
}

if (menuButton && mobileMenu) {
  menuButton.addEventListener("click", () => {
    const isOpen = document.body.classList.toggle("menu-open");
    menuButton.setAttribute("aria-expanded", String(isOpen));
    mobileMenu.setAttribute("aria-hidden", String(!isOpen));
  });

  mobileMenu.addEventListener("click", (event) => {
    if (event.target.matches("a, button")) {
      closeMenu();
    }
  });
}

function openTrial() {
  if (typeof trialDialog.showModal === "function") {
    trialDialog.showModal();
    document.body.classList.add("dialog-open");
  } else {
    window.location.hash = "trial";
  }
}

document.querySelectorAll(".open-trial, .open-demo").forEach((button) => {
  button.addEventListener("click", openTrial);
});

if (trialDialog) {
  trialDialog.addEventListener("close", () => {
    document.body.classList.remove("dialog-open");
  });

  trialDialog.addEventListener("click", (event) => {
    const rect = trialDialog.getBoundingClientRect();
    const clickedBackdrop =
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom;

    if (clickedBackdrop) {
      trialDialog.close();
    }
  });
}

billingButtons.forEach((button) => {
  button.addEventListener("click", () => {
    const billing = button.dataset.billing;

    billingButtons.forEach((item) => item.classList.toggle("is-selected", item === button));
    priceEls.forEach((price) => {
      price.textContent = price.dataset[billing];
      const note = price.nextElementSibling;
      if (note) {
        note.textContent =
          billing === "monthly"
            ? "/ month, billed monthly"
            : "/ month, billed annually";
      }
    });
  });
});

if (cookieCard) {
  const cookieChoice = localStorage.getItem("webmonkie-cookie-choice");
  if (cookieChoice) {
    cookieCard.classList.add("is-hidden");
  }

  cookieCard.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => {
      localStorage.setItem("webmonkie-cookie-choice", button.textContent.trim().toLowerCase());
      cookieCard.classList.add("is-hidden");
    });
  });
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeMenu();
  }
});
