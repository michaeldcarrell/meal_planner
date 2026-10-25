import hmac
import json
import logging
import secrets
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Sum
from django.http import Http404, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from meals import calendar_sync, kroger, services
from meals.models import (
    COOK_TIMES, CUISINES, MEAL_SCHEDULE_PRESETS, MEAL_STYLES, MEAL_TYPES, PLAN_WEEKDAYS, PROTEIN_LEVELS, PROTEINS, HouseholdSettings, KrogerAuth, MealPlan,
    MemberPreferences, PlannedMeal, Rating, Recipe, week_start_for,
)

logger = logging.getLogger(__name__)


def _plan_or_404(week_start: str) -> MealPlan:
    try:
        return get_object_or_404(MealPlan, week_start=date.fromisoformat(week_start))
    except ValueError:
        raise Http404()


class Index(LoginRequiredMixin, View):
    def get(self, request):
        return redirect('plan', week_start_for(timezone.localdate()).isoformat())


class Logout(View):
    def get(self, request):
        logout(request)
        return redirect('/')


def _setup_steps() -> list[dict] | None:
    """First-run checklist for the week page, or None once everything is done."""
    household = HouseholdSettings.load()
    steps = [
        {'label': 'Tell the planner what you like', 'done': household.preferences_updated_at is not None,
         'hint': 'Cooking time, proteins, cuisines, and foods anyone won\'t eat.', 'url': '/preferences/'},
        {'label': 'Connect your King Soopers account', 'done': kroger.is_connected(),
         'hint': 'So the grocery list can fill your cart.', 'url': '/settings/#kroger'},
        {'label': 'Choose your King Soopers store', 'done': bool(household.kroger_location_id),
         'hint': 'Products and prices come from this store.', 'url': '/settings/#store'},
        {'label': 'Connect a Google Calendar', 'done': bool(household.calendar_id),
         'hint': 'Each meal shows up as an event with the recipe.', 'url': '/settings/#calendar'},
        {'label': 'Plan your first week', 'done': MealPlan.objects.filter(meals__isnull=False).exists(),
         'hint': 'Meals go on the calendar and the grocery list is built from them.', 'url': None},
    ]
    return None if all(step['done'] for step in steps) else steps


def _move_options(meal: PlannedMeal, meals) -> list[tuple]:
    """(day, meal already there or None) for every other day of the meal's week."""
    taken = {m.date: m for m in meals if m.meal_type == meal.meal_type}
    return [(day, taken.get(day)) for day in meal.plan.days if day != meal.date]


class PlanView(LoginRequiredMixin, View):
    def get(self, request, week_start):
        try:
            start = week_start_for(date.fromisoformat(week_start))
        except ValueError:
            raise Http404()
        plan = MealPlan.objects.filter(week_start=start).first()
        meals = list(plan.meals.select_related('recipe')) if plan else []
        for meal in meals:
            meal.my_rating = meal.recipe.rating_for(request.user)
            meal.votes = list(meal.recipe.ratings.select_related('user'))
        favorites = list(Recipe.objects.filter(archived=False).annotate(total=Sum('ratings__value'))
                         .filter(total__gt=0).order_by('name'))
        for meal in meals:
            meal.swap_favorites = [r for r in favorites if r.pk != meal.recipe_id]
            meal.move_options = _move_options(meal, meals)
        minutes = [m.recipe.total_minutes for m in meals if m.recipe.total_minutes]
        return render(request, 'meals/plan.html', {
            'setup_steps': _setup_steps(),
            'plan': plan,
            'week_start': start,
            'week_end': start + timedelta(days=6),
            'today': timezone.localdate(),
            'meals': meals,
            'favorites': favorites,
            'meal_types': MEAL_TYPES,
            'meals_label': services.meals_label(meals),
            # Name each meal's type once the week has more than dinners in it.
            'show_meal_types': any(m.meal_type != 'dinner' for m in meals),
            'avg_minutes': round(sum(minutes) / len(minutes)) if minutes else None,
            'locked_count': sum(1 for m in meals if m.locked),
            'prev_week': (start - timedelta(days=7)).isoformat(),
            'next_week': (start + timedelta(days=7)).isoformat(),
        })


class GeneratePlan(LoginRequiredMixin, View):
    def post(self, request, week_start):
        start = date.fromisoformat(week_start)
        regenerate = request.POST.get('regenerate') == '1'
        try:
            services.generate_week(start, regenerate=regenerate)
            messages.success(request, 'Plan regenerated.' if regenerate else 'Plan created.')
        except Exception as e:
            logger.exception('generate_week failed')
            messages.error(request, f'Could not generate the plan: {e}')
        return redirect('plan', week_start)


class EmailPlan(LoginRequiredMixin, View):
    def post(self, request, week_start):
        plan = _plan_or_404(week_start)
        sent_to = services.send_plan_email(plan)
        messages.success(request, f"Emailed {', '.join(sent_to)}." if sent_to else 'No email recipients configured.')
        return redirect('plan', week_start)


class MealDetail(LoginRequiredMixin, View):
    def get(self, request, pk):
        meal = get_object_or_404(PlannedMeal.objects.select_related('recipe', 'plan'), pk=pk)
        meal.move_options = _move_options(meal, meal.plan.meals.select_related('recipe'))
        return render(request, 'meals/meal_detail.html', {
            'meal': meal,
            'recipe': meal.recipe,
            'my_rating': meal.recipe.ratings.filter(user=request.user).first(),
            'votes': meal.recipe.ratings.select_related('user'),
            'favorites': Recipe.objects.filter(archived=False).annotate(total=Sum('ratings__value'))
                .filter(total__gt=0).exclude(pk=meal.recipe_id).order_by('name'),
        })


def _back(request, fallback):
    target = request.POST.get('next', '')
    return redirect(target if target.startswith('/') and not target.startswith('//') else fallback)


class RateRecipe(LoginRequiredMixin, View):
    def post(self, request, pk):
        recipe = get_object_or_404(Recipe, pk=pk)
        try:
            value = int(request.POST['value'])
        except (KeyError, ValueError):
            return HttpResponseBadRequest()
        if value == 0:
            Rating.objects.filter(recipe=recipe, user=request.user).delete()
        elif value in (Rating.LIKE, Rating.DISLIKE):
            Rating.objects.update_or_create(recipe=recipe, user=request.user, defaults={
                'value': value, 'comment': request.POST.get('comment', '')[:500],
            })
        else:
            return HttpResponseBadRequest()
        return _back(request, f'/recipes/{recipe.pk}/')


class ReplaceMeal(LoginRequiredMixin, View):
    def post(self, request, pk):
        meal = get_object_or_404(PlannedMeal.objects.select_related('plan', 'recipe'), pk=pk)
        old_name = meal.recipe.name
        recipe_id = request.POST.get('recipe_id')
        try:
            if recipe_id:
                recipe = get_object_or_404(Recipe, pk=recipe_id, archived=False)
                services.replace_with_recipe(meal, recipe, request.user)
            else:
                services.replace_with_suggestion(meal, request.user, request.POST.get('hint', '').strip())
            messages.success(request, f'Swapped {old_name} for {meal.recipe.name}.')
        except Exception as e:
            logger.exception('replace failed for meal %s', pk)
            messages.error(request, f'Could not replace {old_name}: {e}')
        return _back(request, f'/plan/{meal.plan.week_start.isoformat()}/')


class AddMeal(LoginRequiredMixin, View):
    """Add one meal to this week only, either a favorite or one Claude suggests."""
    def post(self, request, week_start):
        plan = _plan_or_404(week_start)
        meal_type = request.POST.get('meal_type', '')
        try:
            day = date.fromisoformat(request.POST.get('date', ''))
        except ValueError:
            return HttpResponseBadRequest()
        if meal_type not in dict(MEAL_TYPES):
            return HttpResponseBadRequest()
        recipe_id = request.POST.get('recipe_id')
        recipe = get_object_or_404(Recipe, pk=recipe_id, archived=False) if recipe_id else None
        try:
            meal = services.add_meal(plan, day, meal_type, request.user, recipe=recipe,
                                     hint=request.POST.get('hint', '').strip()[:300])
            messages.success(request, f'Added {meal.recipe.name} for {day:%A} {meal.get_meal_type_display().lower()}.')
            return redirect(f'/plan/{week_start}/#meal-{meal.pk}')
        except Exception as e:
            logger.exception('add_meal failed for %s %s', day, meal_type)
            messages.error(request, f'Could not add that meal: {e}')
        return redirect('plan', week_start)


class MoveMeal(LoginRequiredMixin, View):
    def post(self, request, pk):
        meal = get_object_or_404(PlannedMeal.objects.select_related('plan', 'recipe'), pk=pk)
        try:
            day = date.fromisoformat(request.POST.get('date', ''))
        except ValueError:
            return HttpResponseBadRequest()
        name, from_day = meal.recipe.name, meal.date
        try:
            moved = services.move_meal(meal, day)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('plan', meal.plan.week_start.isoformat())
        messages.success(request, f'Moved {name} from {from_day:%A} to {day:%A}.')
        return redirect(f'/plan/{meal.plan.week_start.isoformat()}/#meal-{moved.pk}')


class RemoveMeal(LoginRequiredMixin, View):
    def post(self, request, pk):
        meal = get_object_or_404(PlannedMeal.objects.select_related('plan', 'recipe'), pk=pk)
        week_start = meal.plan.week_start.isoformat()
        services.remove_meal(meal)
        messages.success(request, f'Removed {meal.recipe.name} from {meal.date:%A}. It won\'t be re-planned this week.')
        return redirect('plan', week_start)


class ToggleLock(LoginRequiredMixin, View):
    def post(self, request, pk):
        meal = get_object_or_404(PlannedMeal, pk=pk)
        meal.locked = not meal.locked
        meal.save(update_fields=['locked'])
        return _back(request, f'/plan/{meal.plan.week_start.isoformat()}/')


class RecipeList(LoginRequiredMixin, View):
    def get(self, request):
        recipes = (Recipe.objects.annotate(total=Sum('ratings__value'), times=Count('planned_meals', distinct=True))
                   .order_by('archived', '-total', 'name'))
        return render(request, 'meals/recipes.html', {'recipes': recipes})


class RecipeDetail(LoginRequiredMixin, View):
    def get(self, request, pk):
        recipe = get_object_or_404(Recipe, pk=pk)
        return render(request, 'meals/recipe_detail.html', {
            'recipe': recipe,
            'my_rating': recipe.ratings.filter(user=request.user).first(),
            'votes': recipe.ratings.select_related('user'),
            'history': recipe.planned_meals.order_by('-date')[:10],
        })


class ToggleArchive(LoginRequiredMixin, View):
    def post(self, request, pk):
        recipe = get_object_or_404(Recipe, pk=pk)
        recipe.archived = not recipe.archived
        recipe.save(update_fields=['archived'])
        messages.success(request, f"{recipe.name} {'will no longer be suggested' if recipe.archived else 'can be suggested again'}.")
        return redirect('recipe_detail', pk)


class Groceries(LoginRequiredMixin, View):
    def get(self, request, week_start):
        plan = _plan_or_404(week_start)
        return render(request, 'meals/groceries.html', {
            'plan': plan,
            'items': services.grocery_list(plan),
            'kroger_connected': kroger.is_connected(),
            'store_set': bool(HouseholdSettings.load().kroger_location_id),
            'last_push': plan.cart_pushes.order_by('-created_at').first(),
        })

    def post(self, request, week_start):
        plan = _plan_or_404(week_start)
        items = request.POST.getlist('item')
        if not items:
            messages.error(request, 'Nothing selected.')
            return redirect('groceries', week_start)
        try:
            push = services.push_to_cart(plan, items, request.user)
            added = sum(1 for r in push.results if r['status'] == 'added')
            messages.success(request, f'Added {added} of {len(items)} items to your King Soopers cart.')
        except kroger.KrogerNotConnected:
            messages.error(request, 'Connect your King Soopers account first.')
        except Exception as e:
            logger.exception('push_to_cart failed')
            messages.error(request, f'Could not fill the cart: {e}')
        return redirect('groceries', week_start)


class CurrentGroceries(LoginRequiredMixin, View):
    """The tab bar's Groceries link: this week's list, or the week page if nothing is planned yet."""
    def get(self, request):
        start = week_start_for(timezone.localdate())
        if not MealPlan.objects.filter(week_start=start, meals__isnull=False).exists():
            messages.error(request, 'Plan this week first; the grocery list comes from its meals.')
            return redirect('plan', start.isoformat())
        return redirect('groceries', start.isoformat())


class PreferencesView(LoginRequiredMixin, View):
    """What the household likes (shared) and what each member loves or won't eat (their own)."""
    def get(self, request):
        household = HouseholdSettings.load()
        mine = MemberPreferences.objects.filter(user=request.user).first()
        proteins = household.protein_preferences or {}
        schedule = household.schedule
        return render(request, 'meals/preferences.html', {
            'household': household,
            'schedule': [(key, label, schedule[key]) for key, label in MEAL_TYPES],
            'plan_weekdays': PLAN_WEEKDAYS,
            'schedule_presets': [label for label, _ in MEAL_SCHEDULE_PRESETS],
            'schedule_presets_data': [days for _, days in MEAL_SCHEDULE_PRESETS],
            'cook_times': COOK_TIMES,
            'proteins': [(key, label, proteins.get(key, 'some')) for key, label in PROTEINS],
            'protein_levels': PROTEIN_LEVELS,
            'cuisines': CUISINES,
            'styles': MEAL_STYLES,
            'mine': mine,
            'others': MemberPreferences.objects.exclude(user=request.user).select_related('user'),
        })

    def post(self, request):
        household = HouseholdSettings.load()
        schedule = {
            key: sorted({int(d) for d in request.POST.getlist(f'days_{key}') if d in '0123456' and len(d) == 1})
            for key, _ in MEAL_TYPES
        }
        if any(schedule.values()):
            household.meal_schedule = schedule
        else:
            messages.error(request, 'Pick at least one meal a week; kept your previous meal schedule.')
        try:
            minutes = int(request.POST.get('max_minutes', ''))
        except ValueError:
            minutes = household.max_weeknight_minutes
        if minutes in {m for m, *_ in COOK_TIMES}:
            household.max_weeknight_minutes = minutes
        levels = {key for key, _ in PROTEIN_LEVELS}
        household.protein_preferences = {
            key: request.POST.get(f'protein_{key}') for key, _ in PROTEINS
            if request.POST.get(f'protein_{key}') in levels - {'some'}
        }
        household.favorite_cuisines = [c for c in CUISINES if c in request.POST.getlist('cuisine')]
        household.meal_styles = [m for m in MEAL_STYLES if m in request.POST.getlist('style')]
        household.dietary_notes = request.POST.get('notes', '').strip()[:2000]
        household.preferences_updated_at = timezone.now()
        household.save()
        MemberPreferences.objects.update_or_create(user=request.user, defaults={
            'loves': request.POST.get('loves', '').strip()[:1000],
            'dislikes': request.POST.get('dislikes', '').strip()[:1000],
        })
        messages.success(request, 'Preferences saved. The next plan and any swaps will use them.')
        return redirect('preferences')


class SettingsView(LoginRequiredMixin, View):
    """King Soopers connection and store, reachable before any plan exists."""
    def get(self, request):
        household = HouseholdSettings.load()
        zip_code = request.GET.get('zip', '').strip()
        stores, store_error = [], ''
        if zip_code:
            if not (zip_code.isdigit() and len(zip_code) == 5):
                store_error = 'Enter a 5-digit ZIP code.'
            else:
                try:
                    stores = [_store_summary(loc) for loc in kroger.find_locations(zip_code)]
                except Exception as e:
                    logger.exception('Kroger location search failed')
                    store_error = f'Could not search stores: {e}'
                if not stores and not store_error:
                    store_error = f'No stores found near {zip_code}.'
        return render(request, 'meals/settings.html', {
            'household': household,
            'kroger_auth': KrogerAuth.objects.filter(pk=1).first(),
            'calendar_bot': calendar_sync.service_account_email(),
            'current_plan': MealPlan.objects.filter(week_start=week_start_for(timezone.localdate()),
                                                    meals__isnull=False).first(),
            'zip': zip_code,
            'stores': stores,
            'store_error': store_error,
        })


def _store_summary(loc: dict) -> dict:
    address = loc.get('address', {})
    return {
        'id': loc['locationId'],
        'name': loc.get('name', ''),
        'chain': loc.get('chain', ''),
        'address': ', '.join(p for p in (address.get('addressLine1'), address.get('city')) if p),
    }


class ChooseStore(LoginRequiredMixin, View):
    def post(self, request):
        location_id = request.POST.get('location_id', '').strip()
        if not (location_id.isalnum() and len(location_id) <= 20):
            return HttpResponseBadRequest()
        household = HouseholdSettings.load()
        household.kroger_location_id = location_id
        household.kroger_location_name = request.POST.get('name', '').strip()[:200]
        household.save(update_fields=['kroger_location_id', 'kroger_location_name'])
        messages.success(request, f"Store set to {household.kroger_location_name or location_id}.")
        return redirect('/settings/#store')


class SetCalendar(LoginRequiredMixin, View):
    def post(self, request):
        household = HouseholdSettings.load()
        calendar_id = '' if request.POST.get('disconnect') else request.POST.get('calendar_id', '').strip()
        if calendar_id:
            if len(calendar_id) > 255:
                return HttpResponseBadRequest()
            problem = calendar_sync.check_access(calendar_id)
            if problem:
                messages.error(request, problem)
                return redirect('/settings/#calendar')
        household.calendar_id = calendar_id
        household.save(update_fields=['calendar_id'])
        if not calendar_id:
            messages.success(request, 'Calendar disconnected. Meals will no longer be added.')
            return redirect('/settings/#calendar')
        plan = MealPlan.objects.filter(week_start=week_start_for(timezone.localdate())).first()
        synced, failed = services.sync_plan_calendar(plan) if plan else (0, 0)
        messages.success(request, 'Calendar connected.' + (f' Added {synced} meals from this week.' if synced else ''))
        if failed:
            messages.error(request, f'{failed} meals could not be added; see the server log.')
        return redirect('/settings/#calendar')


class SyncCalendar(LoginRequiredMixin, View):
    def post(self, request):
        plan = MealPlan.objects.filter(week_start=week_start_for(timezone.localdate())).first()
        if not plan or not HouseholdSettings.load().calendar_id:
            return redirect('/settings/#calendar')
        synced, failed = services.sync_plan_calendar(plan)
        if failed:
            messages.error(request, f'Synced {synced} meals; {failed} failed. See the server log.')
        else:
            messages.success(request, f"Synced {synced} meals to the calendar.")
        return redirect('/settings/#calendar')


class KrogerConnect(LoginRequiredMixin, View):
    def get(self, request):
        state = secrets.token_urlsafe(24)
        request.session['kroger_state'] = state
        request.session['kroger_next'] = request.GET.get('next', '/settings/')
        return redirect(kroger.authorize_url(state))


class KrogerCallback(LoginRequiredMixin, View):
    def get(self, request):
        expected = request.session.pop('kroger_state', None)
        if not expected or not hmac.compare_digest(expected, request.GET.get('state', '')):
            return HttpResponseForbidden('State mismatch; try connecting again.')
        if 'code' not in request.GET:
            messages.error(request, f"King Soopers connection cancelled: {request.GET.get('error', 'unknown')}")
        else:
            kroger.exchange_code(request.GET['code'])
            messages.success(request, 'King Soopers account connected.')
        next_url = request.session.pop('kroger_next', '/')
        return redirect(next_url if next_url.startswith('/') and not next_url.startswith('//') else '/')


@method_decorator(csrf_exempt, name='dispatch')
class InternalGenerateWeek(View):
    """Called by the Airflow DAG every Sunday: build this week's plan and email it.

    Auth is the shared `X-Meal-Planner-Secret` header. Safe to re-run: existing meals are kept
    and the email only goes out once per plan unless `force_email` is set.
    """
    def post(self, request):
        secret = request.headers.get('X-Meal-Planner-Secret', '')
        if not settings.INTERNAL_API_SECRET or not hmac.compare_digest(secret, settings.INTERNAL_API_SECRET):
            return HttpResponseForbidden()
        body = json.loads(request.body or b'{}')
        start = date.fromisoformat(body['week_start']) if body.get('week_start') else None
        plan = services.generate_week(start)
        sent_to = []
        if plan.emailed_at is None or body.get('force_email'):
            sent_to = services.send_plan_email(plan)
        return JsonResponse({
            'week_start': plan.week_start.isoformat(),
            'meals': [{'date': m.date.isoformat(), 'meal_type': m.meal_type, 'recipe': m.recipe.name}
                      for m in plan.meals.select_related('recipe')],
            'emailed': sent_to,
        })
