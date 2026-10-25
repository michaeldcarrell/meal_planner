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

from meals import kroger, services
from meals.models import MealPlan, PlannedMeal, Rating, Recipe, week_start_for

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
        minutes = [m.recipe.total_minutes for m in meals if m.recipe.total_minutes]
        return render(request, 'meals/plan.html', {
            'plan': plan,
            'week_start': start,
            'week_end': start + timedelta(days=6),
            'today': timezone.localdate(),
            'meals': meals,
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
            messages.error(request, 'Plan this week first; the grocery list comes from its dinners.')
            return redirect('plan', start.isoformat())
        return redirect('groceries', start.isoformat())


class KrogerConnect(LoginRequiredMixin, View):
    def get(self, request):
        state = secrets.token_urlsafe(24)
        request.session['kroger_state'] = state
        request.session['kroger_next'] = request.GET.get('next', '/')
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
            'meals': [{'date': m.date.isoformat(), 'recipe': m.recipe.name} for m in plan.meals.select_related('recipe')],
            'emailed': sent_to,
        })
