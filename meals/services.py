"""Plan lifecycle: generate a week, swap meals, build the grocery list, send the email, fill the cart.

Views, the management command and the Airflow-facing endpoint all go through here.
"""
import logging
import re
from collections import OrderedDict
from datetime import date, timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from meals import calendar_sync, kroger, planner
from meals.models import (
    AllowedUser, CartPush, GroceryProduct, HouseholdSettings, MealPlan, MealSwap, PlannedMeal, Recipe,
    week_start_for,
)

logger = logging.getLogger(__name__)


def plan_dates(week_start: date) -> list[date]:
    weekdays = HouseholdSettings.load().weekdays
    return [d for d in (week_start + timedelta(days=i) for i in range(7)) if d.weekday() in weekdays]


def _sync_calendar(meals) -> int:
    """Calendar trouble shouldn't lose a plan; log it and carry on. Returns how many failed."""
    failed = 0
    for meal in meals:
        try:
            calendar_sync.sync_meal(meal)
        except Exception:
            failed += 1
            logger.exception('Calendar sync failed for meal %s', meal.pk)
    return failed


def sync_plan_calendar(plan: MealPlan) -> tuple[int, int]:
    """Push every meal in `plan` to the calendar (e.g. after it's first set). Returns (synced, failed)."""
    meals = list(plan.meals.select_related('recipe'))
    failed = _sync_calendar(meals)
    return len(meals) - failed, failed


def generate_week(week_start: date | None = None, regenerate: bool = False) -> MealPlan:
    """Create the plan for the week starting `week_start` (a Sunday; default this week).

    With `regenerate`, every unlocked meal is re-picked; otherwise only empty days are filled,
    so calling this twice is harmless.
    """
    week_start = week_start_for(week_start or timezone.localdate())
    plan, _ = MealPlan.objects.get_or_create(week_start=week_start)
    dates = plan_dates(week_start)

    if regenerate:
        for meal in plan.meals.filter(locked=False):
            _remove_meal(meal)
    existing = {m.date: m for m in plan.meals.select_related('recipe')}
    locked = {d: m.recipe for d, m in existing.items()}
    if all(d in existing for d in dates):
        return plan

    choices = planner.choose_week(dates, locked)
    with transaction.atomic():
        new_meals = [
            PlannedMeal.objects.create(
                plan=plan, date=date.fromisoformat(c.date), recipe=planner.resolve_recipe(c), why=c.why[:300])
            for c in choices
        ]
    _sync_calendar(new_meals)
    return plan


def _remove_meal(meal: PlannedMeal):
    try:
        calendar_sync.delete_meal_event(meal)
    except Exception:
        logger.exception('Calendar delete failed for meal %s', meal.pk)
    meal.delete()


def _swap(meal: PlannedMeal, recipe: Recipe, why: str, user, reason: str) -> PlannedMeal:
    MealSwap.objects.create(recipe=meal.recipe, user=user, reason=reason[:300])
    meal.recipe = recipe
    meal.why = why[:300]
    meal.save(update_fields=['recipe', 'why'])
    _sync_calendar([meal])
    return meal


def replace_with_suggestion(meal: PlannedMeal, user, hint: str = '') -> PlannedMeal:
    """Ask Claude for something else for this night, guided by the user's hint."""
    choice = planner.choose_replacement(meal, hint)
    recipe = planner.resolve_recipe(choice)
    return _swap(meal, recipe, choice.why, user, hint)


def replace_with_recipe(meal: PlannedMeal, recipe: Recipe, user) -> PlannedMeal:
    return _swap(meal, recipe, f'Picked by {user.first_name or user.username}', user, 'picked a different recipe')


def _normalize(item: str) -> str:
    return re.sub(r'\s+', ' ', item.strip().lower())


def grocery_list(plan: MealPlan) -> list[dict]:
    """Ingredients for the week merged by grocery item, pantry staples marked.

    Returns [{"grocery_item", "uses": ["2 lb (Tacos)", ...], "pantry_staple"}], staples last.
    """
    staples = HouseholdSettings.load().pantry_staple_set
    merged: OrderedDict[str, dict] = OrderedDict()
    for meal in plan.meals.select_related('recipe'):
        for ing in meal.recipe.ingredients:
            key = _normalize(ing.get('grocery_item') or ing['name'])
            entry = merged.setdefault(key, {'grocery_item': key, 'uses': [], 'pantry_staple': False})
            entry['uses'].append(f"{ing.get('quantity', '')} {ing['name']} ({meal.recipe.name})".strip())
            entry['pantry_staple'] = entry['pantry_staple'] or bool(ing.get('pantry_staple')) or key in staples
    return sorted(merged.values(), key=lambda e: (e['pantry_staple'], e['grocery_item']))


def push_to_cart(plan: MealPlan, grocery_items: list[str], user) -> CartPush:
    """Match each grocery item to a King Soopers product (remembered per item) and add them to the cart."""
    location_id = HouseholdSettings.load().kroger_location_id
    if not location_id:
        raise ValueError('Choose your King Soopers store on the Settings page first.')

    results, to_add = [], []
    for item in grocery_items:
        product = GroceryProduct.objects.filter(grocery_item=item).first()
        if not product:
            matches = kroger.search_products(item, location_id, limit=1)
            if not matches:
                results.append({'grocery_item': item, 'upc': '', 'description': '', 'status': 'no match'})
                continue
            product = GroceryProduct.objects.create(
                grocery_item=item, upc=matches[0]['upc'], description=matches[0].get('description', '')[:300])
        to_add.append({'upc': product.upc, 'quantity': 1})
        results.append({'grocery_item': item, 'upc': product.upc, 'description': product.description,
                        'status': 'added'})

    if to_add:
        kroger.add_to_cart(to_add)
    return CartPush.objects.create(plan=plan, user=user, results=results)


def plan_email_context(plan: MealPlan) -> dict:
    base = settings.APP_BASE_URL
    meals = list(plan.meals.select_related('recipe'))
    minutes = [m.recipe.total_minutes for m in meals if m.recipe.total_minutes]
    previous = MealPlan.objects.filter(week_start=plan.week_start - timedelta(days=7)).first()
    return {
        'plan': plan,
        'meals': [{'meal': m, 'url': base + reverse('meal_detail', args=[m.pk])} for m in meals],
        'avg_minutes': round(sum(minutes) / len(minutes)) if minutes else None,
        'favorites': sum(1 for m in meals if m.recipe.score > 0),
        'last_week': [
            {'meal': m, 'url': base + reverse('meal_detail', args=[m.pk])}
            for m in (previous.meals.select_related('recipe') if previous else [])
        ],
        'plan_url': base + reverse('plan', args=[plan.week_start.isoformat()]),
        'groceries_url': base + reverse('groceries', args=[plan.week_start.isoformat()]),
        'preferences_url': base + reverse('preferences'),
        'base_url': base,
    }


def send_plan_email(plan: MealPlan) -> list[str]:
    recipients = list(AllowedUser.objects.filter(receives_plan_email=True).values_list('email', flat=True))
    if not recipients:
        return []
    context = plan_email_context(plan)
    message = EmailMultiAlternatives(
        subject=f'Dinners for the week of {plan.week_start:%b %-d}',
        body=render_to_string('email/plan_summary.txt', context),
        to=recipients,
    )
    message.attach_alternative(render_to_string('email/plan_summary.html', context), 'text/html')
    message.send()
    plan.emailed_at = timezone.now()
    plan.save(update_fields=['emailed_at'])
    return recipients
