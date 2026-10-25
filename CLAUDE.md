# CLAUDE.md

Django 6 app (Google login via allauth, gated by the `AllowedUser` table) that plans weeknight dinners. See README.md for the user-facing flow and setup.

## Layout

- `meals/services.py` — the only entry point for plan changes: `generate_week`, `replace_with_suggestion` / `replace_with_recipe`, `grocery_list`, `push_to_cart`, `send_plan_email`. Views, the `generate_meal_plan` command and `/internal/generate-week/` (which Airflow's `meal_plan_weekly` DAG calls) all go through it.
- `meals/planner.py` — the Claude call. It uses `beta.messages.parse` with Pydantic output models (`WeekChoices`), effort `medium`, and server-side refusal fallbacks. The household context (scored recipes, rating comments, the last 6 weeks of meals, swap reasons, archived recipes) is built in `_household_context`.
- `meals/calendar_sync.py` — Google Calendar through a service account. Failures are logged, not raised, so a calendar outage never loses a plan.
- `meals/kroger.py` — Kroger API: user OAuth tokens are kept in the `KrogerAuth` singleton, product and location search use a client-credentials token, and `PUT /cart/add` fills the cart. Remembered UPCs live in `GroceryProduct`.
- UI: server-rendered Bootstrap 5 templates styled by `static/global/css/global.css` ("Cast Iron" tokens, `--mp-*`, mirroring Simpli Budget's mobile-first shell: phone tab bar, bottom sheets, mono titles). Dark is the default; the `theme` cookie (set by `static/global/js/global.js`) picks light, read by `meals.context_processors.ui`, which also sets `nav_section`. Use the tokens, not hard-coded colours.
- `HouseholdSettings` (pk=1) and `KrogerAuth` (pk=1) are singletons, both edited in /admin/.

## Conventions

- Weeks run Sunday through Saturday (`week_start_for`). Plans are unique per `week_start`, and meals are unique per (plan, date).
- `generate_week` is idempotent: it only fills empty days, unless `regenerate=True`, which re-picks unlocked meals.
- Tests (`SECRET_KEY=dev python manage.py test meals`) run on SQLite with Claude/Calendar/Kroger mocked. `PlannerRequestTests` goes through the real SDK request/parse path using an httpx2 MockTransport.
- Never commit `.env*` or `secrets/`.
