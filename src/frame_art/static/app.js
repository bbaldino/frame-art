// Pin/ban without a full page load, so the catalogue keeps its scroll position.
// Without JavaScript the forms still work: the redirect lands on the card's anchor.
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!form.closest(".actions")) return;
  const card = form.closest("article.card");
  if (!card || !card.id) return;
  event.preventDefault();
  card.classList.add("updating");
  try {
    // The POST redirects back to this page, so the response is the fresh page.
    const resp = await fetch(form.action, {
      method: "POST",
      body: new FormData(form, event.submitter),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const page = new DOMParser().parseFromString(await resp.text(), "text/html");
    const fresh = page.getElementById(card.id);
    if (fresh) {
      card.replaceWith(fresh);
    } else {
      // The work no longer matches the page's filter (e.g. banned under "Eligible").
      card.classList.remove("updating");
      card.classList.add("gone");
      card.querySelector(".actions").textContent = "Updated; hidden by this filter";
    }
  } catch (err) {
    card.classList.remove("updating");
    form.submit(); // fall back to a normal post
  }
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
