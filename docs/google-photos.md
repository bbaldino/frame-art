# Google Photos for frame-art

frame-art reads your Google Photos only through Google's **Photos Picker**. You
choose photos (or an album) in Google's own picker, and frame-art copies just
those. Since 31 March 2025 Google lets no app read a whole library, so frame-art
can't watch an album: pick again when you add photos to it, and frame-art skips
the ones it already has.

## One-time setup (about 10 minutes, in the Google Cloud console)

1. Go to <https://console.cloud.google.com/> and create a project, e.g. "frame-art",
   or reuse one you already have. Its consent screen and publishing status are
   shared with everything else in it; give frame-art its own OAuth client (step 4).
2. **APIs & Services → Library**: search for **Photos Picker API** and enable it.
3. **APIs & Services → OAuth consent screen** (Google Auth Platform):
   - Audience: **External**, publishing status **Testing**.
   - Add your own Google account under **Test users**.
   - Scopes: you don't need to add any here; frame-art asks for
     `photospicker.mediaitems.readonly` when you sign in.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app**. Name it "frame-art". (If the form asks for
     JavaScript origins or redirect URIs, it is set to Web application: change it.)
   - Download the JSON.
5. Save the JSON as `google-photos/client.json` in frame-art's keys directory and
   make it private:
   - On the server: the stack's `keys/` directory (it is `/keys` inside the
     container), so `keys/google-photos/client.json`.
   - `chmod 600 keys/google-photos/client.json`
   - Never paste it into a chat or the message bus.

Because the app stays in **Testing**, Google ends the sign-in after 7 days (its
token reply carries `refresh_token_expires_in`).
frame-art then shows **Sign in again** on the Your photos page; nothing else
breaks.

## Signing in

1. On **Your photos**, click **Sign in with Google** and approve access.
2. Google then sends your browser to an address starting with
   `http://127.0.0.1:8765/`, and the page **fails to load. That is expected.**
3. Copy that page's whole address from the address bar and paste it into the
   box on frame-art's page. frame-art keeps the sign-in on the server (readable
   only by frame-art) and never shows it on a page.

## Adding photos

Click **+ Add from Google Photos**, then **Open Google Photos**. Choose photos,
press **Done**, and come back to frame-art's tab: the photos arrive within a
minute and get a suggested kind (portrait or scene).
