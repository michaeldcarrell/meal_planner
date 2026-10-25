"""Claude-backed meal selection: picks a week of dinners, or one replacement, from household history."""
import json
import logging
from datetime import date, timedelta

import anthropic
from django.conf import settings
from django.db.models import Sum
from django.utils import timezone
from pydantic import BaseModel, Field

from meals.models import (
    COOK_TIMES, PROTEINS, HouseholdSettings, MealSwap, MemberPreferences, PlannedMeal, Rating, Recipe,
)

logger = logging.getLogger(__name__)

HISTORY_WEEKS = 6
MAX_KNOWN_RECIPES = 150

SYSTEM_PROMPT = """You plan weeknight dinners for one household. Each dinner is either a recipe the \
household already knows (refer to it by its id) or a new recipe you write out in full.

How to choose:
- Respect the household notes (allergies and hard dislikes are absolute).
- Follow the household's preferences: stay within the cooking time (for quick meals, aim for the \
low end of the range), never use proteins marked "never" and lean on the ones marked "more", and favor \
the cuisines and styles they love while still mixing in others now and then.
- Never build a dinner around a food any member dislikes, and leave it out of recipes where it isn't \
essential. Work in foods members love when it fits.
- Prefer recipes the household rated well; never pick ones they disliked or archived, and treat \
recent swaps as "not in the mood for this" signals about the dish and the stated reason.
- Don't repeat anything eaten in the recent history unless the household loves it and it has been \
at least three weeks.
- Vary proteins, cuisines and cooking methods across the week; keep weeknights within the time limit.
- New recipes should be ones a home cook can shop for at a regular American grocery store \
(King Soopers / Kroger).

For new recipes, list every ingredient. `quantity` is what the recipe uses ("2 lb", "1 can (15 oz)"). \
`grocery_item` is the generic thing to buy at the store, singular and without prep words \
("boneless skinless chicken thighs", "yellow onion", "black beans") so the same item can be merged \
across recipes. Mark `pantry_staple` true for things most kitchens already have (salt, pepper, \
cooking oil, common dried spices, flour, sugar). Instructions are concise numbered steps."""


class Ingredient(BaseModel):
    name: str
    quantity: str
    grocery_item: str
    pantry_staple: bool


class NewRecipe(BaseModel):
    name: str
    summary: str = Field(description='One or two appetizing sentences.')
    cuisine: str
    total_minutes: int
    servings: int
    ingredients: list[Ingredient]
    instructions: str


class DinnerChoice(BaseModel):
    date: str = Field(description='YYYY-MM-DD')
    existing_recipe_id: int | None = Field(description='Id of a known recipe, or null for a new one.')
    new_recipe: NewRecipe | None = Field(description='Full recipe when existing_recipe_id is null.')
    why: str = Field(description='Short reason for this pick, shown to the household.')


class WeekChoices(BaseModel):
    dinners: list[DinnerChoice]


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _preferences(household: HouseholdSettings) -> dict:
    labels = dict(PROTEINS)
    proteins = household.protein_preferences or {}
    time_label = next(
        (f'{label} ({detail})' for minutes, label, detail in COOK_TIMES if minutes == household.max_weeknight_minutes),
        f'Up to {household.max_weeknight_minutes} min')
    return {
        'cooking_time': time_label,
        'proteins_more_often': [labels[k] for k, v in proteins.items() if v == 'more' and k in labels],
        'proteins_never': [labels[k] for k, v in proteins.items() if v == 'never' and k in labels],
        'favorite_cuisines': household.favorite_cuisines,
        'favorite_styles': household.meal_styles,
        'members': [
            {'name': p.user.first_name or p.user.username, 'loves': p.love_list, 'dislikes': p.dislike_list}
            for p in MemberPreferences.objects.select_related('user')
            if p.love_list or p.dislike_list
        ],
    }


def _household_context(exclude_dates: set[date] | None = None) -> str:
    """Everything the planner should know, as a compact JSON-ish text block."""
    household = HouseholdSettings.load()
    today = timezone.localdate()

    recipe_rows = (
        Recipe.objects.filter(archived=False)
        .annotate(total=Sum('ratings__value'))
        .order_by('-total', '-created_at')[:MAX_KNOWN_RECIPES]
    )
    known = [
        {'id': r.id, 'name': r.name, 'cuisine': r.cuisine, 'minutes': r.total_minutes, 'score': r.total or 0}
        for r in recipe_rows
    ]

    comments = [
        {'recipe': r.recipe.name, 'rating': 'liked' if r.value > 0 else 'disliked', 'comment': r.comment}
        for r in Rating.objects.exclude(comment='').select_related('recipe').order_by('-updated_at')[:40]
    ]

    history_qs = PlannedMeal.objects.filter(
        date__gte=today - timedelta(weeks=HISTORY_WEEKS)).select_related('recipe')
    if exclude_dates:
        history_qs = history_qs.exclude(date__in=exclude_dates)
    history = [{'date': m.date.isoformat(), 'recipe_id': m.recipe_id, 'name': m.recipe.name} for m in history_qs]

    swaps = [
        {'recipe': s.recipe.name, 'reason': s.reason}
        for s in MealSwap.objects.filter(created_at__gte=timezone.now() - timedelta(weeks=HISTORY_WEEKS))
        .select_related('recipe').order_by('-created_at')[:30]
    ]
    archived = list(Recipe.objects.filter(archived=True).values_list('name', flat=True)[:100])

    return json.dumps({
        'household_notes': household.dietary_notes or '(none)',
        'servings': household.servings,
        'max_weeknight_minutes': household.max_weeknight_minutes,
        'preferences': _preferences(household),
        'known_recipes': known,
        'rating_comments': comments,
        'recent_history': history,
        'recent_swaps': swaps,
        'archived_never_suggest': archived,
    }, indent=1)


def _ask(instructions: str, context: str) -> WeekChoices:
    response = _client().beta.messages.parse(
        model=settings.CLAUDE_MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{
            'role': 'user',
            'content': f'<household>\n{context}\n</household>\n\n{instructions}',
        }],
        output_format=WeekChoices,
        output_config={'effort': 'medium'},
        # On a safety decline, let the API retry on a fallback model instead of failing the run.
        betas=['server-side-fallback-2026-07-01'],
        fallbacks='default',
    )
    if response.stop_reason == 'refusal':
        raise RuntimeError(f'Claude declined to plan meals: {response.stop_details}')
    if response.stop_reason == 'max_tokens' or response.parsed_output is None:
        raise RuntimeError(f'Claude returned an incomplete plan (stop_reason={response.stop_reason})')
    logger.info('planner usage: %s', response.usage)
    return response.parsed_output


def choose_week(dates: list[date], locked: dict[date, Recipe]) -> list[DinnerChoice]:
    """Pick dinners for each of `dates` that isn't already in `locked`."""
    household = HouseholdSettings.load()
    open_dates = [d for d in dates if d not in locked]
    if not open_dates:
        return []
    locked_text = '\n'.join(f'- {d:%A %Y-%m-%d}: {r.name} (id {r.id})' for d, r in sorted(locked.items()))
    instructions = (
        f"Plan dinners for these dates: {', '.join(f'{d:%A} {d.isoformat()}' for d in open_dates)}.\n"
        f"Already set for this week (don't duplicate them): \n{locked_text or '- none'}\n"
        f"Aim for about {household.favorites_per_week} well-liked known recipes this week (fewer if "
        f"there aren't enough), and new recipes for the rest. Return exactly one dinner per listed date."
    )
    choices = _ask(instructions, _household_context(exclude_dates=set(dates)))
    by_date = {c.date: c for c in choices.dinners}
    missing = [d for d in open_dates if d.isoformat() not in by_date]
    if missing:
        raise RuntimeError(f'Claude skipped dates: {missing}')
    return [by_date[d.isoformat()] for d in open_dates]


def choose_replacement(meal: PlannedMeal, hint: str) -> DinnerChoice:
    """Pick one dinner to replace `meal`, honoring the requester's free-text `hint`."""
    others = meal.plan.meals.exclude(pk=meal.pk).select_related('recipe')
    others_text = '\n'.join(f'- {m.date:%A}: {m.recipe.name}' for m in others)
    instructions = (
        f"Replace the dinner on {meal.date:%A} {meal.date.isoformat()} (currently \"{meal.recipe.name}\", "
        f"which the household wants swapped out).\n"
        f"What they asked for instead: {hint or '(no preference given — just something different)'}\n"
        f"The rest of this week:\n{others_text or '- nothing else planned'}\n"
        f"Return exactly one dinner, for {meal.date.isoformat()}, and don't pick \"{meal.recipe.name}\" again."
    )
    choices = _ask(instructions, _household_context())
    if not choices.dinners:
        raise RuntimeError('Claude returned no replacement')
    choice = choices.dinners[0]
    choice.date = meal.date.isoformat()
    return choice


def resolve_recipe(choice: DinnerChoice) -> Recipe:
    """Turn a DinnerChoice into a saved Recipe (looking up a known one, or creating the new one)."""
    if choice.existing_recipe_id:
        recipe = Recipe.objects.filter(pk=choice.existing_recipe_id, archived=False).first()
        if recipe:
            return recipe
        logger.warning('Claude referenced unknown/archived recipe id %s', choice.existing_recipe_id)
    if not choice.new_recipe:
        raise RuntimeError(f'Dinner for {choice.date} has neither a known recipe nor a new one')
    new = choice.new_recipe
    return Recipe.objects.create(
        name=new.name,
        summary=new.summary,
        cuisine=new.cuisine,
        total_minutes=new.total_minutes,
        servings=new.servings,
        ingredients=[i.model_dump() for i in new.ingredients],
        instructions=new.instructions,
    )
