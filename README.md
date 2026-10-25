# Meal Planner

Plans the household's weeknight dinners with Claude, learns from 👍/👎 ratings and swaps,
puts each dinner on a shared Google Calendar, emails the week's summary, and fills the
King Soopers cart through the Kroger API.

## How a week works

1. **Sunday 8am (Denver)** — Airflow's `meal_plan_weekly` DAG POSTs to
   `/internal/generate-week/`. The app asks Claude for dinners on the configured weekdays
   (default Mon–Fri), mixing well-liked repeats with new recipes, avoiding recent meals,
   disliked/archived recipes and recent swap reasons. Each dinner becomes a calendar event,
   and everyone on the allowed list with *receives plan email* gets the summary. The email
   also links to last week's meals so people can rate them.
2. **During the week** — anyone signed in can open the plan to:
   - 👍 or 👎 a recipe (with an optional comment). Ratings are per person, per recipe.
   - **Replace** a dinner. Either type a hint ("something quicker", "no pasta") and Claude
     suggests one, or pick one of the favorites. The calendar event updates, and the swapped-out
     dish plus the hint are remembered as a soft dislike.
   - **Lock** a dinner, so that *Regenerate unlocked* keeps it.
   - **Never suggest** a recipe again (archive it).
3. **Shopping** — the Groceries page merges every ingredient by grocery item and leaves
   pantry staples unchecked. *Add to King Soopers cart* matches each item to a product at your
   store and adds 1 of each for pickup. The chosen product is remembered per item; fix wrong
   matches in Admin → Grocery products. Checkout still happens in the King Soopers app,
   because the Kroger API can't place orders.

## Setup

1. **Database** — create a `meal_planner` database on the host Postgres. Migrations run
   automatically when the container starts.
2. **Environment** — set these in the Portainer stack's *Environment variables* (Portainer writes
   them to `stack.env`; for a local `docker compose up`, put them in a `stack.env` here). Never commit either:
   - `SECRET_KEY`, `APP_BASE_URL` (e.g. `https://meals.carrell.us`)
   - `DB_HOST` (`host.docker.internal`), `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_PORT`
   - `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` — OAuth client for Google sign-in. Add
     `<APP_BASE_URL>/accounts/google/login/callback/` as an authorized redirect URI.
   - `ANTHROPIC_API_KEY`, and optionally `CLAUDE_MODEL` (default `claude-opus-5-5`)
   - `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` (Gmail app password), `DEFAULT_FROM_EMAIL`
   - `KROGER_CLIENT_ID`, `KROGER_CLIENT_SECRET` — from developer.kroger.com. Register the
     redirect URI `<APP_BASE_URL>/kroger/callback/`; the app needs the Cart, Products and
     Identity APIs.
   - `INTERNAL_API_SECRET` — the same value as `MEAL_PLANNER_SECRET` in Airflow's `.env`
3. **Google Calendar** — put a service account JSON key at
   `/home/michael/apps/meal_planner/secrets/google_service_account.json` (mounted by absolute path).
   The existing gspread service account works if the Calendar API is enabled for its project.
   Share the target calendar with the service account's email ("Make changes to events").
4. **Start** — deploy the Portainer stack (git-backed, this repo). The app listens on host port 8006 and joins
   the `proxy` network as `meal_planner`. Add an NPM proxy host and a Pi-hole record for the
   public hostname.
5. **First run** — run `docker exec -it meal_planner python manage.py createsuperuser`. Then in
   `/admin/`:
   - Add every household member under *Allowed users*.
   - Fill in *Household settings*: weekdays, servings, time limit, dietary notes, calendar
     ID and store.
   - To find the store ID, run `docker exec meal_planner python manage.py kroger_locations <zip>`.
6. **Connect King Soopers** — open Groceries → *Connect your account* and sign in with the
   household's King Soopers login.

## Manual runs

```bash
docker exec meal_planner python manage.py generate_meal_plan [--week-start 2026-11-01] [--regenerate] [--email]
```

## Tests

```bash
SECRET_KEY=dev python manage.py test meals   # SQLite, no network; Claude/Calendar/Kroger are mocked
```
