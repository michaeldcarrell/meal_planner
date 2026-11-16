import json
from datetime import date, timedelta
from fractions import Fraction
from unittest import mock

import requests
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from meals import calendar_sync, photos, planner, quantities, services
from meals.models import (
    AllowedUser, GroceryProduct, Household, HouseholdSettings, KrogerAuth, MealPlan, MemberPreferences, MealSwap,
    Membership, PlannedMeal, Rating, Recipe, RecipePhoto, week_start_for,
)

SUNDAY = date(2026, 10, 25)


def new_recipe(name, items=('yellow onion',)):
    return planner.NewRecipe(
        name=name, summary=f'{name} summary', cuisine='Mexican', total_minutes=30, servings=4,
        ingredients=[planner.Ingredient(name=i, quantity='1', grocery_item=i, pantry_staple=False) for i in items]
        + [planner.Ingredient(name='salt', quantity='1 tsp', grocery_item='salt', pantry_staple=True)],
        instructions='1. Cook.', photo_query=name.lower(),
    )


def make_household(name='Home', *emails, quiet=()):
    """A household with these members (AllowedUsers, created as needed); `quiet` ones get no weekly email."""
    household = Household.objects.create(name=name)
    for email in (*emails, *quiet):
        member, _ = AllowedUser.objects.get_or_create(email=email)
        Membership.objects.create(household=household, member=member, receives_plan_email=email not in quiet)
    return household


def fake_choose_week(home, slots, locked, hint=''):
    return [
        planner.MealChoice(date=d.isoformat(), meal_type=t, existing_recipe_id=None,
                           new_recipe=new_recipe(f'Dish {d:%a}' if t == 'dinner' else f'{t.title()} {d:%a}'), why='variety')
        for d, t in slots if (d, t) not in locked
    ]


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', INTERNAL_API_SECRET='s3cret')
@mock.patch('meals.calendar_sync.sync_meal')
@mock.patch('meals.calendar_sync.delete_meal_event')
class PlanServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username='mike', email='mike@example.com')
        self.home = make_household('Home', 'mike@example.com', 'partner@example.com', quiet=['quiet@example.com'])

    def test_week_start_is_sunday(self, *_):
        self.assertEqual(week_start_for(date(2026, 10, 29)), SUNDAY)
        self.assertEqual(week_start_for(SUNDAY), SUNDAY)

    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_generate_week_fills_weeknights_and_is_idempotent(self, choose, delete_event, sync):
        plan = services.generate_week(self.home, SUNDAY)
        self.assertEqual([m.date.weekday() for m in plan.meals.all()], [0, 1, 2, 3, 4])
        self.assertEqual(sync.call_count, 5)
        services.generate_week(self.home, SUNDAY)
        self.assertEqual(choose.call_count, 1)
        self.assertEqual(plan.meals.count(), 5)

    def test_week_page_shows_a_plan_still_being_generated(self, *_):
        """Leaving the week mid-plan and coming back shows it's still planning, and a second click
        doesn't start another run."""
        seen = {}

        def choose(home, slots, locked, hint=''):
            self.client.force_login(self.user)
            page = self.client.get(f'/plan/{SUNDAY}/')
            seen['page'] = page.content.decode()
            seen['status'] = self.client.get(f'/plan/{SUNDAY}/status/').json()
            with mock.patch('meals.planner.choose_week') as second:
                self.client.post(f'/plan/{SUNDAY}/generate/')
                seen['second_run'] = second.called
            return fake_choose_week(home, slots, locked)

        with mock.patch('meals.planner.choose_week', side_effect=choose):
            plan = services.generate_week(self.home, SUNDAY)
        self.assertIn('Planning this week', seen['page'])
        self.assertNotIn('Plan this week</button>', seen['page'])
        self.assertEqual(seen['status'], {'generating': True})
        self.assertFalse(seen['second_run'])
        self.assertEqual(plan.meals.count(), 5)
        self.assertEqual(self.client.get(f'/plan/{SUNDAY}/status/').json(), {'generating': False})
        self.assertNotIn('Planning this week', self.client.get(f'/plan/{SUNDAY}/').content.decode())

    @mock.patch('meals.planner.choose_week', side_effect=RuntimeError('boom'))
    def test_failed_generation_clears_the_flag_and_stale_flags_expire(self, *_):
        with self.assertRaises(RuntimeError):
            services.generate_week(self.home, SUNDAY)
        plan = MealPlan.objects.get(week_start=SUNDAY)
        self.assertIsNone(plan.generating_since)
        # A run killed outright (worker timeout) never clears it; it stops counting after a while.
        plan.generating_since = timezone.now() - MealPlan.GENERATING_STALE_AFTER
        plan.save()
        self.assertFalse(plan.is_generating)
        with self.assertRaises(RuntimeError):  # not PlanBusy: the stale claim is taken over
            services.generate_week(self.home, SUNDAY)

    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_regenerate_keeps_locked_meals(self, choose, delete_event, sync):
        plan = services.generate_week(self.home, SUNDAY)
        keep = plan.meals.first()
        keep.locked = True
        keep.save()
        services.generate_week(self.home, SUNDAY, regenerate=True)
        self.assertEqual(plan.meals.count(), 5)
        self.assertTrue(plan.meals.filter(pk=keep.pk).exists())
        self.assertEqual(delete_event.call_count, 4)
        self.assertEqual(choose.call_args.args[2], {(keep.date, 'dinner'): keep.recipe})

    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_generate_week_follows_the_meal_schedule(self, choose, delete_event, sync):
        household = HouseholdSettings.load(self.home)
        household.meal_schedule = {'breakfast': [5, 6], 'lunch': [0], 'dinner': [0, 1]}
        household.save()
        plan = services.generate_week(self.home, SUNDAY)
        self.assertEqual([(m.date.day, m.meal_type) for m in plan.meals.all()], [
            (25, 'breakfast'), (26, 'lunch'), (26, 'dinner'), (27, 'dinner'), (31, 'breakfast')])
        self.assertEqual(services.generate_week(self.home, SUNDAY), plan)
        self.assertEqual(choose.call_count, 1)

        # Adding a lunch later fills just that one.
        household.meal_schedule['lunch'] = [0, 1]
        household.save()
        services.generate_week(self.home, SUNDAY)
        self.assertEqual(choose.call_args.args[1][3], (date(2026, 10, 27), 'lunch'))
        self.assertEqual(plan.meals.filter(meal_type='lunch').count(), 2)

    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_removed_meal_stays_removed_for_that_week(self, choose, delete_event, sync):
        plan = services.generate_week(self.home, SUNDAY)
        tuesday = plan.meals.get(date=date(2026, 10, 27))
        services.remove_meal(tuesday)
        delete_event.assert_called_once()
        self.assertEqual(plan.meals.count(), 4)
        services.generate_week(self.home, SUNDAY)
        services.generate_week(self.home, SUNDAY, regenerate=True)
        self.assertFalse(plan.meals.filter(date=date(2026, 10, 27)).exists())
        self.assertNotIn((date(2026, 10, 27), 'dinner'), choose.call_args.args[1])
        # Next week still follows the schedule.
        self.assertEqual(services.generate_week(self.home, date(2026, 11, 1)).meals.count(), 5)

    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_added_meal_is_planned_and_kept_on_regenerate(self, choose, delete_event, sync):
        plan = services.generate_week(self.home, SUNDAY)
        saturday = date(2026, 10, 31)
        meal = services.add_meal(plan, saturday, 'lunch', self.user, hint='something light')
        self.assertEqual((meal.recipe.name, meal.meal_type), ('Lunch Sat', 'lunch'))
        self.assertEqual(choose.call_args.args[1], [(saturday, 'lunch')])
        self.assertEqual(choose.call_args.kwargs['hint'], 'something light')
        self.assertEqual(len(choose.call_args.args[2]), 5)  # the rest of the week, so it isn't duplicated
        sync.assert_called_with(meal)
        services.generate_week(self.home, SUNDAY, regenerate=True)
        self.assertTrue(plan.meals.filter(date=saturday, meal_type='lunch').exists())
        with self.assertRaises(ValueError):
            services.add_meal(plan, saturday, 'lunch', self.user)
        with self.assertRaises(ValueError):
            services.add_meal(plan, date(2026, 11, 1), 'dinner', self.user)

    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_add_and_remove_from_the_week_page(self, choose, delete_event, sync):
        plan = services.generate_week(self.home, SUNDAY)
        favorite = Recipe.objects.create(household=self.home, name='Pancakes')
        Rating.objects.create(recipe=favorite, user=self.user, value=1)
        self.client.force_login(self.user)
        page = self.client.get(f'/plan/{SUNDAY}/')
        self.assertContains(page, 'Add a meal to this week')
        self.assertContains(page, f'aria-label="Remove {plan.meals.first().recipe.name} from this week"')
        self.assertContains(self.client.get(f'/meals/{plan.meals.first().pk}/'), f'/meals/{plan.meals.first().pk}/remove/')
        response = self.client.post(f'/plan/{SUNDAY}/add/', {
            'date': '2026-10-25', 'meal_type': 'breakfast', 'recipe_id': favorite.pk})
        meal = plan.meals.get(meal_type='breakfast')
        self.assertRedirects(response, f'/plan/{SUNDAY}/#meal-{meal.pk}', fetch_redirect_response=False)
        self.assertEqual(meal.recipe, favorite)
        self.assertEqual(choose.call_count, 1)  # a favorite needs no Claude call
        self.assertEqual(self.client.post(f'/plan/{SUNDAY}/add/', {'date': 'x', 'meal_type': 'lunch'}).status_code, 400)
        self.assertEqual(self.client.post(f'/plan/{SUNDAY}/add/', {'date': '2026-10-25', 'meal_type': 'brunch'}).status_code, 400)

        response = self.client.post(f'/meals/{meal.pk}/remove/')
        self.assertRedirects(response, f'/plan/{SUNDAY}/', fetch_redirect_response=False)
        self.assertFalse(plan.meals.filter(meal_type='breakfast').exists())
        plan.refresh_from_db()
        self.assertEqual(plan.slot_overrides, {'2026-10-25:breakfast': False})

    @mock.patch('meals.calendar_sync.move_meal_event')
    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_move_meal_to_a_free_day_and_trade_with_a_taken_one(self, choose, move_event, delete_event, sync):
        plan = services.generate_week(self.home, SUNDAY)
        monday = plan.meals.get(date=date(2026, 10, 26))
        monday.locked = True
        monday.save()

        # To a free day: the meal moves, and the week remembers so neither day is re-planned wrongly.
        moved = services.move_meal(monday, date(2026, 10, 31))
        self.assertEqual((moved.pk, moved.date, moved.recipe.name), (monday.pk, date(2026, 10, 31), 'Dish Mon'))
        move_event.assert_called_once_with(moved, 5)
        services.generate_week(self.home, SUNDAY, regenerate=True)
        self.assertFalse(plan.meals.filter(date=date(2026, 10, 26)).exists())
        self.assertTrue(plan.meals.filter(date=date(2026, 10, 31), locked=True).exists())

        # To a taken day: the two trade places, lock and calendar event included.
        move_event.reset_mock()
        saturday = plan.meals.get(date=date(2026, 10, 31))
        saturday.calendar_event_id = 'evt-sat'
        saturday.save()
        tuesday = plan.meals.get(date=date(2026, 10, 27))
        tuesday.calendar_event_id = 'evt-tue'
        tuesday.save()
        tuesday_recipe = tuesday.recipe
        landed = services.move_meal(saturday, date(2026, 10, 27))
        self.assertEqual(landed.pk, tuesday.pk)
        self.assertEqual((landed.recipe.name, landed.locked, landed.calendar_event_id), ('Dish Mon', True, 'evt-sat'))
        saturday.refresh_from_db()
        self.assertEqual((saturday.recipe, saturday.locked, saturday.calendar_event_id), (tuesday_recipe, False, 'evt-tue'))
        self.assertEqual(sorted((m.pk, d) for m, d in (c.args for c in move_event.call_args_list)),
                         sorted([(tuesday.pk, -4), (saturday.pk, 4)]))
        with self.assertRaises(ValueError):
            services.move_meal(saturday, date(2026, 11, 2))

    @mock.patch('meals.calendar_sync.move_meal_event')
    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_move_from_the_week_page(self, choose, move_event, delete_event, sync):
        plan = services.generate_week(self.home, SUNDAY)
        meal = plan.meals.get(date=date(2026, 10, 26))
        self.client.force_login(self.user)
        page = self.client.get(f'/plan/{SUNDAY}/')
        self.assertContains(page, f'aria-label="Move {meal.recipe.name} to another day"')
        self.assertContains(page, 'Trade with Dish Tue')
        self.assertContains(self.client.get(f'/meals/{meal.pk}/'), f'/meals/{meal.pk}/move/')
        response = self.client.post(f'/meals/{meal.pk}/move/', {'date': '2026-10-25'})
        self.assertRedirects(response, f'/plan/{SUNDAY}/#meal-{meal.pk}', fetch_redirect_response=False)
        meal.refresh_from_db()
        self.assertEqual(meal.date, SUNDAY)
        self.assertEqual(self.client.post(f'/meals/{meal.pk}/move/', {'date': 'soon'}).status_code, 400)

    @mock.patch('meals.calendar_sync.move_meal_event')
    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_move_a_leftover_meal_into_another_week(self, choose, move_event, delete_event, sync):
        """Last week's uncooked Friday dinner replaces next Monday's."""
        this_week = week_start_for(timezone.localdate())
        last = services.generate_week(self.home, this_week - timedelta(days=7))
        upcoming = services.generate_week(self.home, this_week + timedelta(days=7))
        friday, monday = last.week_start + timedelta(days=5), upcoming.week_start + timedelta(days=1)
        leftover = last.meals.get(date=friday)
        leftover.locked = True
        leftover.save()
        replaced = upcoming.meals.get(date=monday)

        self.client.force_login(self.user)
        page = self.client.get(f'/meals/{leftover.pk}/').content.decode()
        self.assertIn('Replace Dish Mon', page)
        self.assertIn(f'data-confirm="Replace Dish Mon on Monday, {monday:%b} {monday.day} with Dish Fri?', page)
        self.assertLess(page.index('>This week<'), page.index('>Last week<'))  # leftovers open on this week
        response = self.client.post(f'/meals/{leftover.pk}/move/', {'date': monday.isoformat()})
        self.assertRedirects(response, f'/plan/{upcoming.week_start}/#meal-{leftover.pk}', fetch_redirect_response=False)

        leftover.refresh_from_db()
        self.assertEqual((leftover.plan, leftover.date, leftover.locked), (upcoming, monday, True))
        self.assertFalse(PlannedMeal.objects.filter(pk=replaced.pk).exists())
        self.assertEqual([(c.args[0].recipe.name, c.args[0].date) for c in delete_event.call_args_list],
                         [(replaced.recipe.name, monday)])
        move_event.assert_called_once_with(leftover, (monday - friday).days)
        self.assertFalse(MealSwap.objects.exists())  # being bumped isn't a dislike
        # Neither week plans around the move: Friday stays empty, Monday keeps the leftover.
        services.generate_week(self.home, last.week_start, regenerate=True)
        services.generate_week(self.home, upcoming.week_start, regenerate=True)
        self.assertFalse(last.meals.filter(date=friday).exists())
        self.assertEqual(upcoming.meals.get(date=monday).recipe.name, 'Dish Fri')

        # Into a day of another week with nothing planned: nothing is replaced. Into the past: refused.
        delete_event.reset_mock()
        services.move_meal(last.meals.get(date=last.week_start + timedelta(days=4)), upcoming.week_start + timedelta(days=6))
        delete_event.assert_not_called()
        self.assertEqual(upcoming.meals.count(), 6)
        with self.assertRaises(ValueError):
            services.move_meal(upcoming.meals.get(date=monday), last.week_start + timedelta(days=1))

    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_servings_scale_the_meal_groceries_and_calendar(self, choose, delete_event, sync):
        plan = services.generate_week(self.home, SUNDAY)
        meal = plan.meals.get(date=date(2026, 10, 26))  # 'Dish Mon': 1 yellow onion, 1 tsp salt; serves 4
        self.client.force_login(self.user)
        page = self.client.get(f'/meals/{meal.pk}/')
        self.assertContains(page, 'aria-label="More servings"')
        self.assertContains(page, '<output class="servings-count num" aria-live="polite">4</output>', html=False)

        sync.reset_mock()
        response = self.client.post(f'/meals/{meal.pk}/servings/', {'servings': '6', 'next': f'/meals/{meal.pk}/'})
        self.assertRedirects(response, f'/meals/{meal.pk}/', fetch_redirect_response=False)
        meal.refresh_from_db()
        self.assertEqual((meal.servings, meal.serving_count, meal.scale), (6, 6, Fraction(3, 2)))
        sync.assert_called_once_with(meal)
        page = self.client.get(f'/meals/{meal.pk}/')
        self.assertContains(page, '<span class="qty">1 1/2 tsp</span>', html=False)
        self.assertContains(page, 'For 6 · recipe makes 4')
        self.assertContains(page, 'Reset to 4')
        self.assertContains(self.client.get(f'/plan/{SUNDAY}/'), 'Serves 6')
        self.assertContains(self.client.get(f'/recipes/{meal.recipe_id}/'), '<span class="qty">1 tsp</span>', html=False)
        onion = next(i for i in services.grocery_list(plan) if i['grocery_item'] == 'yellow onion')
        self.assertIn(('Dish Mon', '1 1/2'), [(u['meal'].recipe.name, u['quantity']) for u in onion['uses']])
        description = calendar_sync._event_content(meal)['description']
        self.assertIn('• 1 1/2 tsp salt', description)
        self.assertIn('serves 6', description)

        # Trading days takes the servings along; resetting goes back to the recipe's own.
        with mock.patch('meals.calendar_sync.move_meal_event'):
            moved = services.move_meal(meal, date(2026, 10, 27))
        self.assertEqual((moved.recipe.name, moved.servings), ('Dish Mon', 6))
        self.client.post(f'/meals/{moved.pk}/servings/', {'servings': ''})
        moved.refresh_from_db()
        self.assertIsNone(moved.servings)
        self.client.post(f'/meals/{moved.pk}/servings/', {'servings': '4'})  # same as the recipe: stored as blank
        moved.refresh_from_db()
        self.assertIsNone(moved.servings)
        for bad in ('0', '25', 'lots'):
            self.assertEqual(self.client.post(f'/meals/{moved.pk}/servings/', {'servings': bad}).status_code, 400)

    @mock.patch('meals.kroger.add_to_cart')
    @mock.patch('meals.kroger.search_products', side_effect=lambda term, loc, limit: [{'upc': term, 'description': term}])
    def test_cart_fill_marks_items_and_meals_ordered_and_list_filters_by_meal(self, search, add, *_):
        household = HouseholdSettings.load(self.home)
        household.kroger_location_id = '62000123'
        household.save()
        KrogerAuth.objects.create(household=self.home, access_token='a', refresh_token='r',
                                  expires_at=timezone.now() + timedelta(hours=1))
        plan = MealPlan.objects.create(household=self.home, week_start=SUNDAY)

        def plan_meal(day, name, items):
            recipe = planner.resolve_recipe(self.home, planner.MealChoice(
                date='x', meal_type='dinner', existing_recipe_id=None, new_recipe=new_recipe(name, items), why=''))
            return PlannedMeal.objects.create(plan=plan, date=day, recipe=recipe)
        tacos = plan_meal(date(2026, 10, 26), 'Tacos', ['yellow onion', 'black beans'])
        bowls = plan_meal(date(2026, 10, 27), 'Rice bowls', ['yellow onion', 'rice'])
        url = f'/plan/{SUNDAY}/groceries/'
        self.client.force_login(self.user)

        page = self.client.get(url).content.decode()
        self.assertIn('<span class="chip-name">Tacos</span>', page)  # filter chip and badges
        self.assertIn('3 to buy · 4 items', page)
        filtered = self.client.get(url, {'meal': tacos.pk}).content.decode()
        self.assertIn('value="black beans"', filtered)
        self.assertNotIn('value="rice"', filtered)
        self.assertIn('<span class="meal-badge is-selected"', filtered)

        # Fill the cart from the Tacos filter: Tacos is ordered; Rice bowls still needs rice.
        response = self.client.post(f'{url}?meal={tacos.pk}', {'item': ['yellow onion', 'black beans']})
        self.assertRedirects(response, f'{url}?meal={tacos.pk}', fetch_redirect_response=False)
        self.assertIn('Ingredients ordered for Tacos.', [str(m) for m in self.client.get(url).context['messages']][0])
        items = services.grocery_list(plan)
        self.assertEqual({i['grocery_item'] for i in items if i['ordered']}, {'yellow onion', 'black beans'})
        self.assertEqual(services.ordered_meal_ids(items), {tacos.pk})
        page = self.client.get(url).content.decode()
        self.assertIn('value="rice" checked', page)
        self.assertNotIn('value="black beans" checked', page)  # ordered items are left unchecked...
        self.assertLess(page.index('value="rice"'), page.index('value="black beans"'))  # ...and listed after
        self.assertIn('1 to buy · 4 items', page)
        self.assertEqual(self.client.get(f'/plan/{SUNDAY}/').content.decode().count('tag text-like">Ordered'), 1)

        self.client.post(url, {'item': ['rice']})
        self.assertEqual(services.ordered_meal_ids(services.grocery_list(plan)), {tacos.pk, bowls.pk})
        # Swapping in a recipe with new ingredients puts that meal back on the list.
        with mock.patch('meals.planner.choose_replacement', return_value=planner.MealChoice(
                date='2026-10-27', meal_type='dinner', existing_recipe_id=None,
                new_recipe=new_recipe('Tofu stir fry', ['tofu']), why='')):
            services.replace_with_suggestion(bowls, self.user)
        self.assertEqual(services.ordered_meal_ids(services.grocery_list(plan)), {tacos.pk})

    def test_resolve_recipe_reuses_known_recipe(self, *_):
        known = Recipe.objects.create(household=self.home, name='Tacos')
        choice = planner.MealChoice(date='2026-10-26', meal_type='dinner', existing_recipe_id=known.pk, new_recipe=None,
                                    why='loved')
        self.assertEqual(planner.resolve_recipe(self.home, choice), known)
        # Another household's recipe id isn't reused; with no new recipe to fall back on, that's an error.
        other = Recipe.objects.create(household=make_household('Other'), name='Their tacos')
        choice.existing_recipe_id = other.pk
        with self.assertRaises(RuntimeError):
            planner.resolve_recipe(self.home, choice)

    @mock.patch('meals.planner.choose_replacement')
    def test_replace_records_swap(self, choose_replacement, delete_event, sync):
        plan = MealPlan.objects.create(household=self.home, week_start=SUNDAY)
        old = Recipe.objects.create(household=self.home, name='Chili')
        meal = PlannedMeal.objects.create(plan=plan, date=date(2026, 10, 26), recipe=old)
        choose_replacement.return_value = planner.MealChoice(
            date='2026-10-26', meal_type='dinner', existing_recipe_id=None, new_recipe=new_recipe('Stir fry'), why='quicker')
        services.replace_with_suggestion(meal, self.user, 'something quicker')
        meal.refresh_from_db()
        self.assertEqual(meal.recipe.name, 'Stir fry')
        swap = MealSwap.objects.get()
        self.assertEqual((swap.recipe, swap.reason), (old, 'something quicker'))
        sync.assert_called_once()

    def test_grocery_list_merges_items_and_flags_staples(self, *_):
        plan = MealPlan.objects.create(household=self.home, week_start=SUNDAY)
        for i, name in enumerate(['A', 'B']):
            recipe = planner.resolve_recipe(self.home, planner.MealChoice(
                date='x', meal_type='dinner', existing_recipe_id=None, new_recipe=new_recipe(name, ['Yellow Onion', f'item {name}']),
                why=''))
            PlannedMeal.objects.create(plan=plan, date=date(2026, 10, 26 + i), recipe=recipe)
        items = {i['grocery_item']: i for i in services.grocery_list(plan)}
        self.assertEqual(len(items['yellow onion']['uses']), 2)
        self.assertTrue(items['salt']['pantry_staple'])
        self.assertEqual(services.grocery_list(plan)[-1]['grocery_item'], 'salt')

    @mock.patch('meals.kroger.add_to_cart')
    @mock.patch('meals.kroger.search_products')
    def test_push_to_cart_remembers_products(self, search, add, *_):
        household = HouseholdSettings.load(self.home)
        household.kroger_location_id = '62000123'
        household.save()
        plan = MealPlan.objects.create(household=self.home, week_start=SUNDAY)
        GroceryProduct.objects.create(household=self.home, grocery_item='yellow onion', upc='111', description='Onion')
        # Another household's remembered product for the same item isn't used.
        GroceryProduct.objects.create(household=make_household('Other'), grocery_item='black beans', upc='999')
        search.side_effect = lambda term, loc, limit: [] if term == 'unicorn' else [{'upc': '222', 'description': 'Beans'}]
        push = services.push_to_cart(plan, ['yellow onion', 'black beans', 'unicorn'], self.user)
        add.assert_called_once_with(self.home, [{'upc': '111', 'quantity': 1}, {'upc': '222', 'quantity': 1}])
        self.assertEqual([r['status'] for r in push.results], ['added', 'added', 'no match'])
        self.assertTrue(GroceryProduct.objects.filter(household=self.home, grocery_item='black beans', upc='222').exists())

    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_internal_endpoint_generates_and_emails_once(self, *_):
        url = '/internal/generate-week/'
        self.assertEqual(self.client.post(url).status_code, 403)
        body = json.dumps({'week_start': SUNDAY.isoformat()})
        headers = {'X-Meal-Planner-Secret': 's3cret'}
        response = self.client.post(url, body, content_type='application/json', headers=headers)
        self.assertEqual(response.status_code, 200)
        [result] = response.json()['households']
        self.assertEqual((result['household'], len(result['meals'])), ('Home', 5))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(sorted(mail.outbox[0].to), ['mike@example.com', 'partner@example.com'])
        self.assertIn('Dish Mon', mail.outbox[0].body)
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn('Oct 25 – Oct 31', html)
        self.assertIn('/preferences/', html)
        self.assertEqual(html.count('Rate or swap'), 5)
        self.client.post(url, body, content_type='application/json', headers=headers)
        self.assertEqual(len(mail.outbox), 1)

    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_pages_render_and_rating_works(self, *_):
        plan = services.generate_week(self.home, SUNDAY)
        meal = plan.meals.first()
        self.client.force_login(self.user)
        detail = self.client.get(f'/meals/{meal.pk}/')
        self.assertContains(detail, 'data-steps="recipe-')
        self.assertContains(detail, '<span class="visually-hidden">Step 1: </span>Cook.', html=False)
        for url in [f'/plan/{SUNDAY}/', f'/meals/{meal.pk}/', '/recipes/', f'/recipes/{meal.recipe_id}/',
                    f'/plan/{SUNDAY}/groceries/', '/plan/2026-11-01/']:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.client.post(f'/recipes/{meal.recipe_id}/rate/', {'value': '1', 'comment': 'yum'})
        self.assertEqual(Rating.objects.get().value, 1)
        self.client.post(f'/recipes/{meal.recipe_id}/rate/', {'value': '0'})
        self.assertFalse(Rating.objects.exists())

    def test_pages_require_login(self, *_):
        self.assertEqual(self.client.get('/recipes/').status_code, 302)

    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_setup_checklist_until_kroger_store_and_plan(self, *_):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(f'/plan/{SUNDAY}/'), 'Tell the planner what you like')
        self.client.post('/preferences/', {'max_minutes': '20'})
        self.assertContains(self.client.get(f'/plan/{SUNDAY}/'), 'Connect your King Soopers account')
        KrogerAuth.objects.create(household=self.home, access_token='a', refresh_token='r', expires_at=timezone.now())
        household = HouseholdSettings.load(self.home)
        household.kroger_location_id = '62000123'
        household.save()
        self.assertContains(self.client.get(f'/plan/{SUNDAY}/'), 'Connect a Google Calendar')
        household.calendar_id = 'dinners@group.calendar.google.com'
        household.save()
        self.assertContains(self.client.get(f'/plan/{SUNDAY}/'), 'Plan your first week')
        services.generate_week(self.home, SUNDAY)
        self.assertNotContains(self.client.get(f'/plan/{SUNDAY}/'), 'Get set up')

    def test_preferences_set_the_meal_schedule(self, *_):
        self.client.force_login(self.user)
        response = self.client.get('/preferences/')
        self.assertContains(response, 'Meals each week')
        self.assertContains(response, 'Weekend breakfasts')
        self.client.post('/preferences/', {'days_breakfast': ['5', '6', '9', 'x'], 'days_dinner': ['0', '2', '2']})
        household = HouseholdSettings.load(self.home)
        self.assertEqual(household.schedule, {'breakfast': [5, 6], 'lunch': [], 'dinner': [0, 2]})
        self.assertEqual(household.meals_per_week, 4)

        # Unticking everything keeps the old schedule rather than planning nothing.
        self.client.post('/preferences/', {})
        self.assertEqual(HouseholdSettings.load(self.home).meals_per_week, 4)

    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_mixed_week_labels_meals(self, *_):
        household = HouseholdSettings.load(self.home)
        household.meal_schedule = {'breakfast': [6], 'dinner': [0]}
        household.save()
        plan = services.generate_week(self.home, SUNDAY)
        self.client.force_login(self.user)
        page = self.client.get(f'/plan/{SUNDAY}/')
        self.assertContains(page, '<h2>Meals</h2>', html=False)
        self.assertContains(page, 'class="meal-type" aria-hidden="true">Breakfast</span>')
        self.assertEqual(services.plan_email_context(plan)['meals_label'], 'Meals')
        services.send_plan_email(plan)
        self.assertTrue(mail.outbox[0].subject.startswith('Meals for the week of'))
        self.assertIn('SUN 25  BREAKFAST: Breakfast Sun', mail.outbox[0].body)

    def test_preferences_save_and_reach_the_planner(self, *_):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/preferences/').status_code, 200)
        self.client.post('/preferences/', {
            'max_minutes': '20', 'protein_chicken': 'more', 'protein_pork': 'never', 'protein_beef': 'some',
            'protein_fish': 'bogus', 'cuisine': ['Mexican', 'Thai', 'Martian'], 'style': ['Spicy'],
            'notes': 'Tree nut allergy', 'dislikes': 'broccoli,  olives\nmushrooms', 'loves': 'salmon',
        })
        household = HouseholdSettings.load(self.home)
        self.assertEqual(household.max_weeknight_minutes, 20)
        self.assertEqual(household.protein_preferences, {'chicken': 'more', 'pork': 'never'})
        self.assertEqual(household.favorite_cuisines, ['Mexican', 'Thai'])
        self.assertEqual(household.meal_styles, ['Spicy'])
        self.assertIsNotNone(household.preferences_updated_at)
        self.assertEqual(MemberPreferences.objects.get(user=self.user).dislike_list, ['broccoli', 'olives', 'mushrooms'])

        context = json.loads(planner._household_context(self.home))
        self.assertEqual(context['household_notes'], 'Tree nut allergy')
        prefs = context['preferences']
        self.assertEqual(prefs['cooking_time'], 'Quick (10–20 min)')
        self.assertEqual((prefs['proteins_more_often'], prefs['proteins_never']), (['Chicken'], ['Pork']))
        self.assertEqual(prefs['members'], [{'name': 'mike', 'loves': ['salmon'],
                                              'dislikes': ['broccoli', 'olives', 'mushrooms']}])

        # Another member sees what this one won't eat.
        partner = User.objects.create(username='sam', first_name='Sam', email='partner@example.com')
        self.client.force_login(partner)
        self.assertContains(self.client.get('/preferences/'), 'broccoli, olives, mushrooms')

    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_settings_connects_checks_and_syncs_calendar(self, choose, delete_event, sync):
        self.client.force_login(self.user)
        services.generate_week(self.home)  # this week, so connecting syncs it
        sync.reset_mock()
        with mock.patch('meals.calendar_sync.check_access', return_value="The bot can't see that calendar."):
            self.client.post('/settings/calendar/', {'calendar_id': 'wrong@group.calendar.google.com'})
        self.assertEqual(HouseholdSettings.load(self.home).calendar_id, '')
        self.assertFalse(sync.called)

        with mock.patch('meals.calendar_sync.check_access', return_value=None) as check:
            self.client.post('/settings/calendar/', {'calendar_id': ' dinners@group.calendar.google.com '})
        check.assert_called_once_with('dinners@group.calendar.google.com')
        self.assertEqual(HouseholdSettings.load(self.home).calendar_id, 'dinners@group.calendar.google.com')
        meals = MealPlan.objects.get(week_start=week_start_for(timezone.localdate())).meals.count()
        self.assertEqual(sync.call_count, meals)

        sync.reset_mock()
        self.client.post('/settings/calendar/sync/')
        self.assertEqual(sync.call_count, meals)

        self.client.post('/settings/calendar/', {'calendar_id': 'dinners@group.calendar.google.com', 'disconnect': '1'})
        self.assertEqual(HouseholdSettings.load(self.home).calendar_id, '')

    @mock.patch('meals.kroger.find_locations', return_value=[{
        'locationId': '62000123', 'chain': 'KINGSOOPERS', 'name': 'King Soopers - Arapahoe',
        'address': {'addressLine1': '1 Main St', 'city': 'Centennial'},
    }])
    def test_settings_finds_and_chooses_store(self, find_locations, *_):
        self.client.force_login(self.user)
        response = self.client.get('/settings/')
        self.assertContains(response, 'Connect King Soopers')
        self.assertContains(response, 'dinner at 5:30 p.m.')
        self.assertNotContains(response, 'breakfast at')
        response = self.client.get('/settings/', {'zip': '80112'})
        self.assertContains(response, 'King Soopers - Arapahoe')
        find_locations.assert_called_once_with('80112')
        self.assertContains(self.client.get('/settings/', {'zip': 'abc'}), 'Enter a 5-digit ZIP code.')
        self.client.post('/settings/store/', {'location_id': '62000123', 'name': 'King Soopers - Arapahoe'})
        household = HouseholdSettings.load(self.home)
        self.assertEqual((household.kroger_location_id, household.kroger_location_name),
                         ('62000123', 'King Soopers - Arapahoe'))
        self.assertEqual(self.client.post('/settings/store/', {'location_id': '../x'}).status_code, 400)


class ScaleQuantityTests(TestCase):
    def check(self, factor, cases):
        for text, expected in cases:
            self.assertEqual(quantities.scale_quantity(text, factor), expected, text)

    def test_one_and_a_half(self):
        self.check(Fraction(6, 4), [
            ('1 1/2 cups', '2 1/4 cups'), ('1/3 cup', '1/2 cup'), ('1-2 tsp', '1 1/2-3 tsp'), ('12', '18'),
            ('1.5 cups uncooked', '2.25 cups uncooked'), ('1 can (13.5 oz)', '1 1/2 cans (13.5 oz)'),
            ('2 cans (15 oz each)', '3 cans (15 oz each)'), ('4 fillets (1.5 lb)', '6 fillets (2.25 lb)'),
            ('2 large (about 1.5 lb)', '3 large (about 2.25 lb)'), ('1 loaf', '1 1/2 loaves'), ('½ cup', '3/4 cup'),
            ('1 1/4 tsp, divided', '1 7/8 tsp, divided'),
        ])

    def test_half_goes_back_to_singular(self):
        self.check(Fraction(1, 2), [
            ('2 cloves', '1 clove'), ('2 pouches (8.5 oz each)', '1 pouch (8.5 oz each)'), ('1 1/2 cups', '3/4 cup'),
            ('5 cups (about 2 crowns)', '2 1/2 cups (about 1 crown)'), ('1 bag (14 oz)', '1/2 bag (14 oz)'),
        ])

    def test_left_alone(self):
        self.check(Fraction(2), [('to taste', 'to taste'), ('', ''), ('a pinch', 'a pinch')])
        self.check(1, [('1 1/2 cups', '1 1/2 cups')])


class RecipeStepsTests(TestCase):
    def steps(self, text):
        return Recipe(instructions=text).steps

    def test_numbered_lines(self):
        self.assertEqual(self.steps('1. Heat oil.\n\n2) Add onion and cook\n   until soft.\nStep 3: Serve.'),
                         ['Heat oil.', 'Add onion and cook until soft.', 'Serve.'])

    def test_numbered_steps_on_one_line(self):
        self.assertEqual(self.steps('1. Bake at 400 for 12. min 2. Rest 5 min. 3. Slice.'),
                         ['Bake at 400 for 12. min', 'Rest 5 min.', 'Slice.'])

    def test_unnumbered_lines_and_empty(self):
        self.assertEqual(self.steps('Boil water\nCook pasta 2 minutes'), ['Boil water', 'Cook pasta 2 minutes'])
        self.assertEqual(self.steps(''), [])


class CalendarSyncTests(TestCase):
    """The app sets an event's time once, at creation; later syncs never move it."""

    def setUp(self):
        self.home = make_household('Home')
        household = HouseholdSettings.load(self.home)
        household.calendar_id = 'dinners@group.calendar.google.com'
        household.save()
        recipe = Recipe.objects.create(household=self.home, name='Tacos', ingredients=[{'name': 'onion', 'quantity': '1'}])
        plan = MealPlan.objects.create(household=self.home, week_start=SUNDAY)
        self.meal = PlannedMeal.objects.create(plan=plan, date=date(2026, 10, 26), recipe=recipe)
        self.events = mock.Mock()
        self.events.insert.return_value.execute.return_value = {'id': 'evt1'}
        patcher = mock.patch('meals.calendar_sync._service')
        patcher.start().return_value.events.return_value = self.events
        self.addCleanup(patcher.stop)

    def test_time_set_on_create_then_only_content_is_patched(self):
        calendar_sync.sync_meal(self.meal)
        body = self.events.insert.call_args.kwargs['body']
        self.assertEqual(body['start']['dateTime'], '2026-10-26T17:30:00')
        self.assertEqual(body['end']['dateTime'], '2026-10-26T18:30:00')
        self.meal.refresh_from_db()
        self.assertEqual(self.meal.calendar_event_id, 'evt1')

        self.meal.recipe = Recipe.objects.create(household=self.home, name='Pho', ingredients=[])
        self.meal.save()
        calendar_sync.sync_meal(self.meal)
        self.events.update.assert_not_called()
        patch = self.events.patch.call_args.kwargs
        self.assertEqual(patch['eventId'], 'evt1')
        self.assertEqual(patch['body']['summary'], '🍽 Pho')
        self.assertFalse({'start', 'end'} & patch['body'].keys())
        self.assertEqual(self.events.insert.call_count, 1)

    def test_breakfast_uses_its_own_time_and_icon(self):
        self.meal.meal_type = 'breakfast'
        self.meal.save()
        calendar_sync.sync_meal(self.meal)
        body = self.events.insert.call_args.kwargs['body']
        self.assertEqual((body['start']['dateTime'], body['end']['dateTime']),
                         ('2026-10-26T08:00:00', '2026-10-26T08:30:00'))
        self.assertEqual(body['summary'], '🍳 Tacos')

    def test_move_shifts_the_event_keeping_its_time(self):
        self.meal.calendar_event_id = 'evt1'
        self.meal.date = date(2026, 11, 2)
        self.meal.save()
        # Someone moved it to 7pm in Google; it's the Monday before DST ends (UTC-6 then, UTC-7 after).
        self.events.get.return_value.execute.return_value = {
            'start': {'dateTime': '2026-10-26T19:00:00-06:00', 'timeZone': 'America/Denver'},
            'end': {'dateTime': '2026-10-26T20:30:00-06:00', 'timeZone': 'America/Denver'},
        }
        calendar_sync.move_meal_event(self.meal, 7)
        body = self.events.patch.call_args.kwargs['body']
        self.assertEqual(body['start'], {'dateTime': '2026-11-02T19:00:00', 'timeZone': 'America/Denver'})
        self.assertEqual(body['end'], {'dateTime': '2026-11-02T20:30:00', 'timeZone': 'America/Denver'})
        self.assertEqual(body['summary'], '🍽 Tacos')
        self.events.insert.assert_not_called()

    def test_move_recreates_a_missing_event_on_the_new_day(self):
        from googleapiclient.errors import HttpError
        self.meal.calendar_event_id = 'gone'
        self.meal.date = date(2026, 10, 28)
        self.meal.save()
        self.events.get.return_value.execute.side_effect = HttpError(mock.Mock(status=404), b'')
        calendar_sync.move_meal_event(self.meal, 2)
        self.assertEqual(self.events.insert.call_args.kwargs['body']['start']['dateTime'], '2026-10-28T17:30:00')
        self.meal.refresh_from_db()
        self.assertEqual(self.meal.calendar_event_id, 'evt1')

    def test_event_deleted_in_google_is_recreated(self):
        from googleapiclient.errors import HttpError
        self.meal.calendar_event_id = 'gone'
        self.meal.save()
        self.events.patch.return_value.execute.side_effect = HttpError(mock.Mock(status=404), b'')
        calendar_sync.sync_meal(self.meal)
        self.assertEqual(self.events.insert.call_args.kwargs['body']['start']['dateTime'], '2026-10-26T17:30:00')
        self.meal.refresh_from_db()
        self.assertEqual(self.meal.calendar_event_id, 'evt1')


class PlannerRequestTests(TestCase):
    """Runs the real SDK request/parse path against a canned HTTP response (no network)."""

    def test_choose_week_parses_structured_output(self):
        import anthropic
        import httpx2

        payload = {'meals': [{
            'date': '2026-10-26', 'meal_type': 'lunch', 'existing_recipe_id': None, 'why': 'new',
            'new_recipe': new_recipe('Tacos').model_dump(),
        }]}
        seen = {}

        def handler(request):
            seen['body'] = json.loads(request.content)
            return httpx2.Response(200, json={
                'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': 'claude-opus-5-5',
                'content': [{'type': 'text', 'text': json.dumps(payload)}],
                'stop_reason': 'end_turn', 'stop_sequence': None,
                'usage': {'input_tokens': 1, 'output_tokens': 1},
            })

        client = anthropic.Anthropic(api_key='test', http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        with mock.patch('meals.planner._client', return_value=client):
            choices = planner.choose_week(make_household('Home'), [(date(2026, 10, 26), 'lunch')], {})
        self.assertEqual(choices[0].new_recipe.name, 'Tacos')
        self.assertEqual(seen['body']['output_config']['format']['type'], 'json_schema')
        self.assertEqual(seen['body']['fallbacks'], 'default')


def _png(width=3000, height=2000) -> bytes:
    import io
    from PIL import Image
    out = io.BytesIO()
    Image.new('RGB', (width, height), (200, 120, 40)).save(out, 'PNG')
    return out.getvalue()


def _pixabay_get(photos_by_query, image=b'\xff\xd8jpeg-bytes'):
    """A stand-in for requests.get: Pixabay search answers from `photos_by_query`, image downloads return `image`."""
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append(params['q'] if params else url)
        response = mock.Mock(headers={'Content-Type': 'image/jpeg'}, content=image)
        response.raise_for_status.return_value = None
        response.ok = True
        if params:
            hit = photos_by_query.get(params['q'])
            response.json.return_value = {'hits': [hit] if hit else []}
        return response
    get.calls = calls
    return get


PIXABAY_HIT = {'pageURL': 'https://pixabay.com/photos/noodles-123/', 'user': 'Ana Cook',
               'largeImageURL': 'https://pixabay.com/get/123_1280.jpg', 'webformatURL': 'https://pixabay.com/get/123_640.jpg'}


@override_settings(PIXABAY_API_KEY='pixabay-key')
class RecipePhotoTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username='mike', email='mike@example.com', first_name='Mike')
        self.home = make_household('Home', 'mike@example.com')
        self.recipe = Recipe.objects.create(household=self.home, name='Peanut Noodles with Edamame and Crunchy Veggies',
                                            photo_query='peanut noodles')

    def test_search_words_fall_back_to_the_short_name(self):
        self.recipe.photo_query = ''
        self.assertEqual(photos.search_queries(self.recipe),
                         ['Peanut Noodles', 'Peanut Noodles with Edamame and Crunchy Veggies'])

    def test_fetch_stores_the_first_match_with_its_credit(self):
        get = _pixabay_get({'peanut noodles': PIXABAY_HIT})
        with mock.patch('meals.photos.requests.get', side_effect=get):
            self.assertTrue(photos.fetch_photo(self.recipe))
        self.assertEqual(get.calls, ['peanut noodles', 'https://pixabay.com/get/123_1280.jpg'])
        self.recipe.refresh_from_db()
        self.assertEqual((self.recipe.photo_source, self.recipe.photo_credit), ('pixabay', 'Ana Cook'))
        self.assertEqual(bytes(self.recipe.photo.image), b'\xff\xd8jpeg-bytes')
        self.assertTrue(self.recipe.photo_url.startswith(f'/recipes/{self.recipe.pk}/photo/?v='))

    def test_no_match_is_searched_once_and_errors_are_retried(self):
        get = _pixabay_get({})
        with mock.patch('meals.photos.requests.get', side_effect=get):
            self.assertEqual(photos.fetch_missing_photos(), 0)
            self.assertEqual(photos.fetch_missing_photos(), 0)
        self.assertEqual(len(get.calls), 3)  # three queries, once
        self.assertEqual(self.recipe.photo_url, '')

        other = Recipe.objects.create(household=self.home, name='Tacos', photo_query='tacos')
        with mock.patch('meals.photos.requests.get', side_effect=requests.ConnectionError):
            self.assertFalse(photos.fetch_photo(other))
        other.refresh_from_db()
        self.assertIsNone(other.photo_searched_at)  # picked up again next run

    @mock.patch('meals.calendar_sync.sync_meal')
    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_planning_returns_before_photos_are_found(self, *_):
        get = _pixabay_get({f'dish {d}': PIXABAY_HIT for d in ('mon', 'tue', 'wed', 'thu', 'fri')})
        queued = []
        with mock.patch('meals.photos._run_in_background', side_effect=queued.append), \
                mock.patch('meals.photos.requests.get', side_effect=get), \
                self.captureOnCommitCallbacks(execute=True):
            plan = services.generate_week(self.home, SUNDAY)
            self.assertEqual(get.calls, [])  # nothing fetched while planning
        self.assertEqual(len(queued), 1)
        with mock.patch('meals.photos.requests.get', side_effect=get):
            queued[0]()
        for meal in plan.meals.select_related('recipe'):
            self.assertEqual(meal.recipe.photo_source, 'pixabay')

    @override_settings(PIXABAY_API_KEY='')
    def test_without_a_key_nothing_is_queued(self):
        with mock.patch('meals.photos._run_in_background') as run, self.captureOnCommitCallbacks(execute=True):
            photos.fetch_missing_photos_soon()
        run.assert_not_called()

    def test_upload_replaces_the_photo_scaled_down_and_can_be_removed(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image
        import io
        self.client.force_login(self.user)
        page = f'/recipes/{self.recipe.pk}/'
        self.assertContains(self.client.get(page), 'Add your photo')
        response = self.client.post(f'/recipes/{self.recipe.pk}/photo/upload/', {
            'photo': SimpleUploadedFile('dinner.png', _png(), content_type='image/png'), 'next': page})
        self.assertRedirects(response, page, fetch_redirect_response=False)
        self.recipe.refresh_from_db()
        self.assertEqual((self.recipe.photo_source, self.recipe.photo_credit), ('upload', 'Mike'))
        served = self.client.get(self.recipe.photo_url)
        self.assertEqual(served['Content-Type'], 'image/jpeg')
        self.assertEqual(Image.open(io.BytesIO(served.content)).size, (1600, 1067))
        self.assertContains(self.client.get(page), self.recipe.photo_url)

        self.client.post(f'/recipes/{self.recipe.pk}/photo/upload/', {'remove': '1', 'next': page})
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.photo_url, '')
        self.assertFalse(RecipePhoto.objects.exists())
        self.assertEqual(photos.fetch_missing_photos(), 0)  # a removed photo isn't searched for again

    def test_unreadable_upload_is_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.force_login(self.user)
        self.client.post(f'/recipes/{self.recipe.pk}/photo/upload/',
                         {'photo': SimpleUploadedFile('notes.txt', b'not a photo', content_type='text/plain')})
        self.assertFalse(RecipePhoto.objects.exists())

    def test_photo_needs_login(self):
        RecipePhoto.objects.create(recipe=self.recipe, image=b'x')
        self.assertEqual(self.client.get(f'/recipes/{self.recipe.pk}/photo/').status_code, 302)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', INTERNAL_API_SECRET='s3cret')
@mock.patch('meals.calendar_sync.sync_meal')
@mock.patch('meals.calendar_sync.delete_meal_event')
@mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
class HouseholdTests(TestCase):
    """Each household has its own menu; someone in two switches between them."""

    def setUp(self):
        self.user = User.objects.create(username='mike', email='Mike@Example.com', first_name='Mike')
        self.home = make_household('Home', 'mike@example.com', 'partner@example.com')
        self.cabin = make_household('Cabin', 'mike@example.com', 'friend@example.com')
        self.client.force_login(self.user)

    def switch(self, household):
        return self.client.post('/households/switch/', {'household': household.pk})

    def test_each_household_sees_only_its_own_week_and_recipes(self, *_):
        home_plan = services.generate_week(self.home, SUNDAY)
        cabin_recipe = Recipe.objects.create(household=self.cabin, name='Cabin chili')
        self.switch(self.home)
        page = self.client.get(f'/plan/{SUNDAY}/')
        self.assertContains(page, 'Dish Mon')
        self.assertContains(page, 'aria-label="Household: Home. Switch household"')
        self.assertNotContains(self.client.get('/recipes/'), 'Cabin chili')

        response = self.switch(self.cabin)
        self.assertRedirects(response, '/', fetch_redirect_response=False)
        self.assertEqual(AllowedUser.objects.get(email='mike@example.com').active_household, self.cabin)
        self.assertNotContains(self.client.get(f'/plan/{SUNDAY}/'), 'Dish Mon')
        self.assertContains(self.client.get('/recipes/'), 'Cabin chili')

        # Actions on the other household's things are refused while looking at this one.
        meal = home_plan.meals.first()
        self.assertEqual(self.client.post(f'/meals/{meal.pk}/lock/').status_code, 404)
        self.assertEqual(self.client.post(f'/meals/{meal.pk}/remove/').status_code, 404)
        self.assertEqual(self.client.post(f'/plan/{SUNDAY}/add/', {
            'date': '2026-10-25', 'meal_type': 'dinner', 'recipe_id': meal.recipe_id}).status_code, 404)
        self.assertEqual(self.client.post(f'/recipes/{meal.recipe_id}/rate/', {'value': '1'}).status_code, 404)
        self.assertTrue(home_plan.meals.filter(pk=meal.pk, locked=False).exists())

    def test_a_link_to_another_of_their_households_switches_to_it(self, *_):
        meal = services.generate_week(self.home, SUNDAY).meals.first()
        self.switch(self.cabin)
        # From a calendar event: the meal page follows it into Home.
        self.assertContains(self.client.get(f'/meals/{meal.pk}/'), meal.recipe.name)
        self.assertEqual(AllowedUser.objects.get(email='mike@example.com').active_household, self.home)
        # From the email: ?household= picks the household for a week page.
        self.switch(self.cabin)
        self.assertContains(self.client.get(f'/plan/{SUNDAY}/?household={self.home.pk}'), 'Dish Mon')

    def test_no_way_into_a_household_they_are_not_in(self, *_):
        stranger = make_household('Neighbors', 'friend@example.com')
        meal = services.generate_week(stranger, SUNDAY).meals.first()
        self.switch(self.home)
        self.assertEqual(self.switch(stranger).status_code, 400)
        self.assertEqual(self.client.get(f'/meals/{meal.pk}/').status_code, 404)
        self.assertEqual(self.client.get(f'/recipes/{meal.recipe_id}/').status_code, 404)
        RecipePhoto.objects.create(recipe=meal.recipe, image=b'x')
        self.assertEqual(self.client.get(f'/recipes/{meal.recipe_id}/photo/').status_code, 404)
        self.assertNotContains(self.client.get(f'/plan/{SUNDAY}/?household={stranger.pk}'), 'Dish Mon')
        self.assertEqual(AllowedUser.objects.get(email='mike@example.com').active_household, self.home)

    def test_someone_in_no_household_gets_their_own(self, *_):
        newcomer = User.objects.create(username='kim', email='kim@example.com')
        AllowedUser.objects.create(email='kim@example.com', name='Kim')
        self.client.force_login(newcomer)
        page = self.client.get(f'/plan/{SUNDAY}/')
        self.assertContains(page, 'Tell the planner what you like')
        self.assertNotContains(page, 'Switch household')  # only one household: no switcher
        self.assertEqual(list(AllowedUser.objects.get(email='kim@example.com').households.values_list('name', flat=True)),
                         ["Kim's household"])

    def test_signed_in_but_not_allowed_is_forbidden(self, *_):
        self.client.force_login(User.objects.create(username='admin', email='admin@example.com'))
        self.assertEqual(self.client.get('/recipes/').status_code, 403)

    def test_settings_and_preferences_are_per_household(self, *_):
        self.switch(self.home)
        self.client.post('/preferences/', {'max_minutes': '20', 'notes': 'Nut allergy', 'dislikes': 'olives'})
        self.switch(self.cabin)
        self.assertNotContains(self.client.get('/preferences/'), 'Nut allergy')
        self.assertEqual(HouseholdSettings.load(self.cabin).max_weeknight_minutes, 45)
        settings_page = self.client.get('/settings/')
        self.assertContains(settings_page, 'Shared by everyone in Cabin.')
        self.assertContains(settings_page, 'friend@example.com')
        self.assertNotContains(settings_page, 'partner@example.com')

        # A member's own dislikes go with them to every household they're in; other people's don't.
        User.objects.create(username='pat', email='partner@example.com', first_name='Pat')
        MemberPreferences.objects.create(user=User.objects.get(username='pat'), dislikes='cilantro')
        members = json.loads(planner._household_context(self.cabin))['preferences']['members']
        self.assertEqual(members, [{'name': 'Mike', 'loves': [], 'dislikes': ['olives']}])
        home_context = json.loads(planner._household_context(self.home))
        self.assertEqual(home_context['household_notes'], 'Nut allergy')
        self.assertEqual(len(home_context['preferences']['members']), 2)

    def test_planner_context_only_has_this_households_history(self, *_):
        services.generate_week(self.home, SUNDAY)
        Recipe.objects.create(household=self.cabin, name='Cabin chili', archived=True)
        with mock.patch('meals.planner.timezone.localdate', return_value=date(2026, 10, 30)):
            home = json.loads(planner._household_context(self.home))
            cabin = json.loads(planner._household_context(self.cabin))
        self.assertEqual(len(home['known_recipes']), 5)
        self.assertEqual(len(home['recent_history']), 5)
        self.assertEqual(home['archived_never_suggest'], [])
        self.assertEqual((cabin['known_recipes'], cabin['recent_history']), ([], []))
        self.assertEqual(cabin['archived_never_suggest'], ['Cabin chili'])

    def test_weekly_job_plans_and_emails_every_household(self, choose, *_):
        headers = {'X-Meal-Planner-Secret': 's3cret'}
        body = json.dumps({'week_start': SUNDAY.isoformat()})
        make_household('Empty')  # no members: skipped
        response = self.client.post('/internal/generate-week/', body, content_type='application/json', headers=headers)
        self.assertEqual(response.status_code, 200)
        results = {r['household']: r for r in response.json()['households']}
        self.assertEqual(sorted(results), ['Cabin', 'Home'])
        self.assertEqual(results['Home']['emailed'], ['mike@example.com', 'partner@example.com'])
        self.assertEqual(results['Cabin']['emailed'], ['friend@example.com', 'mike@example.com'])
        subjects = sorted(m.subject for m in mail.outbox)
        self.assertEqual(subjects, ['Dinners for the week of Oct 25 · Cabin', 'Dinners for the week of Oct 25 · Home'])
        home_email = next(m for m in mail.outbox if m.subject.endswith('Home'))
        self.assertIn(f'?household={self.home.pk}', home_email.alternatives[0][0])

        # One household failing doesn't stop the others, and is reported so Airflow retries.
        mail.outbox.clear()
        failing = make_household('Broken', 'friend@example.com')
        choose.side_effect = lambda home, *a, **k: (_ for _ in ()).throw(RuntimeError('boom')) \
            if home == failing else fake_choose_week(home, *a, **k)
        response = self.client.post('/internal/generate-week/', json.dumps({'week_start': '2026-11-01'}),
                                    content_type='application/json', headers=headers)
        self.assertEqual(response.status_code, 500)
        results = {r['household']: r for r in response.json()['households']}
        self.assertEqual(results['Broken']['error'], 'RuntimeError: boom')
        self.assertEqual(len(results['Home']['meals']), 5)
        self.assertEqual(len(mail.outbox), 2)

    @mock.patch('meals.kroger.exchange_code')
    @mock.patch('meals.kroger.authorize_url', return_value='https://kroger.example/authorize')
    def test_kroger_connects_to_the_household_it_was_started_from(self, authorize, exchange, *_):
        self.switch(self.cabin)
        self.client.get('/kroger/connect/')
        state = self.client.session['kroger_state']
        self.switch(self.home)  # switched while away at King Soopers
        self.client.get('/kroger/callback/', {'state': state, 'code': 'abc'})
        exchange.assert_called_once_with(self.cabin, 'abc')


class HouseholdMigrationTests(TransactionTestCase):
    """0009 puts a pre-household database into one household, so nobody loses anything on upgrade."""

    def migrate(self, target):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([('meals', target)])
        return executor.loader.project_state([('meals', target)]).apps

    def test_existing_data_moves_into_one_household(self):
        old = self.migrate('0007_recipe_photos')
        AllowedUser = old.get_model('meals', 'AllowedUser')
        AllowedUser.objects.create(email='michaeldcarrell@gmail.com')
        AllowedUser.objects.create(email='hannamarie95@gmail.com', receives_plan_email=False)
        old.get_model('meals', 'HouseholdSettings').objects.create(pk=1, calendar_id='cal@group.calendar.google.com')
        old.get_model('meals', 'KrogerAuth').objects.create(
            pk=1, access_token='a', refresh_token='r', expires_at=timezone.now())
        recipe = old.get_model('meals', 'Recipe').objects.create(name='Tacos')
        plan = old.get_model('meals', 'MealPlan').objects.create(week_start=SUNDAY)
        old.get_model('meals', 'PlannedMeal').objects.create(plan=plan, date=SUNDAY, recipe=recipe)
        old.get_model('meals', 'GroceryProduct').objects.create(grocery_item='onion', upc='1')

        new = self.migrate('0010_household_required')
        [household] = new.get_model('meals', 'Household').objects.all()
        self.assertEqual(household.name, 'Home')
        members = new.get_model('meals', 'Membership').objects.filter(household=household)
        self.assertEqual(sorted(members.values_list('member__email', 'receives_plan_email')),
                         [('hannamarie95@gmail.com', False), ('michaeldcarrell@gmail.com', True)])
        self.assertEqual(set(new.get_model('meals', 'AllowedUser').objects.values_list('active_household', flat=True)),
                         {household.pk})
        for name in ('HouseholdSettings', 'KrogerAuth', 'MealPlan', 'Recipe', 'GroceryProduct'):
            self.assertEqual(list(new.get_model('meals', name).objects.values_list('household', flat=True)),
                             [household.pk], name)
        self.assertEqual(new.get_model('meals', 'HouseholdSettings').objects.get().calendar_id,
                         'cal@group.calendar.google.com')
