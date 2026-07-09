# AutoPost-Studio

A small, open source web app for reviewing and publishing pre-made videos to
TikTok using TikTok's official Login Kit and Content Posting API (Direct Post).

There is no shared server and no hosted version of this app. Everyone who
uses it runs their own copy on their own computer and connects their own
TikTok account. Nothing about your account, your videos, or your credentials
is ever sent anywhere except directly to TikTok's own API.

## What it does

1. You connect your own TikTok account through TikTok's official login screen.
2. The app shows you a video waiting in your `review/` folder, along with its
   caption.
3. Before posting, you manually:
   - choose who can see the post (nothing is pre-selected)
   - choose whether comments, duets, and stitches are allowed (all off by
     default)
   - confirm you consent to publish
4. Only once all three are set does the Publish button activate. The app then
   sends the video to TikTok using Direct Post.

## Setup

### 1. Register your own TikTok app

Go to [developers.tiktok.com](https://developers.tiktok.com/), create an app,
and add these products:
- **Login Kit**
- **Content Posting API** (with Direct Post enabled)

Add the following as a Redirect URI on your TikTok app — this must be exact:
```
https://infiniteproof.github.io/AutoPost-Studio/callback
```

> **Why a GitHub Pages URL?** TikTok requires a publicly accessible HTTPS
> redirect URI. This app uses a hosted callback page that receives the
> authorization code from TikTok and forwards it to your local app at
> `localhost:5000/auth/callback`. Nothing is stored on GitHub Pages — it's
> just a relay.

You'll be given a **Client Key** and **Client Secret** — keep the secret
private, never commit it to a public repo.

### 2. Install dependencies

```bash
pip install flask requests
```

### 3. Set your environment variables

```bash
# macOS/Linux
export TIKTOK_CLIENT_KEY="your_client_key"
export TIKTOK_CLIENT_SECRET="your_client_secret"

# Windows (PowerShell)
setx TIKTOK_CLIENT_KEY "your_client_key"
setx TIKTOK_CLIENT_SECRET "your_client_secret"
```

### 4. Add a video to review

Create a `review/` folder **one level above** `app.py` (i.e. next to the
repo folder, not inside it), and put an `.mp4` file in it. Optionally add a
matching `.json` file with the same name containing:
```json
{
  "title": "Your video title",
  "social_caption": "Caption to use when posting"
}
```

### 5. Run it

```bash
python app.py
```

Open `http://localhost:5000`, click **Connect TikTok Account**, log in, and
you'll land on the review screen.

## Notes

- This app only ever talks to TikTok's own API directly — there's no
  middleman server.
- Access tokens are saved locally to `tiktok_token.json` and are never
  uploaded anywhere.
- TikTok requires Direct Post integrations to go through an audit before
  posts can be public; until then, posts are restricted to private
  (`SELF_ONLY`) viewing. See TikTok's
  [Content Sharing Guidelines](https://developers.tiktok.com/doc/content-sharing-guidelines)
  for details.

## License

AutoPost-Studio is licensed under the **Business Source License 1.1**.

- Free to download, run, and modify for personal, non-commercial use.
- Commercial use (selling it, building a paid hosted service on top of it, etc.) requires a separate license.
- On January 1, 2028, the license automatically converts to MIT.

See `LICENSE` for the full terms. For commercial licensing, contact [infiniteproof9@gmail.com](mailto:infiniteproof9@gmail.com).
