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
