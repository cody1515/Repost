# Repost App

This app runs on Railway around the clock. It watches the creators you've added, downloads their new videos, and posts them to your Instagram Reels and TikTok with their credit. You control it from your phone or any browser.

---

## 1. Put the code on GitHub
Create a **private** repo, for example `repost-app`, and upload everything in this folder. (Or tell Claude the repo name and it can push the code for you.)

## 2. Deploy on Railway
1. Go to railway.com. Choose **New Project → Deploy from GitHub repo** and pick `repost-app`.
2. **Add a volume.** This is where your creators, settings, and post history are saved. Without it, they're wiped every time the app redeploys.
   Click the service, then **Settings → Volumes → Add Volume**. Set the mount path to `/data`.
3. **Add variables** under the service's **Variables** tab:
   | Name | Value |
   |---|---|
   | `APP_PASSWORD` | the password you'll use to log in to the app |
   | `TIKTOK_CLIENT_KEY` | from step 4 (add it later) |
   | `TIKTOK_CLIENT_SECRET` | from step 4 (add it later) |
4. **Get a web address.** Go to **Settings → Networking → Generate Domain**. You'll get something like `repost-app-production.up.railway.app`.
5. Open that address on your phone and log in.
   - iPhone: tap **Share → Add to Home Screen**.
   - Android: tap **⋮ → Add to Home screen**.

   It'll open like a normal app after that.

## 3. Connect your Instagram (official API, one time)
1. Go to developers.facebook.com. Choose **My Apps → Create App** (type: Business), then add the **Instagram** product. Use the "API setup with Facebook login" option.
2. Open **Graph API Explorer** and select your app. Add these permissions: `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`. Click **Generate Access Token**.
3. In the **Access Token Debugger**, click **Extend Access Token**.
4. Back in Graph API Explorer, using that long-lived token, run `me/accounts`. Copy your Page's `access_token`. It doesn't expire.
5. Run `PAGE_ID?fields=instagram_business_account`. Copy the `id` it returns.
6. In the app, go to **Settings → Your Instagram**. Paste the ID and the token, then tap **Save settings**.

## 4. Connect your TikTok (official API, one time)
1. Go to developers.tiktok.com and choose **Manage apps → Connect an app**. Add **Login Kit** and **Content Posting API**.
2. For the **Redirect URI**, paste the one shown in the app under **Settings → Your TikTok**. It's `https://YOUR-DOMAIN/tiktok/callback`.
3. Copy the Client key and Client secret into the Railway variables from step 2. Railway restarts the app on its own.
4. In the app, tap **Connect TikTok** and approve.

**Drafts vs. direct posting:** until TikTok approves your developer app, anything it posts directly is forced to private. Leave the mode on **Drafts** for now. In Drafts mode, each video lands in your TikTok inbox, and the app's Activity list has a **Copy TikTok caption** button for it. After TikTok approves your app, switch the mode to **Post directly**.

## 5. Add creators
Go to **Creators → + Add**. Pick where to pull their videos from, and enter both handles if you have them. The credit tag uses whichever handle matches the app it's posting to.

When you add a creator, only videos they post from then on get reposted. To also post their last few videos, change **Backfill** in Settings.

---

## Good to know
- **Instagram may block pulling.** Instagram often blocks cloud servers from reading profiles, while TikTok pulling usually works. If checks on an Instagram creator keep failing, you have two options:
  - Set that creator to pull from their TikTok instead.
  - Add a spare account's `sessionid` cookie under **Settings → Your Instagram → Optional login**.

  The **Add a video by link** box always works as a backup.
- **Failed posts retry on their own.** Each one gets 3 tries. After that it shows as **Failed** with the reason and a **Retry** button.
- **The downloader stays up to date.** The tool that downloads videos (yt-dlp) updates itself every time Railway restarts the app. If TikTok downloads suddenly start failing, redeploy from Railway.
- **Cost:** Railway's Hobby plan is $5/month, and this app fits inside it.
