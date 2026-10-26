// Collect/pin/ban without a full page load, so the page keeps its scroll position.
// Without JavaScript the forms still work: the redirect lands on the card's anchor.
// Resolves to the card now on the page (the old one, if the work left the filter).
async function submitAction(form, submitter) {
  const card = form.closest("article.card");
  card.classList.add("updating");
  try {
    // The POST redirects back to this page, so the response is the fresh page.
    const resp = await fetch(form.action, {
      method: "POST",
      body: new FormData(form, submitter),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const page = new DOMParser().parseFromString(await resp.text(), "text/html");
    const fresh = page.getElementById(card.id);
    if (fresh) {
      card.replaceWith(fresh);
      return fresh;
    }
    // The work no longer matches the page's filter (e.g. banned under "Eligible").
    card.classList.remove("updating");
    card.classList.add("gone");
    card.querySelector(".actions").textContent = "Updated; hidden by this filter";
    return card;
  } catch (err) {
    card.classList.remove("updating");
    form.submit(); // fall back to a normal post
    return card;
  }
}

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!form.closest(".actions") || form.closest("#viewer")) return;
  const card = form.closest("article.card");
  if (!card || !card.id) return;
  event.preventDefault();
  submitAction(form, event.submitter);
});

// Tag autocomplete on the catalogue. Without JavaScript the box still works:
// whatever is typed is submitted as one more tag.
const tagInput = document.getElementById("tag-input");
if (tagInput) {
  const form = tagInput.form;
  const list = document.getElementById("tag-suggest");
  let items = [];
  let active = -1;
  let timer;
  let seq = 0;

  const close = () => {
    list.hidden = true;
    active = -1;
    tagInput.removeAttribute("aria-activedescendant");
  };

  const choose = (item) => {
    tagInput.value = item.tag;
    close();
    form.submit();
  };

  const highlight = (i) => {
    active = i;
    [...list.children].forEach((li, n) => li.setAttribute("aria-selected", n === i));
    if (i >= 0) {
      tagInput.setAttribute("aria-activedescendant", list.children[i].id);
      list.children[i].scrollIntoView({ block: "nearest" });
    }
  };

  const show = () => {
    list.replaceChildren(
      ...items.map((item, n) => {
        const li = document.createElement("li");
        li.id = `tag-opt-${n}`;
        li.setAttribute("role", "option");
        const name = document.createElement("span");
        name.textContent = item.tag;
        const meta = document.createElement("span");
        meta.className = "muted";
        meta.textContent = `${item.kind} · ${item.count}`;
        li.append(name, meta);
        // mousedown, not click: it fires before the input's blur closes the list.
        li.addEventListener("mousedown", (e) => {
          e.preventDefault();
          choose(item);
        });
        return li;
      }),
    );
    list.hidden = items.length === 0;
    highlight(-1);
  };

  const fetchSuggestions = async () => {
    const params = new URLSearchParams({ q: tagInput.value.trim() });
    if (location.pathname === "/collection") params.set("collection", "1");
    for (const name of ["source", "kind"]) {
      const value = form.elements[name]?.value;
      if (value) params.set(name, value);
    }
    form.querySelectorAll('input[type="hidden"][name="tag"]').forEach((el) => {
      params.append("tag", el.value);
    });
    const mine = ++seq;
    try {
      const resp = await fetch(`/tags.json?${params}`);
      if (!resp.ok || mine !== seq) return;
      items = await resp.json();
      if (mine === seq && document.activeElement === tagInput) show();
    } catch {
      // Suggestions are a convenience; typing a tag still works.
    }
  };

  tagInput.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(fetchSuggestions, 120);
  });
  tagInput.addEventListener("focus", fetchSuggestions);
  tagInput.addEventListener("blur", close);
  tagInput.addEventListener("keydown", (e) => {
    if (list.hidden) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      highlight((active + 1) % items.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      highlight(active <= 0 ? items.length - 1 : active - 1);
    } else if (e.key === "Enter" && active >= 0) {
      e.preventDefault();
      choose(items[active]);
    } else if (e.key === "Escape") {
      close();
    }
  });
}

// A closer look: click a card's picture to see it large; arrows step through
// the cards on the page, Esc or a click outside the picture closes it. The
// card's buttons come along (keys C, P and B), acting on the card behind.
const viewer = document.getElementById("viewer");
if (viewer) {
  const big = viewer.querySelector("img");
  const caption = viewer.querySelector(".viewer-caption");
  const actions = viewer.querySelector(".viewer-actions");
  let cards = []; // card ids: a card is replaced whenever its buttons are used
  let index = -1;
  let busy = false;

  const card = () => document.getElementById(cards[index]);

  const showActions = () => {
    const own = card()?.querySelector(".actions");
    actions.replaceChildren(...(own ? [own.cloneNode(true)] : []));
  };

  const show = (i) => {
    index = (i + cards.length) % cards.length;
    const small = card().querySelector("img[data-full]");
    // The thumbnail is cached, so show it at once and swap in the large one.
    big.src = small.currentSrc || small.src;
    big.alt = small.alt;
    const full = new Image();
    full.referrerPolicy = "no-referrer";
    const id = cards[index];
    full.onload = () => {
      if (cards[index] === id) big.src = full.src;
    };
    full.src = small.dataset.full;
    caption.replaceChildren(
      ...[".title", ".who"]
        .map((sel) => card().querySelector(sel)?.cloneNode(true))
        .filter(Boolean),
    );
    showActions();
  };

  // Press the matching button on the card itself, then refresh the copy.
  const act = async (button) => {
    const copies = [...actions.querySelectorAll("button")];
    const original = card()?.querySelectorAll(".actions button")[copies.indexOf(button)];
    if (!original || busy) return;
    busy = true;
    actions.classList.add("updating");
    const id = cards[index];
    await submitAction(original.form, original);
    busy = false;
    actions.classList.remove("updating");
    if (cards[index] === id) showActions();
  };

  document.addEventListener("click", (e) => {
    const img = e.target.closest(".card img[data-full]");
    if (!img) return;
    cards = [...document.querySelectorAll(".card")]
      .filter((c) => c.id && c.querySelector("img[data-full]"))
      .map((c) => c.id);
    show(cards.indexOf(img.closest(".card").id));
    if (!viewer.open) viewer.showModal();
  });
  viewer.addEventListener("submit", (e) => {
    e.preventDefault();
    act(e.submitter);
  });
  viewer.addEventListener("click", (e) => {
    const step = e.target.closest("[data-step]");
    if (step) show(index + Number(step.dataset.step));
    else if (e.target === viewer || e.target.closest("[data-close]")) viewer.close();
  });
  // On the document: a button press replaces the focused button, which can
  // leave focus outside the dialog.
  document.addEventListener("keydown", (e) => {
    if (!viewer.open || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "ArrowRight") show(index + 1);
    else if (e.key === "ArrowLeft") show(index - 1);
    else {
      const button = actions.querySelector(`button[data-key="${e.key.toLowerCase()}"]`);
      if (button) act(button);
    }
  });
  viewer.addEventListener("close", () => big.removeAttribute("src"));
}
