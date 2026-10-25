# CLAUDE.md

Django 6 app (Google login via allauth, gated by the `AllowedUser` table) that plans weeknight dinners. See README.md for the user-facing flow and setup.

## Layout

- `meals/services.py` — the only entry point for plan changes: `generate_week`, `replace_with_suggestion` / `replace_with_recipe`, `grocery_list`, `push_to_cart`, `send_plan_email`. Views, the `generate_meal_plan` command and `/internal/generate-week/` (which Airflow's `meal_plan_weekly` DAG calls) all go through it.
- `meals/planner.py` — the Claude call. It uses `beta.messages.parse` with Pydantic output models (`WeekChoices`), effort `medium`, and server-side refusal fallbacks. The household context (scored recipes, rating comments, the last 6 weeks of meals, swap reasons, archived recipes) is built in `_household_context`.
- `meals/calendar_sync.py` — Google Calendar through a service account. Failures are logged, not raised, so a calendar outage never loses a plan.
- `meals/kroger.py` — Kroger API: user OAuth tokens are kept in the `KrogerAuth` singleton, product and location search use a client-credentials token, and `PUT /cart/add` fills the cart. Remembered UPCs live in `GroceryProduct`.
- UI: server-rendered Bootstrap 5 templates styled by `static/global/css/global.css` ("Cast Iron" tokens, `--mp-*`, mirroring Simpli Budget's mobile-first shell: phone tab bar, bottom sheets, mono titles). Dark is the default; the `theme` cookie (set by `static/global/js/global.js`) picks light, read by `meals.context_processors.ui`, which also sets `nav_section`. Use the tokens, not hard-coded colours.
- Preferences: `/preferences/` sets the household's cooking time (`max_weeknight_minutes`, one of `COOK_TIMES`), `protein_preferences`, `favorite_cuisines`, `meal_styles` and `dietary_notes` on `HouseholdSettings`, plus each member's own loves/dislikes in `MemberPreferences`. Option lists live in `models.py`; `planner._preferences` turns them into the `preferences` block of the household context, and `SYSTEM_PROMPT` says how strictly to apply each.
- `HouseholdSettings` (pk=1) and `KrogerAuth` (pk=1) are singletons. `/settings/` (any member) connects King Soopers and picks the store (`kroger_location_id` / `_name`, via `kroger.find_locations`), and sets `calendar_id` (checked with `calendar_sync.check_access` before saving, then this week is synced); everything else in `HouseholdSettings` is edited in /admin/. Until preferences, Kroger, the store, a calendar and a first plan exist, the week page shows a setup checklist (`views._setup_steps`).

## Conventions

- Weeks run Sunday through Saturday (`week_start_for`). Plans are unique per `week_start`, and meals are unique per (plan, date).
- `generate_week` is idempotent: it only fills empty days, unless `regenerate=True`, which re-picks unlocked meals.
- Tests (`SECRET_KEY=dev python manage.py test meals`) run on SQLite with Claude/Calendar/Kroger mocked. `PlannerRequestTests` goes through the real SDK request/parse path using an httpx2 MockTransport.
- Production must run on Postgres (`DB_HOST` set). With `DEBUG` off and no `DB_HOST`, settings raise `ImproperlyConfigured` rather than fall back to SQLite, because a SQLite file inside the container is wiped on every redeploy (that wiped `AllowedUser` and every plan twice). Tests and `DEBUG=1` still use SQLite.
- Never commit `.env*` or `secrets/`.
