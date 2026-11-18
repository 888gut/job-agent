# Offer Ledger — Setup

Offer Ledger is a local app. Its agent, **Job Agent**, finds Handshake jobs, matches them to your two resumes, tailors a resume when a job sits a bit outside them, and waits for your one-click approval before it applies. Every application is logged to a Google Sheet in your storage Gmail account.

## 1. Start it
Double-click **`start.bat`**. The first run installs everything, puts an **Offer Ledger** icon on your desktop and in the Start menu, then opens the dashboard at <http://127.0.0.1:8765>.

After that, just use the **Offer Ledger** icon. It always runs the latest code in this folder: if the code changed since the server started, it restarts it first (never in the middle of a search or an application run), then opens the dashboard in Chrome. To add the icon again (say, after moving the folder), run `powershell -ExecutionPolicy Bypass -File scripts\install-desktop-icon.ps1`.
Until you clear it, the dashboard shows **sample data**. Use the yellow bar to clear it.

## 2. Claude API key
1. Create a key at <https://console.anthropic.com> → API Keys.
2. Open `.env` in this folder and set `ANTHROPIC_API_KEY=...`.
3. Close the black server window and run `start.bat` again.

## 3. Google OAuth client (one time, about 5 minutes)
This lets the agent use **your own** Google project. You'll sign in on Google's page; the agent never sees your password.
1. Go to <https://console.cloud.google.com>, create a project (any name, e.g. "Offer Ledger").
2. **APIs & Services → Library**: enable **Google Sheets API**, **Google Drive API**, **Google Docs API** and **Gmail API**.
3. **APIs & Services → OAuth consent screen**: choose *External*, fill in the app name and your email.
   Under **Test users**, add **both** Gmail addresses (the Handshake one and the storage one).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** → type **Desktop app**.
5. Download the JSON and save it in this folder as **`credentials.json`**.

## 4. Connect accounts (Settings page)
- **Storage Gmail → Connect**: pick the *storage* account on Google's page. The agent creates the
  "Job Agent - Applications" Sheet and a "Job Agent Resumes" Drive folder there.
- **Handshake Gmail → Connect** (optional): pick the *Handshake* account. This gives read-only access so
  Handshake emails can mark applications Confirmed, Interview or Rejected.
- **Sign in to Handshake**: the dashboard's **Handshake** tab opens with a live view of the agent's
  browser, and your Handshake Gmail address is filled in for you. Finish signing in there however you
  normally do (school SSO, Google, etc.), then click **Done signing in**. The session is remembered in
  `data/browser_profile`. The same tab lets you watch the agent while it searches and applies.

## 5. Upload your resumes (Resumes page)
Upload Resume A and Resume B as PDFs. These two **base resumes are never edited**; jobs that fit well get your original PDF, byte for byte.

Then click **Create Google Doc master** under each one (it needs the storage account connected), and then **Format check**.
The master is a Google Doc that mirrors your PDF's layout: Times fonts, small-caps headings with rules, right-aligned dates, italic company names and bullets. If anything looks off, fix it once in the master Doc and every future tailored copy inherits the fix. Don't change the master's wording.

### Tailored resumes and branches
- Jobs scoring between the minimum match and "Tailor when below" get a **tailored resume**: a one-off copy of the master Doc in your storage account's Drive, with reworded bullets swapped in.
- Each job is tagged with a **branch** (job family, e.g. "Supply chain operations"). The first job in a branch gets a new tailored Doc. Later jobs in the **same branch reuse that Doc** instead of making another one.
- **Tailor fresh for this job** makes a new one-off Doc anyway. **Use original resume** sends the base PDF and keeps the branch Doc.
- You can edit a tailored Doc yourself in Google Docs. The agent exports a fresh PDF from it each time it applies, so your edits are included.
- The Resumes page lists every tailored Doc by branch, with how many times each has been used. The Sheet has a Branch column and a link to the Doc that was sent.

## 6. Run
- **Settings**: set your search terms, minimum match and daily limit.
- **Run search now** (or turn on the schedule). Each new posting is read, scored against both resumes, and either skipped or added to the **Approval Queue**.
- In the queue, open a job to see why it fits, its gaps, the resume changes side by side, and a ready-to-paste answer. Then click **Approve & Submit**, or **Skip**.

### Dry run (on by default)
With dry run on, approving attaches your resume in the agent's browser window and **stops before the final Submit**, so you can watch it work. Once you trust it, turn dry run off in Settings.

### When the agent hands a job to you
The job moves to **Needs you** when:
- the posting is applied for on the employer's own site,
- the form has extra required questions or asks for more documents,
- Handshake shows a CAPTCHA or asks you to sign in again.

Click **Open in browser**, finish the application, then click **I applied** so it's logged.

## Cover letters
- **Settings > Cover letters** (on by default): the agent writes a letter when an application asks for one. Tick **Always** to have one written for every application you approve.
- The **Cover Letters** tab: pick a job from your queue (or paste a posting), pick the resume it's written from (the agent's pick is preselected; a tailored branch resume keeps the letter in step with the resume you send), and optionally add a prompt such as "Lead with my shipping work" or the application's own question.
- The letter may only say what your resumes, transcript and confirmed Career Facts say. A sentence that adds anything else is rewritten once, then left out, and listed under the letter. To use something new, confirm it on Career Facts first.
- Each letter is a Google Doc in the storage account's Drive folder. Edit it there if you like: the agent exports a fresh PDF each time it attaches it. Letters uploaded to Handshake are reused, and unused copies are cleaned up weekly like resume copies.

## What the tailoring will and won't do
Tailoring rewords and reorders what's already in your resume to match the job's language. It only touches bullets, the summary and skill lines. Your name, contact line, headings, job titles, companies and dates are locked.

Two checks block an edit:
1. **No new facts:** no employer, name, number, tool or skill that isn't already in your resume.
2. **Same layout:** the new text must fit in the same number of lines, measured with your PDF's actual fonts, so the page stays identical.

Blocked edits are listed under each job so you can see them.

## Files
| Path | What |
|---|---|
| `app/` | Python backend (FastAPI) |
| `web/` | Dashboard (HTML/CSS/JS) |
| `data/agent.db` | Local database |
| `data/resumes/` | Your originals and tailored versions |
| `data/tokens/` | Google sign-in tokens (delete to disconnect) |
| `data/browser_profile/` | Handshake browser session |

Run the tests with `.venv\Scripts\python.exe -m pytest`.
