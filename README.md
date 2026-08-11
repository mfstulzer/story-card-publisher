# story-card-publisher

Publishes an Instagram story card for @markstulzer for **every** post Typefully
sends that day, shortly after each one goes out.

Runs on GitHub Actions so it does not depend on a laptop being awake.

**How it decides what to card:** the workflow fires hourly across the posting
window and each run cards every post of the day that has *already gone out* and
is not carded yet. The schedule is deliberately dumb — the clock comparison in
`publish.py` is what stops a card going up before its tweet does. Earlier
versions used one cron per expected posting slot, which quietly lost coverage
every time the Typefully queue changed shape. Threads are skipped; a card only
holds one tweet.

- `publish.py` — asks Typefully what goes out today, renders the card, publishes to Instagram
- `render_story.py` — the card renderer (Pillow)
- `cards/` — rendered cards, served publicly via jsDelivr (Instagram fetches by URL)
- `state/published.json` — draft ids already posted, so a rerun cannot double-post

**This repo is public on purpose.** Instagram will only accept an image it can
fetch over the open internet, and serving from a public repo via jsDelivr avoids
needing a separate host or a cross-repo access token. Nothing sensitive lives
here: every credential is a GitHub Actions secret, never a file.
