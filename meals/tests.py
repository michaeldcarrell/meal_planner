import json
from datetime import date
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings

from meals import planner, services
from meals.models import (
    AllowedUser, GroceryProduct, HouseholdSettings, MealPlan, MealSwap, PlannedMeal, Rating, Recipe,
    week_start_for,
)

SUNDAY = date(2026, 10, 25)


def new_recipe(name, items=('yellow onion',)):
    return planner.NewRecipe(
        name=name, summary=f'{name} summary', cuisine='Mexican', total_minutes=30, servings=4,
        ingredients=[planner.Ingredient(name=i, quantity='1', grocery_item=i, pantry_staple=False) for i in items]
        + [planner.Ingredient(name='salt', quantity='1 tsp', grocery_item='salt', pantry_staple=True)],
        instructions='1. Cook.',
    )


def fake_choose_week(dates, locked):
    return [
        planner.DinnerChoice(date=d.isoformat(), existing_recipe_id=None, new_recipe=new_recipe(f'Dish {d:%a}'),
                             why='variety')
        for d in dates if d not in locked
    ]


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', INTERNAL_API_SECRET='s3cret')
@mock.patch('meals.calendar_sync.sync_meal')
@mock.patch('meals.calendar_sync.delete_meal_event')
class PlanServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username='mike', email='mike@example.com')
        AllowedUser.objects.create(email='mike@example.com')
        AllowedUser.objects.create(email='partner@example.com')
        AllowedUser.objects.create(email='quiet@example.com', receives_plan_email=False)

    def test_week_start_is_sunday(self, *_):
        self.assertEqual(week_start_for(date(2026, 10, 29)), SUNDAY)
        self.assertEqual(week_start_for(SUNDAY), SUNDAY)

    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_generate_week_fills_weeknights_and_is_idempotent(self, choose, delete_event, sync):
        plan = services.generate_week(SUNDAY)
        self.assertEqual([m.date.weekday() for m in plan.meals.all()], [0, 1, 2, 3, 4])
        self.assertEqual(sync.call_count, 5)
        services.generate_week(SUNDAY)
        self.assertEqual(choose.call_count, 1)
        self.assertEqual(plan.meals.count(), 5)

    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_regenerate_keeps_locked_meals(self, choose, delete_event, sync):
        plan = services.generate_week(SUNDAY)
        keep = plan.meals.first()
        keep.locked = True
        keep.save()
        services.generate_week(SUNDAY, regenerate=True)
        self.assertEqual(plan.meals.count(), 5)
        self.assertTrue(plan.meals.filter(pk=keep.pk).exists())
        self.assertEqual(delete_event.call_count, 4)
        self.assertEqual(choose.call_args.args[1], {keep.date: keep.recipe})

    def test_resolve_recipe_reuses_known_recipe(self, *_):
        known = Recipe.objects.create(name='Tacos')
        choice = planner.DinnerChoice(date='2026-10-26', existing_recipe_id=known.pk, new_recipe=None, why='loved')
        self.assertEqual(planner.resolve_recipe(choice), known)

    @mock.patch('meals.planner.choose_replacement')
    def test_replace_records_swap(self, choose_replacement, delete_event, sync):
        plan = MealPlan.objects.create(week_start=SUNDAY)
        old = Recipe.objects.create(name='Chili')
        meal = PlannedMeal.objects.create(plan=plan, date=date(2026, 10, 26), recipe=old)
        choose_replacement.return_value = planner.DinnerChoice(
            date='2026-10-26', existing_recipe_id=None, new_recipe=new_recipe('Stir fry'), why='quicker')
        services.replace_with_suggestion(meal, self.user, 'something quicker')
        meal.refresh_from_db()
        self.assertEqual(meal.recipe.name, 'Stir fry')
        swap = MealSwap.objects.get()
        self.assertEqual((swap.recipe, swap.reason), (old, 'something quicker'))
        sync.assert_called_once()

    def test_grocery_list_merges_items_and_flags_staples(self, *_):
        plan = MealPlan.objects.create(week_start=SUNDAY)
        for i, name in enumerate(['A', 'B']):
            recipe = planner.resolve_recipe(planner.DinnerChoice(
                date='x', existing_recipe_id=None, new_recipe=new_recipe(name, ['Yellow Onion', f'item {name}']),
                why=''))
            PlannedMeal.objects.create(plan=plan, date=date(2026, 10, 26 + i), recipe=recipe)
        items = {i['grocery_item']: i for i in services.grocery_list(plan)}
        self.assertEqual(len(items['yellow onion']['uses']), 2)
        self.assertTrue(items['salt']['pantry_staple'])
        self.assertEqual(services.grocery_list(plan)[-1]['grocery_item'], 'salt')

    @mock.patch('meals.kroger.add_to_cart')
    @mock.patch('meals.kroger.search_products')
    def test_push_to_cart_remembers_products(self, search, add, *_):
        household = HouseholdSettings.load()
        household.kroger_location_id = '62000123'
        household.save()
        plan = MealPlan.objects.create(week_start=SUNDAY)
        GroceryProduct.objects.create(grocery_item='yellow onion', upc='111', description='Onion')
        search.side_effect = lambda term, loc, limit: [] if term == 'unicorn' else [{'upc': '222', 'description': 'Beans'}]
        push = services.push_to_cart(plan, ['yellow onion', 'black beans', 'unicorn'], self.user)
        add.assert_called_once_with([{'upc': '111', 'quantity': 1}, {'upc': '222', 'quantity': 1}])
        self.assertEqual([r['status'] for r in push.results], ['added', 'added', 'no match'])
        self.assertTrue(GroceryProduct.objects.filter(grocery_item='black beans', upc='222').exists())

    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_internal_endpoint_generates_and_emails_once(self, *_):
        url = '/internal/generate-week/'
        self.assertEqual(self.client.post(url).status_code, 403)
        body = json.dumps({'week_start': SUNDAY.isoformat()})
        headers = {'X-Meal-Planner-Secret': 's3cret'}
        response = self.client.post(url, body, content_type='application/json', headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['meals']), 5)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(sorted(mail.outbox[0].to), ['mike@example.com', 'partner@example.com'])
        self.assertIn('Dish Mon', mail.outbox[0].body)
        self.client.post(url, body, content_type='application/json', headers=headers)
        self.assertEqual(len(mail.outbox), 1)

    @mock.patch('meals.planner.choose_week', side_effect=fake_choose_week)
    def test_pages_render_and_rating_works(self, *_):
        plan = services.generate_week(SUNDAY)
        meal = plan.meals.first()
        self.client.force_login(self.user)
        for url in [f'/plan/{SUNDAY}/', f'/meals/{meal.pk}/', '/recipes/', f'/recipes/{meal.recipe_id}/',
                    f'/plan/{SUNDAY}/groceries/', '/plan/2026-11-01/']:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.client.post(f'/recipes/{meal.recipe_id}/rate/', {'value': '1', 'comment': 'yum'})
        self.assertEqual(Rating.objects.get().value, 1)
        self.client.post(f'/recipes/{meal.recipe_id}/rate/', {'value': '0'})
        self.assertFalse(Rating.objects.exists())

    def test_pages_require_login(self, *_):
        self.assertEqual(self.client.get('/recipes/').status_code, 302)


class PlannerRequestTests(TestCase):
    """Runs the real SDK request/parse path against a canned HTTP response (no network)."""

    def test_choose_week_parses_structured_output(self):
        import anthropic
        import httpx2

        payload = {'dinners': [{
            'date': '2026-10-26', 'existing_recipe_id': None, 'why': 'new',
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
            choices = planner.choose_week([date(2026, 10, 26)], {})
        self.assertEqual(choices[0].new_recipe.name, 'Tacos')
        self.assertEqual(seen['body']['output_config']['format']['type'], 'json_schema')
        self.assertEqual(seen['body']['fallbacks'], 'default')
